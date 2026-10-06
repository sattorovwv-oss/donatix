"""Деньги за сутки — с 00:00 до 00:00 следующего дня (по времени Душанбе).

Для владельца: сколько за сутки перевели на карты и в крипте, сколько из этого —
его прибыль (продажи минус закупка у поставщика) и сколько можно отправить
поставщику, чтобы пополнять его баланс, а прибыль забирать себе.

  отправить поставщику = поступило − прибыль

Остаток поступлений, не потраченный клиентами за сутки, остаётся на их балансах
(«деньги клиентов») — его тоже закупают у поставщика, только позже.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from . import db, timez
from .config import Config

CUTOFF_HOUR = 0   # сутки с 00:00 до 00:00 следующего дня (по времени Душанбе)


def cutoff_hour(conn: sqlite3.Connection) -> int:
    raw = db.get_setting(conn, "report.cutoff_hour")
    return int(raw) if raw and raw.isdigit() and 0 <= int(raw) <= 23 else CUTOFF_HOUR


def window(conn: sqlite3.Connection, now: datetime | None = None, days_back: int = 0) -> tuple[datetime, datetime]:
    """Сутки 00:00 → 00:00 (местное время). days_back=0 — текущие (ещё идут), 1 — прошлые закрытые."""
    tz = timez.zone(timez.site_zone_name(conn))
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour = cutoff_hour(conn)
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    start -= timedelta(days=days_back)   # арифметика по местным часам: всегда «12:00»
    return start, start + timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _sales(conn: sqlite3.Connection, a: str, b: str) -> tuple[sqlite3.Row, int]:
    o = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_micro), 0) AS revenue, COALESCE(SUM(cost_micro), 0) AS cost "
        "FROM orders WHERE status = 'completed' AND COALESCE(completed_at, created_at) >= ? "
        "AND COALESCE(completed_at, created_at) < ?", (a, b)).fetchone()
    # Реферальные бонусы — наш расход: вычитаем, чтобы видеть чистую прибыль
    referral = int(conn.execute(
        "SELECT COALESCE(SUM(amount_micro), 0) FROM referral_rewards WHERE created_at >= ? AND created_at < ?",
        (a, b)).fetchone()[0])
    return o, referral


def net_profit(conn: sqlite3.Connection, a: str, b: str) -> int:
    """Чистая прибыль за [a, b): продажи − закупка − реферальные бонусы."""
    o, referral = _sales(conn, a, b)
    return int(o["revenue"]) - int(o["cost"]) - referral - _dcoin(conn, a, b)


def _dcoin(conn: sqlite3.Connection, a: str, b: str) -> int:
    """Доля прибыли, отложенная в копилку D-коина, — тоже наш расход."""
    from . import dcoin
    return dcoin.pool_added(conn, a, b)


def summary(conn: sqlite3.Connection, config: Config, start: datetime, end: datetime) -> dict[str, Any]:
    from .payments import settings as pay_settings
    from .payments import title_for
    a, b = _iso(start), _iso(end)
    rate = Decimal(str(pay_settings(conn, config)["tjs_rate"] or 0))

    methods = []
    for r in conn.execute(
            "SELECT method, pay_currency, COALESCE(auto_kind, '') AS auto, COUNT(*) AS n, SUM(amount_micro) AS usd, "
            "SUM(CAST(pay_amount AS REAL)) AS paid FROM payments WHERE status = 'paid' "
            "AND resolved_at >= ? AND resolved_at < ? GROUP BY method, pay_currency, auto ORDER BY usd DESC",
            (a, b)).fetchall():
        methods.append({"title": title_for(conn, config, r["method"]), "currency": r["pay_currency"],
                        "auto": bool(r["auto"]), "count": r["n"], "usd_micro": int(r["usd"] or 0),
                        "paid": Decimal(str(round(r["paid"] or 0, 2)))})
    received = sum(m["usd_micro"] for m in methods)
    to_card = sum(m["usd_micro"] for m in methods if not m["auto"])
    by_currency: dict[str, Decimal] = {}
    for m in methods:
        by_currency[m["currency"]] = by_currency.get(m["currency"], Decimal(0)) + m["paid"]

    o, referral = _sales(conn, a, b)
    revenue, cost = int(o["revenue"]), int(o["cost"])
    profit = revenue - cost
    dcoin_pool = _dcoin(conn, a, b)
    net = profit - referral - dcoin_pool
    # Доли кассиров — из чистой прибыли; остальное владельцу
    from . import cashiers
    staff = [c for c in cashiers.shares(conn, config, a, b, net) if c["share"] or c["active"]]
    staff_total = sum(c["share"] for c in staff)
    manual = int(conn.execute(
        "SELECT COALESCE(SUM(amount_micro), 0) FROM transactions WHERE amount_micro > 0 AND order_id IS NULL "
        "AND created_by IS NOT NULL AND note NOT LIKE 'Пополнение:%' AND created_at >= ? AND created_at < ?",
        (a, b)).fetchone()[0])
    clients = int(conn.execute("SELECT COALESCE(SUM(balance_micro), 0) FROM users WHERE role = 'client'")
                  .fetchone()[0])
    supplier = db.get_setting(conn, "supplier_balance")
    return {
        "start": start, "end": end, "rate": rate,
        "methods": methods, "received": received, "to_card": to_card, "crypto": received - to_card,
        "by_currency": by_currency, "payments": sum(m["count"] for m in methods),
        "orders": int(o["n"]), "revenue": revenue, "cost": cost, "gross_profit": profit,
        "referral": referral, "dcoin": dcoin_pool, "profit": net,
        "cashiers": staff, "cashiers_total": staff_total, "owner_profit": net - staff_total,
        "to_supplier": max(received - net - dcoin_pool, 0), "manual_credit": manual,
        "clients_balance": clients, "supplier_balance": supplier,
    }


def history(conn: sqlite3.Connection, config: Config, days: int = 14) -> list[dict[str, Any]]:
    out = []
    for back in range(days):
        start, end = window(conn, days_back=back)
        out.append(summary(conn, config, start, end))
    return out


def tjs(micro: int, rate: Decimal) -> str:
    """$ в микро → «1 234.50» сомони по курсу оплат."""
    from .money import SCALE
    value = (Decimal(micro) / Decimal(SCALE) * rate).quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ")


def usd(micro: int) -> str:
    """$ в микро → «1 234.50» (для отчётов хватает центов)."""
    from .money import SCALE
    value = (Decimal(micro) / Decimal(SCALE)).quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ")


def report_text(s: dict[str, Any]) -> str:
    from .packs import plural

    def both(micro: int) -> str:
        return f"${usd(micro)}" + (f" ≈ {tjs(micro, s['rate'])} с." if s["rate"] else "")

    def fmt(micro: int) -> str:
        return usd(micro)

    lines = [f"💰 <b>Деньги за {s['start']:%d.%m.%Y}</b> (сутки 00:00 → 00:00)", ""]
    lines.append(f"📥 Поступило: <b>{both(s['received'])}</b> ({s['payments']} пополн.)")
    for m in s["methods"]:
        lines.append(f"   • {m['title']}: {m['paid']:.2f} {m['currency']} · {m['count']} шт.")
    if s["manual_credit"]:
        lines.append(f"   • вручную админом (не в итоге): ${fmt(s['manual_credit'])}")
    lines += ["",
              f"🛒 Продано: {s['orders']} {plural(s['orders'], 'заказ', 'заказа', 'заказов')} на {both(s['revenue'])}",
              f"🏭 Закупка у поставщика: {both(s['cost'])}",
              *([f"📈 Прибыль с продаж: {both(s['gross_profit'])}"] if s.get("referral") or s.get("dcoin") else []),
              *([f"🎁 Бонусы рефералам: −{both(s['referral'])}"] if s.get("referral") else []),
              *([f"🪙 В копилку D-коина (отложить): −{both(s['dcoin'])}"] if s.get("dcoin") else []),
              *([f"📈 Прибыль: {both(s['profit'])}",
                 *(f"👤 Кассир {c['name']} ({c['percent']}%, его банки ${usd(c['received'])}): "
                   f"−{both(c['share'])}" for c in s["cashiers"]),
                 f"💼 <b>Вам: {both(s['owner_profit'])}</b>"] if s.get("cashiers") else
                [f"📈 <b>Ваша прибыль: {both(s['profit'])}</b>"]),
              "",
              f"➡️ <b>Отправить поставщику: {both(s['to_supplier'])}</b>",
              "<i>= поступило − прибыль. Прибыль забираете себе.</i>",
              "",
              f"👛 Деньги клиентов на балансах: {both(s['clients_balance'])}",
              f"🏦 Баланс поставщика: {'$' + s['supplier_balance'] if s['supplier_balance'] else '—'}"]
    return "\n".join(lines)


def maybe_send(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> bool:
    """Раз в сутки в 00:00 — итог прошедших суток 00:00 → 00:00 в админ-бот."""
    from .worker import notify_admin
    if (db.get_setting(conn, "report.daily_on") or "1") != "1":
        return False
    start, end = window(conn, now, days_back=1)
    key = end.strftime("%Y-%m-%d")
    if db.get_setting(conn, "finance.last_day") == key:
        return False
    db.set_setting(conn, "finance.last_day", key)
    notify_admin(config, report_text(summary(conn, config, start, end)), html=True)
    from .cashiers import daily_reports
    daily_reports(conn, config, start, end)
    return True


# ── Учёт денег за любой период: всё в одном месте ───────────────
MAX_DAY_ROWS = 62   # дольше — строки по месяцам


def _extras(conn: sqlite3.Connection, a: str, b: str) -> dict[str, int]:
    """Что не входит в сводку суток: возвраты, обмен D-коинов на баланс."""
    ref = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_micro), 0) FROM orders WHERE status = 'failed' "
        "AND COALESCE(completed_at, updated_at) >= ? AND COALESCE(completed_at, updated_at) < ?", (a, b)).fetchone()
    exch = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(-pool_micro), 0) FROM dcoin_ledger WHERE amount < 0 AND pool_micro < 0 "
        "AND created_at >= ? AND created_at < ?", (a, b)).fetchone()
    return {"refunds": int(ref[0]), "refunded": int(ref[1]), "exchanges": int(exch[0]), "exchanged": int(exch[1])}


def flow(s: dict[str, Any]) -> list[dict[str, Any]]:
    """Куда ушли деньги от продаж: поставщику, рефералам, в копилку D-коина, кассирам, вам."""
    parts = [("cost", "Поставщику (закупка)", s["cost"]), ("ref", "Рефералам", s["referral"]),
             ("dcoin", "Копилка D-коина", s["dcoin"]), ("cash", "Кассирам", s["cashiers_total"]),
             ("own", "Вам", s["owner_profit"])]
    total = sum(max(0, v) for _, _, v in parts) or 1
    return [{"key": k, "label": t, "micro": v, "pct": round(max(0, v) * 100 / total, 1)} for k, t, v in parts]


def _first_day(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MIN(t) FROM (SELECT MIN(created_at) AS t FROM orders "
                       "UNION ALL SELECT MIN(created_at) FROM payments)").fetchone()
    return timez.parse(row[0]) if row and row[0] else None


def ledger(conn: sqlite3.Connection, config: Config, pr: Any, now: datetime | None = None) -> dict[str, Any]:
    """Отчёт за период pr (periods.Period): итог, куда ушли деньги и строки по дням (или месяцам)."""
    tz = timez.zone(timez.site_zone_name(conn))
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = pr.end or today + timedelta(days=1)
    start = pr.start
    if start is None:
        first = _first_day(conn)
        start = (first or now).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = min(end, today + timedelta(days=1))
    total = summary(conn, config, start, end)
    total.update(_extras(conn, _iso(start), _iso(end)))
    by_month = (end - start).days > MAX_DAY_ROWS
    rows: list[dict[str, Any]] = []
    cur = start
    while cur < end:
        if by_month:
            nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        else:
            nxt = cur + timedelta(days=1)
        nxt = min(nxt, end)
        s = summary(conn, config, cur, nxt)
        s.update(_extras(conn, _iso(cur), _iso(nxt)))
        s["label"] = cur.strftime("%m.%Y") if by_month else cur.strftime("%d.%m.%Y")
        s["shares"] = {c["tg_id"]: c["share"] for c in s["cashiers"]}
        rows.append(s)
        cur = nxt
    rows.reverse()                                   # свежие сверху
    names = {c["tg_id"]: c["name"] for c in total["cashiers"]}
    from . import dcoin
    st = dcoin.state(conn)
    return {"total": total, "flow": flow(total), "rows": rows, "by_month": by_month,
            "cashiers": [{"tg_id": k, "name": v} for k, v in names.items()],
            "dcoin_pool": st["pool"], "dcoin_coins": dcoin.fmt_d(st["supply"]),
            "start": start, "end": end}
