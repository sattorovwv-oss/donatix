"""Уведомления клиентам о смене статуса заказа (POST на их адрес с подписью)."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from .orders import public_view
from .security import sign_webhook

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
TIMEOUT = httpx.Timeout(8, connect=4)
BUDGET_SECONDS = 60   # за один проход — не дольше: остальные доставим в следующий


def public_target(url: str) -> bool:
    """Адрес клиента — в интернете, а не внутри нашего сервера (127.0.0.1, 10.x, 169.254.x…).
    Иначе через webhook можно было бы стучаться во внутренние службы сервера (SSRF)."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (OSError, ValueError, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def build(order: sqlite3.Row) -> bytes:
    body = {"event": "order.updated", "order": public_view(order)}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def deliver_pending(conn: sqlite3.Connection, client: httpx.Client | None = None, limit: int = 20) -> int:
    rows = conn.execute(
        "SELECT o.*, u.webhook_url, u.webhook_secret FROM orders o JOIN users u ON u.id = o.user_id "
        "WHERE o.webhook_state = 'pending' ORDER BY o.updated_at LIMIT ?",
        (limit,),
    ).fetchall()
    own = client is None   # свой клиент — настоящая сеть: проверяем адрес; переданный (тесты) — как есть
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    sent = 0
    started = time.monotonic()
    try:
        for row in rows:
            if time.monotonic() - started > BUDGET_SECONDS:
                break
            if not row["webhook_url"] or (own and not public_target(row["webhook_url"])):
                conn.execute("UPDATE orders SET webhook_state = 'none' WHERE id = ?", (row["id"],))
                continue
            body = build(row)
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-Donatix-Timestamp": ts,
                "X-Donatix-Signature": sign_webhook(row["webhook_secret"] or "", ts, body),
            }
            ok = False
            try:
                resp = client.post(row["webhook_url"], content=body, headers=headers)
                ok = 200 <= resp.status_code < 300
            except httpx.HTTPError as exc:
                log.info("webhook %s: %s", row["public_id"], exc)
            attempts = row["webhook_attempts"] + 1
            state = "sent" if ok else ("failed" if attempts >= MAX_ATTEMPTS else "pending")
            conn.execute(
                "UPDATE orders SET webhook_state = ?, webhook_attempts = ? WHERE id = ?",
                (state, attempts, row["id"]),
            )
            sent += ok
    finally:
        if own:
            client.close()
    return sent
