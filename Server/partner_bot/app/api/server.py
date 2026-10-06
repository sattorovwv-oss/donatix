"""Сборка HTTP-приложения API.

Живёт на том же порту, что и приёмник вебхуков поставщика: второй порт
означал бы второй адрес, второй сертификат и вторую строчку в настройках
сервера — а пользы никакой.

Каждый запрос проходит один и тот же путь:

    номер запроса → журнал → разбор ключа → обработчик

Номер (X-Request-Id) выдаётся первым и возвращается в заголовке ответа:
когда разработчик напишет «у меня не работает», этот номер — единственный
способ найти его запрос в журнале, ничего у него не выспрашивая.

Чего в журнале нет и никогда не будет: самого ключа, заголовка
Authorization и тела запроса. Ключ в логах — это ключ, утёкший всем, у
кого есть доступ к логам.
"""
from __future__ import annotations

import logging
import secrets
import time

from aiohttp import web

from app import db, runtime
from app.api import cabinet, docs, guard, routes
from app.api.guard import Denied

log = logging.getLogger(__name__)

PREFIX = "/api/v1"

#: Точки, которые работают без ключа: документация и проверка живости.
#: Сверяем по самому обработчику, а не по концу адреса: товар с именем
#: «health» иначе открыл бы дыру там, где её никто не искал.
def _open(request: web.Request) -> bool:
    return request.match_info.handler in (health, docs.page,
                                          cabinet.page)


def _unrouted(request: web.Request) -> bool:
    """Адрес не нашёлся. Проверять ключ незачем — ответим 404."""
    route = getattr(request.match_info, "route", None)
    return getattr(route, "resource", None) is None


def enabled() -> bool:
    return runtime.get_bool("api_enabled")


def base_url() -> str:
    """Адрес, который владелец даёт разработчикам."""
    from app.services import webhook

    base = webhook.public_url().replace(webhook.PATH, "").rstrip("/")
    return f"{base}{PREFIX}" if base else ""


def client_ip(request: web.Request) -> str:
    """Адрес клиента. За обратным прокси настоящий адрес — в заголовке.

    Заголовку верим, только если владелец сам сказал, что бот стоит за
    прокси: иначе любой мог бы подделать X-Forwarded-For и обойти
    защиту от подбора ключа.
    """
    if runtime.get_bool("api_behind_proxy"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return (peer[0] if peer else "")[:45]


@web.middleware
async def middleware(request: web.Request, handler):
    """Номер запроса, журнал и время ответа — для всех точек сразу."""
    request_id = "req_" + secrets.token_hex(8)
    request["request_id"] = request_id
    started = time.monotonic()
    error = ""
    status = 500

    conn = await db.connect()
    request["conn"] = conn
    caller = None
    try:
        if not (_open(request) or _unrouted(request)):
            caller = await _authorize(request, conn)
            # Пропуск в кабинет умеет только смотреть. Проверяем здесь,
            # а не в каждой точке: забыть проверку в одной новой точке —
            # значит отдать право тратить деньги ссылке из браузера.
            if caller.watch and request.method != "GET":
                raise Denied(403, "read_only",
                             "Это ссылка на кабинет — ей можно только "
                             "смотреть. Для заказов нужен ключ sk_live_.")
        response = await handler(request)
        status = response.status
    except Denied as denied:
        error = denied.code
        status = denied.status
        response = routes.fail(denied.status, denied.code, denied.message)
        if denied.retry_after:
            response.headers["Retry-After"] = str(denied.retry_after)
    except web.HTTPException as exc:
        status = exc.status
        error = "http_error"
        response = routes.fail(
            exc.status,
            "not_found" if exc.status == 404 else "method_not_allowed",
            "Такой точки нет." if exc.status == 404
            else "Метод не поддерживается этой точкой.",
        )
    except Exception as exc:  # noqa: BLE001 — чужому коду нужен ответ, а не тишина
        log.exception("API %s: %s", request_id, exc)
        error = type(exc).__name__
        response = routes.fail(500, "internal_error",
                               "Внутренняя ошибка. Повторите позже.")
    finally:
        ms = int((time.monotonic() - started) * 1000)
        # Точка могла оставить транзакцию открытой (упавшая вставка,
        # ранний выход). Снимаем до журнала: иначе журнал закоммитит
        # чужие полузаписи вместе со своей строкой.
        if await db.release(conn):
            log.warning("API %s: точка оставила открытую транзакцию — снята",
                        request_id)
        try:
            await db.log_api_request(
                conn, request_id=request_id,
                key_id=(caller.key_id if caller else None),
                user_id=(caller.user_id if caller else None),
                method=request.method, path=request.path, status=status,
                error=error, ip=client_ip(request),
                user_agent=request.headers.get("User-Agent", ""), ms=ms,
            )
            if caller and caller.key_id:
                await db.note_api_use(conn, caller.key_id, client_ip(request))
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять ответ
            log.warning("API: запрос не записался в журнал — %s", exc)
        await conn.close()

    response.headers["X-Request-Id"] = request_id
    return response


async def _authorize(request: web.Request, conn) -> guard.Caller:
    if not enabled():
        raise Denied(503, "api_disabled", "API временно выключен.")
    caller = await guard.identify(
        conn, guard.bearer(request.headers.get("Authorization", "")),
        client_ip(request),
    )
    request["caller"] = caller
    return caller


async def health(request: web.Request) -> web.Response:
    return routes.ok(status="ok", version="v1")


def build(bot, provider) -> web.Application:
    """Собрать приложение API. Отдельное — чтобы его можно было
    примонтировать и к своему серверу, и к чужому."""
    app = web.Application(middlewares=[middleware],
                          client_max_size=routes.MAX_BODY)
    app["bot"] = bot
    app["provider"] = provider

    add = app.router.add_route
    add("GET", "/health", health)
    add("GET", "/docs", docs.page)
    add("GET", "/cabinet", cabinet.page)
    add("GET", "/products", routes.products)
    add("GET", "/products/{product_id}", routes.product)
    add("GET", "/games", routes.games)
    add("GET", "/balance", routes.balance)
    add("GET", "/user", routes.user)
    add("GET", "/orders", routes.orders)
    add("GET", "/orders/summary", routes.orders_summary)
    add("GET", "/transactions", routes.transactions)
    add("GET", "/order/status", routes.order_status)
    add("GET", "/check-id", routes.check_id)
    add("GET", "/orders/{order_id}", routes.order_by_id)
    add("POST", "/order/create", routes.order_create)
    add("POST", "/order/refund", routes.order_refund)
    return app


def mount(parent: web.Application, bot, provider) -> web.Application:
    """Повесить API на уже работающий сервер по адресу /api/v1."""
    api = build(bot, provider)
    parent.add_subapp(PREFIX, api)
    return api
