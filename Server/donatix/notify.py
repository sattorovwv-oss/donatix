"""Уведомления клиентам: в кабинете (колокольчик) и, если настроена почта, на email."""

from __future__ import annotations

import logging
import smtplib
import sqlite3
import threading
from email.message import EmailMessage

from . import db
from .config import Config

log = logging.getLogger(__name__)


def notify(conn: sqlite3.Connection, config: Config | None, user_id: int, text: str, link: str = "") -> None:
    conn.execute(
        "INSERT INTO notifications (user_id, text, link, created_at) VALUES (?, ?, ?, ?)",
        (user_id, text[:500], link or None, db.now()),
    )
    if config:
        _push_to_bots(conn, config, user_id, text, link)
        try:   # на телефон/компьютер, даже если сайт закрыт
            from . import webpush
            webpush.send(conn, config, user_id, text, link)
        except Exception:  # noqa: BLE001 — уведомление не должно мешать зачислению или заказу
            log.exception("push-уведомление")
    if config and config.smtp_host:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            threading.Thread(target=_send_email, args=(config, row["email"], text, link), daemon=True).start()


def _push_to_bots(conn: sqlite3.Connection, config: Config, user_id: int, text: str, link: str) -> None:
    """Клиент работает через бота из конструктора — пишем ему в Telegram через его же бота.

    В кабинет сайта владелец бота может и не заходить: без этого он не узнал бы,
    что пополнение зачислено или отклонено.
    """
    from .security import unseal
    rows = conn.execute("SELECT token_enc, admin_ids FROM bots WHERE user_id = ? AND enabled = 1",
                        (user_id,)).fetchall()
    for row in rows:
        token = unseal(config.secret_key, row["token_enc"])
        chats = [c for c in str(row["admin_ids"] or "").replace(" ", "").split(",") if c.lstrip("-").isdigit()]
        if token and chats:
            body = f"🔔 <b>{config.site_name}</b>\n{_esc(text)}" + (f"\n{config.base_url}{link}" if link else "")
            threading.Thread(target=_send_telegram, args=(token, chats, body), daemon=True).start()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send_telegram(token: str, chats: list[str], text: str) -> None:
    import httpx
    for chat in chats:
        try:
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15,
                       json={"chat_id": int(chat), "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": True})
        except httpx.HTTPError as exc:
            log.warning("уведомление в бота %s: %s", chat, exc)


def _send_email(config: Config, to: str, text: str, link: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"{config.site_name}: {text[:80]}"
    msg["From"] = config.smtp_from or config.smtp_user
    msg["To"] = to
    body = text + (f"\n\n{config.base_url}{link}" if link else "") + f"\n\n— {config.site_name}"
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as s:
            s.starttls()
            if config.smtp_user:
                s.login(config.smtp_user, config.smtp_password)
            s.send_message(msg)
    except Exception as exc:  # почта не должна ломать работу сайта
        log.warning("письмо на %s не отправлено: %s", to, exc)


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)
    ).fetchone()[0]


def latest(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()


def mark_read(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (db.now(), user_id))
