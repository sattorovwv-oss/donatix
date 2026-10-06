"""Push-уведомления на телефон и компьютер (Web Push): приложение-APK (TWA) и Chrome — даже когда сайт закрыт.

Ключи VAPID сайт создаёт сам при первом запуске и хранит в базе (settings), настраивать ничего не нужно.
Подписка — у каждого устройства своя (таблица push_subs). Устройство отписалось или удалило приложение —
сервис push ответит 404/410, и подписку удаляем.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
from typing import Any

from . import db
from .config import Config

log = logging.getLogger(__name__)

_lock = threading.Lock()


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keys(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """(закрытый ключ PEM, открытый ключ для браузера). Нет библиотеки — None."""
    if not available():
        return None
    with _lock:
        private = db.get_setting(conn, "push.vapid_private")
        public = db.get_setting(conn, "push.vapid_public")
        if private and public:
            return private, public
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        public = _b64(key.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint))
        db.set_setting(conn, "push.vapid_private", private)
        db.set_setting(conn, "push.vapid_public", public)
        return private, public


def subscribe(conn: sqlite3.Connection, user_id: int, endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth:
        raise ValueError("Неверная подписка")
    conn.execute(
        "INSERT INTO push_subs (user_id, endpoint, p256dh, auth, ua, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
        "auth = excluded.auth, ua = excluded.ua",
        (user_id, endpoint, p256dh[:200], auth[:100], ua[:200], db.now()))


def unsubscribe(conn: sqlite3.Connection, user_id: int, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subs WHERE user_id = ? AND endpoint = ?", (user_id, endpoint))


def count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM push_subs WHERE user_id = ?", (user_id,)).fetchone()[0]


def title_for(text: str) -> str:
    """Короткий заголовок по тексту уведомления — первое, что человек видит на экране блокировки."""
    t = text.lower()
    if "выполнен" in t and "не выполнен" not in t:
        return "✅ Заказ выполнен"
    if "не выполнен" in t or "вернул" in t or "возвращ" in t:
        return "↩️ Деньги вернулись на баланс"
    if "отклон" in t:
        return "❌ Заявка отклонена"
    if "пополнен" in t or "зачисл" in t:
        return "💰 Баланс пополнен"
    return "🔔 Donatix"


def send(conn: sqlite3.Connection, config: Config, user_id: int, text: str, link: str = "") -> int:
    """Всем устройствам клиента — в фоне, сайт не ждёт. Вернёт, на сколько устройств отправляем."""
    subs = [dict(r) for r in conn.execute("SELECT endpoint, p256dh, auth FROM push_subs WHERE user_id = ?",
                                          (user_id,)).fetchall()]
    if not subs:
        return 0
    pair = keys(conn)
    if pair is None:
        return 0
    payload = json.dumps({"title": title_for(text), "body": text[:240], "url": link or "/panel/notifications",
                          "tag": f"dx-{user_id}-{abs(hash(text)) % 10_000}"}, ensure_ascii=False)
    threading.Thread(target=_deliver, args=(config, pair[0], subs, payload), name="donatix-push", daemon=True).start()
    return len(subs)


def _deliver(config: Config, private_pem: str, subs: list[dict[str, Any]], payload: str) -> None:
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    vapid = Vapid.from_pem(private_pem.encode())
    contact = f"mailto:{config.admin_email}" if config.admin_email else (config.base_url or "https://donatix.tj")
    dead: list[str] = []
    for s in subs:
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}, payload,
                    vapid_private_key=vapid, vapid_claims={"sub": contact}, ttl=86400, timeout=10)
            # vapid_claims — новый словарь на каждое устройство: библиотека дописывает в него адрес push-сервиса
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", 0) if exc.response is not None else 0
            if status in (404, 410):   # устройство отписалось или удалило приложение
                dead.append(s["endpoint"])
            else:
                log.info("push: %s %s", status, str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 — уведомление не должно ломать ничего вокруг
            log.info("push: не отправлено: %s", exc)
    if dead:
        conn = db.connect(config.db_path)
        try:
            conn.executemany("DELETE FROM push_subs WHERE endpoint = ?", [(e,) for e in dead])
        finally:
            conn.close()
