"""Клиенты, API-ключи и журнал движения денег."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import db
from .config import TIERS, Config
from .money import MoneyError, to_decimal
from .security import hash_api_key, hash_password, new_api_key, new_webhook_secret, seal, unseal, verify_password

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class AccountError(ValueError):
    pass


class InsufficientBalance(Exception):
    def __init__(self, need: int, have: int):
        super().__init__("недостаточно средств")
        self.need = need
        self.have = have


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    login: str,
    password: str,
    role: str = "client",
    status: str = "pending",
    project: str = "",
) -> int:
    email = email.strip().lower()
    login = login.strip()
    if not EMAIL_RE.match(email):
        raise AccountError("Неверный email.")
    if not LOGIN_RE.match(login):
        raise AccountError("Имя пользователя: 3–32 символа, латиница, цифры, _ . -")
    if len(password) < 8:
        raise AccountError("Пароль — минимум 8 символов.")
    exists = conn.execute(
        "SELECT email, login FROM users WHERE email = ? OR lower(login) = lower(?)", (email, login)
    ).fetchone()
    if exists:
        raise AccountError("Такой email или имя уже заняты.")
    cur = conn.execute(
        "INSERT INTO users (email, login, password_hash, role, status, created_at, webhook_secret, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (email, login, hash_password(password), role, status, db.now(), new_webhook_secret(),
         project.strip()[:300] or None),
    )
    return int(cur.lastrowid)


def authenticate(conn: sqlite3.Connection, email_or_login: str, password: str) -> sqlite3.Row | None:
    ident = email_or_login.strip()
    user = conn.execute(
        "SELECT * FROM users WHERE email = lower(?) OR lower(login) = lower(?)", (ident, ident)
    ).fetchone()
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    return user


def ensure_admin(conn: sqlite3.Connection, config: Config) -> None:
    """Создать первого админа из .env, если админов ещё нет."""
    if not (config.admin_email and config.admin_password):
        return
    if conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        return
    login = config.admin_email.split("@")[0][:32] or "admin"
    if not LOGIN_RE.match(login):
        login = "admin"
    create_user(conn, email=config.admin_email, login=login, password=config.admin_password,
                role="admin", status="active")


def markup_for(user: sqlite3.Row, config: Config, kind: str = "") -> Decimal:
    """Наценка клиента: для вида товара (если задана в настройках) → личная → по уровню.

    Отдельная наценка на вид товара (Telegram Stars, Premium, Steam) действует на весь проект —
    для всех клиентов, в том числе с личной наценкой."""
    if kind in config.kind_markups:
        return config.kind_markups[kind]
    if user["markup_override"] not in (None, ""):
        return to_decimal(user["markup_override"])
    return config.markups.get(user["tier"], config.markups["bronze"])


def update_user_admin(
    conn: sqlite3.Connection, user_id: int, *, status: str, tier: str, markup_override: str
) -> None:
    if status not in ("pending", "active", "blocked"):
        raise AccountError("Неверный статус.")
    if tier not in TIERS:
        raise AccountError("Неверный уровень.")
    override = markup_override.strip().replace(",", ".")
    if override:
        try:
            value = to_decimal(override)
        except MoneyError as exc:
            raise AccountError("Наценка — число в процентах.") from exc
        if not (Decimal("-50") <= value <= Decimal("500")):
            raise AccountError("Наценка вне разумных пределов.")
    conn.execute(
        "UPDATE users SET status = ?, tier = ?, markup_override = ? WHERE id = ?",
        (status, tier, override or None, user_id),
    )


def set_webhook(conn: sqlite3.Connection, user_id: int, url: str) -> None:
    url = url.strip()
    if url and not re.match(r"^https?://[^\s]+$", url):
        raise AccountError("Адрес webhook должен начинаться с http:// или https://")
    from .webhooks import public_target
    if url and not public_target(url):
        raise AccountError("Адрес webhook должен быть доступен из интернета (не локальный и не внутренний).")
    conn.execute("UPDATE users SET webhook_url = ? WHERE id = ?", (url or None, user_id))


def rotate_webhook_secret(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET webhook_secret = ? WHERE id = ?", (new_webhook_secret(), user_id))


# ── API-ключи ────────────────────────────────────────────────


def create_api_key(conn: sqlite3.Connection, user_id: int, name: str, secret: str = "") -> str:
    active = conn.execute(
        "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND revoked_at IS NULL", (user_id,)
    ).fetchone()[0]
    if active >= 10:
        raise AccountError("Не больше 10 активных ключей. Отзовите ненужные.")
    key, prefix, key_hash = new_api_key()
    conn.execute(
        "INSERT INTO api_keys (user_id, name, prefix, key_hash, key_enc, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, (name.strip() or "Ключ")[:64], prefix, key_hash, seal(secret, key) if secret else None, db.now()),
    )
    return key


def reveal_api_key(conn: sqlite3.Connection, user_id: int, key_id: int, secret: str) -> str | None:
    row = conn.execute("SELECT key_enc FROM api_keys WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                       (key_id, user_id)).fetchone()
    if row is None or not row["key_enc"]:
        return None
    return unseal(secret, row["key_enc"])


def revoke_api_key(conn: sqlite3.Connection, user_id: int, key_id: int) -> None:
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (db.now(), key_id, user_id),
    )


def user_by_api_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT u.*, k.id AS api_key_id, k.last_used_at AS key_used_at FROM api_keys k "
        "JOIN users u ON u.id = k.user_id WHERE k.key_hash = ? AND k.revoked_at IS NULL",
        (hash_api_key(key),),
    ).fetchone()
    if row is not None:
        # Боты зовут API много раз в минуту — отметку «был активен» пишем не чаще раза в минуту,
        # иначе каждый запрос превращается в две записи на диск и очередь к базе.
        ts = db.now()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ACTIVITY_EVERY)).strftime("%Y-%m-%dT%H:%M:%S")
        if (row["key_used_at"] or "") < cutoff or (row["last_active_at"] or "") < cutoff:
            with db.tx(conn):
                conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (ts, row["api_key_id"]))
                conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (ts, row["id"]))
    return row


ACTIVITY_EVERY = 60


def api_key_problem(conn: sqlite3.Connection, key: str) -> str:
    """Почему ключ не подошёл — понятно для человека, который его вставлял."""
    from .security import API_KEY_PREFIX
    key = key.strip().strip('"\'')
    if key.endswith(("…", "...")) or (key.startswith(API_KEY_PREFIX) and len(key) < 30):
        return ("Передано только начало ключа. Полный ключ — около 50 символов: в кабинете «API» нажмите "
                "«Показать» и «Скопировать».")
    if not key.startswith(API_KEY_PREFIX):
        return f"Это не ключ {API_KEY_PREFIX}… Скопируйте ключ в кабинете, раздел «API»."
    revoked = conn.execute("SELECT 1 FROM api_keys WHERE key_hash = ? AND revoked_at IS NOT NULL",
                           (hash_api_key(key),)).fetchone()
    if revoked:
        return "Этот ключ отозван. Возьмите действующий в кабинете, раздел «API»."
    return "Неверный API-ключ. Скопируйте его заново в кабинете, раздел «API» (кнопка «Скопировать»)."


# ── Журнал денег ─────────────────────────────────────────────


def post_ledger(
    conn: sqlite3.Connection,
    user_id: int,
    amount_micro: int,
    note: str,
    *,
    order_id: int | None = None,
    created_by: int | None = None,
    allow_negative: bool = False,
) -> int:
    """Изменить баланс и записать в журнал. Вызывать ВНУТРИ db.tx()."""
    before = conn.execute("SELECT balance_micro FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    after = before + amount_micro
    if after < 0 and not allow_negative:
        raise InsufficientBalance(need=-amount_micro, have=before)
    conn.execute("UPDATE users SET balance_micro = ? WHERE id = ?", (after, user_id))
    cur = conn.execute(
        "INSERT INTO transactions (user_id, type, amount_micro, balance_before, balance_after, note, "
        "order_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, "credit" if amount_micro >= 0 else "debit", amount_micro, before, after, note,
         order_id, created_by, db.now()),
    )
    if amount_micro < 0:
        _warn_low_balance(conn, user_id, before, after)
    return int(cur.lastrowid)


def _warn_low_balance(conn: sqlite3.Connection, user_id: int, before: int, after: int) -> None:
    """Клиенту — одно уведомление, когда баланс опустился ниже порога из админки."""
    raw = db.get_setting(conn, "pay.low_balance_usd")
    threshold = int(Decimal(raw) * 10_000) if raw else 100_000
    if threshold and before >= threshold > after:
        from .money import fmt
        from .notify import notify
        text = f"На балансе осталось ${fmt(after)} — пополните счёт, чтобы заказы не остановились."
        notify(conn, None, user_id, text, "/panel/balance")


def tx_public_id(tx_id: int) -> str:
    return f"tx{tx_id}"


def parse_tx_id(public_id: str) -> int | None:
    m = re.fullmatch(r"tx(\d+)", public_id.strip())
    return int(m.group(1)) if m else None
