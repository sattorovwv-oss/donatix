"""Приём отчётов поставщика о статусе заказа.

Без этого игровой заказ узнаётся только опросом: бот ходит к поставщику
каждые несколько минут и всё это время клиент смотрит на «пополнение
идёт». Вебхук переворачивает схему — поставщик сам стучится, как только
заказ поменял статус, и клиент узнаёт о выдаче за секунду.

Опрос при этом никуда не девается: в документации поставщика сказано
прямо, что вебхук может не прийти, и тогда заказ зависнет навсегда.
Вебхук — ускорение, а не замена присмотру.

Три правила безопасности, все три обязательны:

  1. Подпись. Тело запроса подписано HMAC-SHA256 на секрете whsec_…
     Без проверки любой прохожий мог бы прислать «заказ выполнен» и
     получить товар бесплатно.
  2. Сравнение подписи — постоянное по времени (hmac.compare_digest).
     Обычное == выдаёт правильный префикс скоростью ответа.
  3. Защита от повторов. Поставщик может прислать одно событие дважды;
     event_id пишется в базу первичным ключом, и повтор отбрасывается.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
from hashlib import sha256

from aiogram import Bot
from aiohttp import web

from app import db, runtime
from app.config import settings

log = logging.getLogger(__name__)

#: Куда поставщик шлёт отчёты. Путь один и тот же у всех — секретность
#: держится на подписи, а не на том, что адрес трудно угадать.
PATH = "/webhook/fazer"

#: Где может лежать подпись: заголовок называют по-разному.
SIGNATURE_HEADERS = (
    "X-Signature", "X-Webhook-Signature", "X-Fazer-Signature",
    "X-Hub-Signature-256", "X-Signature-256", "Signature",
)

#: Больше этого тело быть не может — отсекаем мусор до разбора.
MAX_BODY = 64 * 1024


def secret() -> str:
    """Секрет вебхука. Панель важнее .env: его можно сменить на ходу."""
    return (runtime.get("fazer_webhook_secret")
            or settings.fazer_webhook_secret or "").strip()


def public_url() -> str:
    """Адрес, который владелец вставляет в кабинет поставщика."""
    base = (runtime.get("webhook_public_url")
            or settings.webhook_public_url or "").strip().rstrip("/")
    return f"{base}{PATH}" if base else ""


def _candidates(raw: str) -> list[str]:
    """Из заголовка достаём саму подпись: её пишут с приставкой и без."""
    value = (raw or "").strip()
    if not value:
        return []
    out = [value]
    for part in value.replace(",", " ").split():
        if "=" in part:
            out.append(part.split("=", 1)[1])
        else:
            out.append(part)
    return out


def verify(body: bytes, signature: str, key: str = "") -> bool:
    """Подписано ли тело нашим секретом.

    Сравниваем и с шестнадцатеричной записью, и с base64: сервисы пишут
    подпись то так, то так, а ошибиться здесь — значит отвергать
    настоящие отчёты.
    """
    key = (key or secret()).strip()
    if not key or not signature:
        return False

    digest = hmac.new(key.encode(), body, sha256).digest()
    # Сравниваем байтами: compare_digest на строках падает, если в
    # заголовке пришло что-то не из латиницы, — а прислать туда могут
    # что угодно, и падать на этом приёмник не должен.
    expected = [digest.hex().encode(), base64.b64encode(digest)]

    for candidate in _candidates(signature):
        raw = candidate.encode("utf-8", "ignore")
        for good in expected:
            if hmac.compare_digest(raw, good):
                return True
        # Иногда подпись шлют в base64 от hex-строки — принимаем и это.
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
        if hmac.compare_digest(decoded, digest):
            return True
    return False


def _signature_of(request: web.Request) -> str:
    for name in SIGNATURE_HEADERS:
        value = request.headers.get(name)
        if value:
            return value
    return ""


async def apply_event(bot: Bot, conn, provider, payload: dict) -> str:
    """Разобрать событие и закрыть заказ. Возвращает, что произошло."""
    from app.services import games as gsvc

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event_id = str(payload.get("event_id") or "").strip()
    kind = str(payload.get("event") or "").strip()
    external = str(data.get("order_id") or payload.get("order_id") or "").strip()
    status = str(data.get("status") or payload.get("status") or "").strip().lower()

    if not external:
        return "без номера заказа"

    # Повтор отбрасываем до всякой работы: событие без event_id считаем
    # одноразовым и пропускаем к обработке — потерять выдачу хуже.
    if event_id and not await db.remember_event(
        conn, event_id, kind=kind, order_id=external, status=status
    ):
        log.info("Вебхук: событие %s уже приходило", event_id)
        return "повтор"

    order = await db.find_order_by_external(conn, external)
    if order is None:
        log.info("Вебхук: заказ %s не наш", external)
        return "чужой заказ"
    if order.status in (db.ORDER_DELIVERED, db.ORDER_REFUNDED):
        return "заказ уже закрыт"

    # Дальше решает общая логика игр: она умеет и выдать, и вернуть
    # деньги, и написать клиенту. Свой разбор статусов здесь развёл бы
    # два разных мнения об одном и том же.
    if status in gsvc.DONE or status in gsvc.FAILED:
        result = await gsvc.check(bot, conn, provider, order)
        log.info("Вебхук: заказ %s (%s) → %s", order.id, status, result)
        return result
    return "ещё в работе"


async def handle(request: web.Request) -> web.Response:
    body = await request.content.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        return web.json_response({"ok": False, "error": "too big"}, status=413)

    if not verify(body, _signature_of(request)):
        log.warning("Вебхук: подпись не сошлась, отчёт отброшен")
        return web.json_response({"ok": False, "error": "bad signature"},
                                 status=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    app = request.app
    conn = await db.connect()
    try:
        from app.services import suppliers

        result = await apply_event(
            app["bot"], conn, suppliers.for_games(app["provider"]), payload
        )
    except Exception as exc:  # noqa: BLE001 — ответить надо в любом случае
        log.exception("Вебхук: обработка сорвалась — %s", exc)
        # 500 говорит поставщику «пришли ещё раз»: событие не потеряется.
        return web.json_response({"ok": False}, status=500)
    finally:
        await conn.close()

    return web.json_response({"ok": True, "result": result})


async def _root(request: web.Request) -> web.Response:
    """Что показать тому, кто открыл просто адрес, без пути.

    Пустая страница выглядит как поломка, а открывают корень чаще, чем
    кажется: адрес передают из рук в руки без пути. Поэтому уводим на
    документацию — единственное, что тут интересно постороннему.
    """
    from app.api import server as api_server

    if api_server.enabled():
        raise web.HTTPFound(f"{api_server.PREFIX}/docs")
    return web.Response(
        text="Здесь нет страниц. Это служебный адрес бота.\n",
        content_type="text/plain", charset="utf-8", status=404,
    )


async def serve(bot: Bot, provider) -> web.AppRunner | None:
    """Поднять HTTP-сервер. None — он никому не нужен и не запущен.

    На одном порту живут две разные вещи: приёмник отчётов поставщика и
    API для сторонних разработчиков. Каждая включается сама по себе —
    можно открыть API, не заводя вебхук поставщика, и наоборот.
    """
    from app.api import server as api_server

    port = int(runtime.get_int("webhook_port") or settings.webhook_port or 0)
    if port <= 0:
        return None

    take_hooks = bool(secret())
    take_api = api_server.enabled()
    if not take_hooks and not take_api:
        log.warning("Порт задан, но включать нечего: нет ни секрета вебхука, "
                    "ни включённого API. Сервер не запущен.")
        return None

    app = web.Application(client_max_size=MAX_BODY)
    app["bot"] = bot
    app["provider"] = provider
    app.router.add_get("/healthz", lambda _: web.json_response({"ok": True}))
    app.router.add_get("/", _root)
    if take_hooks:
        app.router.add_post(PATH, handle)
    if take_api:
        api_server.mount(app, bot, provider)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, port)
    await site.start()
    log.info("✅ HTTP слушает %s:%s", settings.webhook_host, port)
    if take_hooks:
        log.info("   Отчёты поставщика: %s", public_url() or PATH)
    if take_api:
        log.info("   API разработчикам: %s",
                 api_server.base_url() or api_server.PREFIX)
    return runner
