"""Неактивные боты партнёров: предупредить трижды, потом отключить.

Каждый бот — отдельная программа на сервере: память и запросы к поставщику.
Бот без продаж просто занимает место. Алгоритм:

1. Нет выполненных продаж N дней (по умолчанию 7) с создания бота, последней
   продажи или последнего включения — партнёр получает предупреждение в своём
   боте и в кабинете: «1 из 3».
2. Следующее — не раньше чем через M дней (по умолчанию 2), если продаж так и нет.
3. После третьего предупреждения и ещё M дней без продаж бот отключается.
   Не удаляется: партнёр включает его в кабинете, и отсчёт начинается заново.

Любая продажа сбрасывает предупреждения. Боты админа не трогаем.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .config import Config

log = logging.getLogger(__name__)

DEFAULTS = {"bots.watch_on": "1", "bots.inactive_days": "7", "bots.warn_every_days": "2", "bots.warnings": "3"}


def _int(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(db.get_setting(conn, key) or DEFAULTS[key])
    except ValueError:
        return int(DEFAULTS[key])


def rules(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"on": (db.get_setting(conn, "bots.watch_on") or "1") == "1",
            "days": max(1, _int(conn, "bots.inactive_days")),
            "every": max(1, _int(conn, "bots.warn_every_days")),
            "warnings": max(1, _int(conn, "bots.warnings"))}


def _dt(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_sale(conn: sqlite3.Connection, api_key_id: int | None) -> datetime | None:
    if not api_key_id:
        return None
    row = conn.execute("SELECT MAX(created_at) FROM orders WHERE api_key_id = ? AND status = 'completed'",
                       (api_key_id,)).fetchone()
    return _dt(row[0]) if row else None


def _tell(conn: sqlite3.Connection, config: Config, bot: sqlite3.Row, text: str) -> None:
    """В сам бот (всем его админам) и в колокольчик кабинета — без дубля в другие боты партнёра."""
    import threading

    from .notify import _send_telegram
    from .security import unseal
    token = unseal(config.secret_key, bot["token_enc"])
    chats = [c for c in str(bot["admin_ids"] or "").replace(" ", "").split(",") if c.lstrip("-").isdigit()]
    if token and chats:
        threading.Thread(target=_send_telegram, args=(token, chats, text), daemon=True).start()
    conn.execute("INSERT INTO notifications (user_id, text, link, created_at) VALUES (?, ?, ?, ?)",
                 (bot["user_id"], text.replace("<b>", "").replace("</b>", "")[:500], "/panel/bots", db.now()))


def check(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> dict[str, int]:
    """Пройтись по включённым ботам. Возвращает, сколько предупреждено и отключено."""
    from .worker import notify_admin
    r = rules(conn)
    stats = {"warned": 0, "disabled": 0, "reset": 0}
    if not r["on"]:
        return stats
    now = now or datetime.now(timezone.utc)
    bots = conn.execute(
        "SELECT b.*, u.role, u.login FROM bots b JOIN users u ON u.id = b.user_id WHERE b.enabled = 1").fetchall()
    for b in bots:
        if b["role"] == "admin":
            continue
        sale = last_sale(conn, b["api_key_id"])
        start = max(filter(None, [_dt(b["active_since"]), _dt(b["created_at"]), sale]))
        if sale and b["warn_count"] and sale > (_dt(b["last_warn_at"]) or start):
            conn.execute("UPDATE bots SET warn_count = 0, last_warn_at = NULL WHERE id = ?", (b["id"],))
            stats["reset"] += 1
            continue
        if now - start < timedelta(days=r["days"]):
            continue
        last_warn = _dt(b["last_warn_at"])
        if last_warn and now - last_warn < timedelta(days=r["every"]):
            continue
        idle = (now - start).days
        if b["warn_count"] < r["warnings"]:
            n = b["warn_count"] + 1
            left = (r["warnings"] - n + 1) * r["every"]
            conn.execute("UPDATE bots SET warn_count = ?, last_warn_at = ? WHERE id = ?", (n, now.isoformat(), b["id"]))
            _tell(conn, config, b,
                  f"⚠️ <b>Предупреждение {n} из {r['warnings']}</b> · @{b['username']}\n"
                  f"В боте нет продаж уже {idle} дн. Если продаж не будет, бот отключится автоматически "
                  f"примерно через {left} дн. Любая продажа снимает предупреждения.")
            stats["warned"] += 1
        else:
            conn.execute("UPDATE bots SET enabled = 0, disabled_reason = 'inactive', updated_at = ? WHERE id = ?",
                         (db.now(), b["id"]))
            from .bots import block_user
            block_user(conn, b["user_id"])   # новый бот вместо отключённого — тоже только через админа
            _tell(conn, config, b,
                  f"⛔️ <b>Бот @{b['username']} отключён</b> — {idle} дн. без продаж после "
                  f"{r['warnings']} предупреждений. Включить снова может только администратор — напишите в поддержку.")
            notify_admin(config, f"⛔️ Бот @{b['username']} (клиент {b['login']}) отключён автоматически: "
                                 f"{idle} дн. без продаж.")
            stats["disabled"] += 1
    if stats["disabled"]:
        from . import bots as bots_mod
        if bots_mod.RUNNER:
            bots_mod.RUNNER.poke()
    return stats


def mark_enabled(conn: sqlite3.Connection, bot_id: int) -> None:
    """Бот включили (снова) — отсчёт без продаж начинается заново."""
    conn.execute("UPDATE bots SET active_since = ?, warn_count = 0, last_warn_at = NULL, disabled_reason = NULL "
                 "WHERE id = ?", (db.now(), bot_id))
