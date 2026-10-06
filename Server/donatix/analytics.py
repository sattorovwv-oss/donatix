"""Аналитика заказов за период: карточки, динамика по дням/часам и разбивка по типам товаров.

Одна функция и для клиента (свои заказы), и для админа (все заказы). Дни считаются
по местному времени (DONATIX_TZ_OFFSET, по умолчанию Душанбе, UTC+5)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .suppliers import KIND_TITLES

PERIODS = {"today": ("Сегодня", 1), "7d": ("7 дней", 7), "30d": ("30 дней", 30), "all": ("Всё время", None)}


def build(conn: sqlite3.Connection, period: str, *, user_id: int | None = None, tz_hours: int = 5) -> dict[str, Any]:
    period = "today" if period == "24h" else period   # старые ссылки «24 часа» → «Сегодня»
    period = period if period in PERIODS else "30d"
    days = PERIODS[period][1]
    now = datetime.now(timezone.utc)
    shift = f"{tz_hours:+d} hours"

    where, args = "1=1", []
    if user_id is not None:
        where += " AND user_id = ?"
        args.append(user_id)
    if days:
        # целые сутки 00:00 → 23:59 по местному времени: сегодня и days−1 суток до него
        local_midnight = (now + timedelta(hours=tz_hours)).replace(hour=0, minute=0, second=0, microsecond=0)
        since = (local_midnight - timedelta(days=days - 1, hours=tz_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        where += " AND created_at >= ?"
        args.append(since)
    else:
        first = conn.execute(f"SELECT MIN(created_at) FROM orders WHERE {where}", args).fetchone()[0]
        since = first[:19] if first else now.strftime("%Y-%m-%dT%H:%M:%S")

    t = conn.execute(
        "SELECT COUNT(*) AS created, "
        "SUM(status = 'completed') AS done, SUM(status = 'failed') AS refunded, "
        "COALESCE(SUM(CASE WHEN status = 'completed' THEN total_micro END), 0) AS turnover, "
        "COALESCE(SUM(CASE WHEN status = 'completed' THEN total_micro - cost_micro END), 0) AS profit "
        f"FROM orders WHERE {where}", args).fetchone()

    # Пополнения: зачисления на баланс, не связанные с заказом (заявки, ручные начисления)
    tw, targs = "type = 'credit' AND order_id IS NULL AND created_at >= ?", [since]
    if user_id is not None:
        tw += " AND user_id = ?"
        targs.append(user_id)
    topped = conn.execute(f"SELECT COALESCE(SUM(amount_micro), 0) FROM transactions WHERE {tw}", targs).fetchone()[0]

    # Динамика: по часам за сутки, иначе по дням; пустые интервалы тоже показываем
    hourly = period == "today"
    fmt = "%Y-%m-%d %H" if hourly else "%Y-%m-%d"
    rows = {r["b"]: r for r in conn.execute(
        f"SELECT strftime('{fmt}', created_at, '{shift}') AS b, COUNT(*) AS created, "
        "SUM(status = 'completed') AS done, SUM(status = 'failed') AS refunded "
        f"FROM orders WHERE {where} GROUP BY b", args)}
    local_now = now + timedelta(hours=tz_hours)
    start = datetime.strptime(since[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(hours=tz_hours)
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    cur = start.replace(minute=0, second=0, microsecond=0)
    if not hourly:
        cur = cur.replace(hour=0)
    series = []
    while cur <= local_now and len(series) < 400:
        key = cur.strftime(fmt)
        r = rows.get(key)
        series.append({"label": cur.strftime("%H:00" if hourly else "%d.%m"),
                       "created": r["created"] if r else 0, "done": (r["done"] or 0) if r else 0,
                       "refunded": (r["refunded"] or 0) if r else 0})
        cur += step

    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS created, SUM(status = 'completed') AS done, SUM(status = 'failed') AS refunded, "
        "COALESCE(SUM(CASE WHEN status = 'completed' THEN total_micro END), 0) AS turnover "
        f"FROM orders WHERE {where} GROUP BY kind ORDER BY turnover DESC", args).fetchall()

    return {
        "period": period, "periods": {k: v[0] for k, v in PERIODS.items()}, "tz_hours": tz_hours,
        "created": t["created"] or 0, "done": t["done"] or 0, "refunded": t["refunded"] or 0,
        "turnover": t["turnover"], "profit": t["profit"], "topped": topped,
        "series": series, "chart": chart(series),
        "by_kind": [{**dict(r), "title": KIND_TITLES.get(r["kind"], r["kind"])} for r in by_kind],
    }


def chart(series: list[dict[str, Any]], w: int = 720, h: int = 260) -> dict[str, Any]:
    """Готовые SVG-пути: плавные линии (кривые Катмулла — Рома) и подписи осей."""
    pad_l, pad_r, pad_t, pad_b = 6, 10, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    peak = max([s["created"] for s in series] + [1])
    top = _nice(peak)
    n = len(series)

    def pts(key: str) -> list[tuple[float, float]]:
        if n == 1:
            return [(pad_l + iw / 2, pad_t + ih - series[0][key] / top * ih)]
        return [(pad_l + i * iw / (n - 1), pad_t + ih - s[key] / top * ih) for i, s in enumerate(series)]

    lines = {k: _smooth(pts(k)) for k in ("created", "done", "refunded")}
    base = pad_t + ih
    created = pts("created")
    area = f"{lines['created']} L{created[-1][0]:.1f},{base:.1f} L{created[0][0]:.1f},{base:.1f} Z" if n else ""
    grid = [{"y": pad_t + ih - i * ih / 4, "v": round(top * i / 4)} for i in range(5)]
    every = max(1, round(n / 6))
    ticks = [{"x": pad_l + (i * iw / (n - 1) if n > 1 else iw / 2), "t": s["label"]}
             for i, s in enumerate(series) if i % every == 0]
    return {"w": w, "h": h, "lines": lines, "area": area, "grid": grid, "ticks": ticks,
            "x0": pad_l, "x1": w - pad_r, "base": base}


def _nice(v: int) -> int:
    for step in (4, 8, 12, 20, 40, 60, 100, 200, 400, 1000, 2000, 4000, 10000):
        if v <= step:
            return step
    return (v // 10000 + 1) * 10000


def _smooth(p: list[tuple[float, float]]) -> str:
    if not p:
        return ""
    if len(p) == 1:
        x, y = p[0]
        return f"M{x - 4:.1f},{y:.1f} L{x + 4:.1f},{y:.1f}"
    d = f"M{p[0][0]:.1f},{p[0][1]:.1f}"
    for i in range(len(p) - 1):
        p0, p1, p2 = p[max(i - 1, 0)], p[i], p[i + 1]
        p3 = p[min(i + 2, len(p) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        # не даём кривой уйти ниже нуля между точками
        base = max(p1[1], p2[1])
        c1 = (c1[0], min(c1[1], base)) if c1[1] > base else c1
        c2 = (c2[0], min(c2[1], base)) if c2[1] > base else c2
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d
