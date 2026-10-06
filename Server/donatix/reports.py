"""Утренний отчёт в админ-бот: как прошёл вчерашний день — без захода в админку."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .money import fmt

REPORT_HOUR = 9  # по местному времени (config.tz_offset)


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    return int(conn.execute(sql, args).fetchone()[0] or 0)


def daily_text(conn: sqlite3.Connection, config: Config, day: datetime) -> str:
    """Сводка за местные сутки `day` (дата по tz_offset)."""
    tz = timezone(timedelta(hours=config.tz_offset))
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    a, b = start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
    rng = "created_at >= ? AND created_at < ?"
    n = _one(conn, f"SELECT COUNT(*) FROM orders WHERE status = 'completed' AND {rng}", (a, b))
    revenue = _one(conn, f"SELECT SUM(total_micro) FROM orders WHERE status = 'completed' AND {rng}", (a, b))
    profit = _one(conn, f"SELECT SUM(total_micro - cost_micro) FROM orders WHERE status = 'completed' AND {rng}",
                  (a, b))
    failed = _one(conn, f"SELECT COUNT(*) FROM orders WHERE status IN ('failed', 'attention') AND {rng}", (a, b))
    paid_n = _one(conn, "SELECT COUNT(*) FROM payments WHERE status = 'paid' AND resolved_at >= ? AND resolved_at < ?",
                  (a, b))
    paid = _one(conn, "SELECT SUM(amount_micro) FROM payments WHERE status = 'paid' AND resolved_at >= ? "
                      "AND resolved_at < ?", (a, b))
    new_users = _one(conn, f"SELECT COUNT(*) FROM users WHERE role = 'client' AND {rng}", (a, b))
    pending = _one(conn, "SELECT COUNT(*) FROM payments WHERE status = 'pending'")
    bots_on = _one(conn, "SELECT COUNT(*) FROM bots WHERE enabled = 1")
    warned = _one(conn, "SELECT COUNT(*) FROM bots WHERE enabled = 1 AND warn_count > 0")
    shop_n = _one(conn, f"SELECT COUNT(*) FROM orders WHERE source = 'shopbot' AND status = 'completed' AND {rng}",
                  (a, b))
    shop_sum = _one(conn, f"SELECT SUM(total_micro) FROM orders WHERE source = 'shopbot' AND status = 'completed' "
                          f"AND {rng}", (a, b))
    supplier = db.get_setting(conn, "supplier_balance")
    from . import traffic
    visitors = traffic.visitors_between(conn, a, b)
    return (f"🌅 <b>Отчёт за {day.strftime('%d.%m.%Y')}</b>\n\n"
            f"👀 Посетителей сайта: <b>{visitors}</b>\n"
            f"🛒 Заказов выполнено: <b>{n}</b>" + (f" · проблемных: {failed}" if failed else "") + "\n"
            + (f"🛍 Из них через бот-магазин: <b>{shop_n}</b> на ${fmt(shop_sum)}\n" if shop_n else "") +
            f"💵 Выручка: <b>${fmt(revenue)}</b>\n"
            f"📈 Прибыль: <b>${fmt(profit)}</b>\n"
            f"💳 Пополнений: <b>{paid_n}</b> на ${fmt(paid)}"
            + (f" · ждут проверки: {pending}" if pending else "") + "\n"
            f"👤 Новых клиентов: <b>{new_users}</b>\n"
            f"🤖 Ботов работает: <b>{bots_on}</b>" + (f" · с предупреждением: {warned}" if warned else "") + "\n"
            f"🏦 Баланс FazerCards: <b>{'$' + supplier if supplier else '—'}</b>")


def maybe_send(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> bool:
    """Раз в день после 9:00 по местному времени — отчёт за вчера. True — отправлен."""
    from .worker import notify_admin
    if (db.get_setting(conn, "report.daily_on") or "1") != "1":
        return False
    local = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=config.tz_offset)))
    today = local.strftime("%Y-%m-%d")
    if local.hour < REPORT_HOUR or db.get_setting(conn, "report.last_day") == today:
        return False
    db.set_setting(conn, "report.last_day", today)
    notify_admin(config, daily_text(conn, config, local - timedelta(days=1)), html=True)
    return True
