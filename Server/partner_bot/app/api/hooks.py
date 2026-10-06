"""Вебхуки клиентам API: «ваш заказ выполнен».

Без них разработчику остаётся только опрашивать /order/status в цикле —
и чем больше у него заказов, тем чаще он это делает. Вебхук переворачивает
схему: мы стучимся сами, как только статус сменился.

Что здесь важно и почему:

  1. Подпись. Тело подписано HMAC-SHA256 на секрете клиента и лежит в
     заголовке X-Signature. Без неё любой, кто узнает адрес, мог бы
     прислать «заказ выполнен» и получить товар бесплатно.

  2. Запрет внутренних адресов. Клиент задаёт адрес сам, а наш сервер
     стоит в той же сети, что и база с кошельком. Без проверки клиент
     мог бы указать http://127.0.0.1:8080 или адрес облачных метаданных
     и заставить наш сервер сходить туда за нас — это SSRF. Поэтому
     адрес проверяется дважды: при сохранении и перед каждой отправкой,
     уже после разбора имени в IP (имя может начать указывать на 127.0.0.1
     позже — тогда проверки при сохранении не хватило бы).

  3. Состояние отправки живёт в базе, а не в памяти: перезапуск бота не
     должен терять «этому ещё не сообщили».
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from hashlib import sha256
from urllib.parse import urlparse

import aiohttp

from app import db

log = logging.getLogger(__name__)

#: Ждать ответа дольше нет смысла: это не наша работа, а уведомление.
TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

#: Сколько раз пробуем, прежде чем оставить заказ в покое. Дальше клиент
#: всё равно узнает статус через /order/status.
MAX_TRIES = 8

#: Как часто фоновая задача смотрит, кому ещё не сообщили.
EVERY = 3.0

#: Статусы заказа у нас — и как они называются в API.
PUBLIC = {
    db.ORDER_DELIVERING: "processing",
    db.ORDER_DELIVERED: "completed",
    db.ORDER_FAILED: "processing",     # деньги held, разбирается владелец
    db.ORDER_REFUNDED: "refunded",
}


def new_secret() -> str:
    return "whsec_" + secrets.token_hex(24)


def sign(body: bytes, secret: str, stamp: str = "") -> str:
    """Подпись уведомления.

    Подписываем время вместе с телом. Без времени подпись остаётся верной
    навсегда: кто однажды перехватил запрос, может повторять его сколько
    угодно, и чужой сервер будет считать, что заказ выполнился снова.
    """
    signed = (stamp.encode() + b"." + body) if stamp else body
    return hmac.new(secret.encode(), signed, sha256).hexdigest()


def private_host(host: str) -> bool:
    """Ведёт ли имя на внутренний адрес. Да — значит адрес запрещён.

    Разбираем имя сами и проверяем каждый полученный адрес: у имени их
    бывает несколько, и хватит одного внутреннего, чтобы запрос ушёл
    внутрь нашей сети.
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True    # не разобралось — не ходим

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def check_url(url: str) -> str:
    """Пусто — адрес годится. Иначе строка с причиной отказа."""
    url = (url or "").strip()
    if not url:
        return "адрес пустой"
    if len(url) > 400:
        return "адрес слишком длинный"

    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "адрес должен начинаться с https://"
    if not parsed.hostname:
        return "в адресе нет имени сервера"
    if parsed.port and parsed.port not in (443, 8443):
        return "порт должен быть 443 или 8443"
    if private_host(parsed.hostname):
        return "адрес ведёт внутрь сети — такие мы не вызываем"
    return ""


def payload_of(row: dict) -> dict:
    """Что уходит клиенту. Ничего лишнего: ни ключа, ни себестоимости."""
    status = PUBLIC.get(row["status"], "processing")
    body = {
        "event": f"order.{status}",
        "order_id": row["ref"],
        "status": status,
        "product_id": row["product_id"],
        "quantity": row.get("quantity", 1),
        "amount": row.get("price", 0),
        "customer": row.get("customer") or "",
        "result": row.get("recipient") or "",
    }
    if status == "refunded":
        body["reason"] = (row.get("error") or "")[:300]
    return body


async def deliver(session: aiohttp.ClientSession, conn, row: dict) -> bool:
    """Отправить одно уведомление. True — клиент ответил, что принял."""
    hook = await db.get_api_hook(conn, row["user_id"])
    if not hook or not hook["enabled"] or not hook["url"]:
        # Адреса нет — сообщать некуда. Помечаем как отправленное, иначе
        # заказ вечно висел бы в очереди и мешал остальным.
        await db.note_api_hook(conn, row["ref"], sent=row["status"])
        return True

    problem = check_url(hook["url"])
    if problem:
        await db.note_api_hook_result(conn, row["user_id"], False, problem)
        await db.note_api_hook(conn, row["ref"], failed=True)
        return False

    body = json.dumps(payload_of(row), ensure_ascii=False).encode()
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": stamp,
        "X-Signature": sign(body, hook["secret"], stamp),
        "X-Signature-Algorithm": "hmac-sha256",
        "X-Order-Id": row["ref"],
        "User-Agent": "StarsBot-Webhook/1",
    }
    try:
        async with session.post(hook["url"], data=body, headers=headers,
                                timeout=TIMEOUT, allow_redirects=False) as resp:
            ok = 200 <= resp.status < 300
            note = "" if ok else f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001 — чужой сервер падает как хочет
        ok, note = False, f"{type(exc).__name__}: {exc}"[:190]

    await db.note_api_hook_result(conn, row["user_id"], ok, note)
    if ok:
        await db.note_api_hook(conn, row["ref"], sent=row["status"])
    else:
        await db.note_api_hook(conn, row["ref"], failed=True)
        log.info("Вебхук клиента %s: %s", row["user_id"], note)
    return ok


async def flush(conn, session: aiohttp.ClientSession | None = None) -> int:
    """Разослать всё, о чём ещё не сообщили. Возвращает сколько ушло."""
    rows = await db.api_orders_to_notify(conn)
    if not rows:
        return 0

    own = session is None
    session = session or aiohttp.ClientSession()
    sent = 0
    try:
        for row in rows:
            if await deliver(session, conn, row):
                sent += 1
    finally:
        if own:
            await session.close()
    return sent


async def loop(conn) -> None:
    """Фоновая рассылка. Одна задача на бота, живёт всё время его работы.

    Опрос базы вместо вызова из каждого места, где меняется статус:
    статус меняют пять разных путей (выдача, возврат, вебхук поставщика,
    присмотр по таймауту, руки владельца), и забыть один из них — значит
    молча не сообщить клиенту о заказе.
    """
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await flush(conn, session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — рассылка не должна умирать
                log.warning("Рассылка вебхуков сорвалась: %s", exc)
            await asyncio.sleep(EVERY)
