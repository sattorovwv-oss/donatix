"""Реферальная программа.

У каждого клиента своя ссылка (…/register?ref=КОД). Кто зарегистрировался по ней —
закрепляется за пригласившим навсегда. С каждого ВЫПОЛНЕННОГО заказа приглашённого
пригласившему автоматически начисляется бонус на баланс: процент от нашей прибыли с
этого заказа (цена продажи минус закупка). Поэтому бонус никогда не уводит сайт в
минус. Процент задаёт админ (Настройки), 0 — программа выключена. Начисление за один
заказ — ровно один раз.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from typing import Any

from . import accounts, db
from .money import fmt

log = logging.getLogger(__name__)

DEFAULT_PERCENT = 10   # от НАШЕЙ прибыли: при наценке 8% это 0.8% от заказа, вам остаётся 7.2%
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_RE = re.compile(r"^[a-z0-9]{4,32}$")


def percent(conn: sqlite3.Connection) -> int:
    raw = db.get_setting(conn, "referral.percent")
    try:
        return max(0, min(50, int(raw))) if raw not in (None, "") else DEFAULT_PERCENT
    except ValueError:
        return DEFAULT_PERCENT


def code_for(conn: sqlite3.Connection, user_id: int) -> str:
    """Код клиента для ссылки; создаётся при первом обращении."""
    row = conn.execute("SELECT ref_code FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["ref_code"]:
        return row["ref_code"]
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        try:
            conn.execute("UPDATE users SET ref_code = ? WHERE id = ? AND ref_code IS NULL", (code, user_id))
        except sqlite3.IntegrityError:
            continue
        row = conn.execute("SELECT ref_code FROM users WHERE id = ?", (user_id,)).fetchone()
        if row and row["ref_code"]:
            return row["ref_code"]
    raise RuntimeError("не удалось создать реферальный код")


def clean_code(raw: Any) -> str | None:
    code = str(raw or "").strip().lower()
    return code if CODE_RE.match(code) else None


def attach(conn: sqlite3.Connection, new_user_id: int, code: str | None) -> bool:
    """Закрепить нового клиента за пригласившим. Себя пригласить нельзя, перезакрепить — тоже."""
    code = clean_code(code)
    if not code:
        return False
    owner = conn.execute("SELECT id FROM users WHERE ref_code = ? AND role = 'client'", (code,)).fetchone()
    if owner is None or owner["id"] == new_user_id:
        return False
    return bool(conn.execute("UPDATE users SET referred_by = ? WHERE id = ? AND referred_by IS NULL",
                             (owner["id"], new_user_id)).rowcount)


def award(conn: sqlite3.Connection, order_id: int) -> int:
    """Начислить бонус пригласившему за выполненный заказ. Возвращает сумму (микро) или 0."""
    pct = percent(conn)
    if pct <= 0:
        return 0
    row = conn.execute(
        "SELECT o.id, o.public_id, o.user_id, o.total_micro, o.cost_micro, o.status, u.referred_by, u.login "
        "FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?", (order_id,)).fetchone()
    if row is None or row["status"] != "completed" or not row["referred_by"]:
        return 0
    profit = int(row["total_micro"]) - int(row["cost_micro"])
    reward = profit * pct // 100
    if reward <= 0:
        return 0
    try:
        with db.tx(conn):
            conn.execute("INSERT INTO referral_rewards (order_id, referrer_id, referred_id, amount_micro, created_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (order_id, row["referred_by"], row["user_id"], reward, db.now()))
            accounts.post_ledger(conn, row["referred_by"], reward,
                                 f"Реферальный бонус: заказ {row['public_id']} клиента {row['login']}")
    except sqlite3.IntegrityError:
        return 0   # за этот заказ уже начисляли
    try:
        from .notify import notify
        notify(conn, None, row["referred_by"],
               f"🎁 Реферальный бонус ${fmt(reward)}: ваш приглашённый {row['login']} сделал заказ.",
               "/panel/referrals")
    except Exception:  # noqa: BLE001 — уведомление не должно мешать начислению
        log.exception("реферал: уведомление")
    return reward


def stats(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    invited = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)).fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN users u ON u.id = o.user_id "
        "WHERE u.referred_by = ? AND o.status = 'completed'", (user_id,)).fetchone()[0]
    earned = conn.execute("SELECT COALESCE(SUM(amount_micro), 0) FROM referral_rewards WHERE referrer_id = ?",
                          (user_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT r.amount_micro, r.created_at, u.login, o.public_id FROM referral_rewards r "
        "JOIN users u ON u.id = r.referred_id JOIN orders o ON o.id = r.order_id "
        "WHERE r.referrer_id = ? ORDER BY r.id DESC LIMIT 30", (user_id,)).fetchall()
    friends = conn.execute(
        "SELECT login, created_at FROM users WHERE referred_by = ? ORDER BY id DESC LIMIT 30", (user_id,)).fetchall()
    return {"invited": invited, "active": active, "earned": int(earned), "rows": rows, "friends": friends}
