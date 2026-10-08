"""Сборка приложения: `python -m donatix serve` или `uvicorn donatix.app:create_app --factory`."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import accounts, admin, api, db, web
from .config import ROOT, Config
from .deps import Forbidden, LoginRequired, render
from .ratelimit import RateLimiter
from .suppliers import Supplier, make_supplier
from .worker import Worker

log = logging.getLogger(__name__)


SLOW_MS = 1500


class _SelectiveGZip:
    """GZip для страниц и API, но не для картинок: JPEG/PNG/WebP уже сжаты — только трата процессора."""

    def __init__(self, app, **kw):
        self.plain = app
        self.gzip = GZipMiddleware(app, **kw)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/media/"):
            await self.plain(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


def factory() -> FastAPI:
    """Для запуска в нескольких процессах: каждый процесс сам собирает приложение из .env."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()
    return create_app()


def quiet_http_logs() -> None:
    """httpx пишет каждый запрос с полным адресом — а в адресе Telegram API токен бота.
    Токены не должны попадать в журнал сервера: оставляем только предупреждения и ошибки."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class _CookieJar:
    """Ответ глазами traffic.track: статус, тип и куда дописать cookie — без лишних обёрток Starlette."""

    def __init__(self, status: int, raw: list):
        self.status_code = status
        self._raw = raw
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw}

    def set_cookie(self, key: str, value: str, max_age: int, httponly: bool = True, samesite: str = "lax",
                   secure: bool = False) -> None:
        cookie = f"{key}={value}; Max-Age={max_age}; Path=/; SameSite={samesite}"
        cookie += "; HttpOnly" if httponly else ""
        cookie += "; Secure" if secure else ""
        self._raw.append((b"set-cookie", cookie.encode("latin-1")))


class _Traffic:
    """Учёт просмотров страниц. Чистый ASGI: API, картинки и админку пропускает сразу, без затрат."""

    def __init__(self, app, db_path):
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope, receive, send):
        from . import traffic
        prefetch = any(k == b"sec-purpose" and b"prefetch" in v for k, v in scope.get("headers") or [])
        if (scope["type"] != "http" or scope.get("method") != "GET" or prefetch   # предзагрузка Chrome — не визит
                or scope.get("path", "").startswith(traffic.SKIP_PREFIXES)):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                try:
                    raw = list(message.get("headers") or [])
                    jar = _CookieJar(message["status"], raw)
                    session = scope.get("session") or {}
                    traffic.track(Request(scope), jar, session.get("user_id"))
                    message = {**message, "headers": raw}
                except Exception:  # noqa: BLE001 — статистика не должна ломать страницу
                    log.exception("посещаемость: не удалось записать просмотр")
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if time.monotonic() - traffic._last_flush >= 2:
            try:
                await run_in_threadpool(traffic.flush, self.db_path)
            except Exception:  # noqa: BLE001
                log.exception("посещаемость: запись в базу")


class _CacheAndTiming:
    """Cache-Control по адресу + журнал медленных запросов. Чистый ASGI — без лишних задач на каждый запрос."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", cache_policy(path, query, message["status"]).encode()))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = (time.monotonic() - started) * 1000
            if ms >= SLOW_MS:   # journalctl -u donatix | grep медленно
                log.warning("медленно: %s %s — %.0f мс", scope.get("method"), path, ms)


def cache_policy(path: str, query: str, status: int) -> str:
    """Как долго браузер и прокси держат ответ у себя."""
    if status >= 400:
        return "no-store"
    if path.startswith("/static/"):
        # Файлы с ?v=хеш меняют адрес при каждом изменении — можно хранить год
        return "public, max-age=31536000, immutable" if "v=" in query else "public, max-age=86400"
    if path.startswith("/media/"):
        return "public, max-age=2592000, stale-while-revalidate=86400"
    if path in ("/robots.txt", "/sitemap.xml"):
        return "public, max-age=3600"
    if path.startswith(("/panel", "/admin", "/api", "/login", "/register", "/logout")):
        return "private, no-store"
    # Публичные страницы: всегда сверяться с сервером (шапка зависит от входа)
    return "private, no-cache"


def create_app(config: Config | None = None, supplier: Supplier | None = None) -> FastAPI:
    config = config or Config.from_env()
    supplier = supplier or make_supplier(config)
    from . import account_check
    account_check.configure(config)   # FlashTopup: ник игрока по ID, если ключи есть в .env
    db.init(config.db_path)
    conn = db.connect(config.db_path)
    try:
        accounts.ensure_admin(conn, config)
        from . import sitecfg
        sitecfg.load(conn, config)  # наценки и прочее, что админ поменял в админке
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import threading

        from . import leader
        started: list = []
        stop_event = threading.Event()

        def start_background() -> None:
            # Только в «ведущем» процессе: заказы, боты, загрузка каталога — ровно по одному экземпляру
            log.info("фоновые задачи: этот процесс ведущий (pid %s)", os.getpid())
            from . import catalog
            catalog.SYNC_LOCK.bind(config)
            bg = Worker(config, supplier)
            bg.start()
            started.append(bg)
            if config.alert_telegram_token and config.alert_telegram_chat_id:
                from .tgbot import AdminBot
                bot = AdminBot(config, supplier=supplier)
                bot.start()
                started.append(bot)
            if config.support_bot_token and config.openai_api_key:
                from .supportbot import SupportBot
                support = SupportBot(config)
                support.start()
                started.append(support)
            if config.shop_bot_token:
                from .shopbot import ShopBot
                shop = ShopBot(config, supplier)
                shop.start()
                started.append(shop)
            if config.run_bots:
                from . import bots
                runner = bots.RUNNER = bots.BotRunner(config, config.internal_url)
                runner.start()
                started.append(runner)

        if config.run_worker:
            from . import catalog
            catalog.SYNC_LOCK.bind(config)
            leader.become_leader(config, start_background, stop_event)
        yield
        stop_event.set()
        for part in reversed(started):
            part.stop()
        leader.release("leader")

    app = FastAPI(
        title=f"{config.site_name} API",
        version="1",
        lifespan=lifespan,
        docs_url="/api/swagger",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.config = config
    app.state.supplier = supplier
    app.state.limiter = RateLimiter()

    # Посещаемость. Добавлена ДО SessionMiddleware — значит, внутри неё и видит сессию (кто вошёл)
    app.add_middleware(_Traffic, db_path=config.db_path)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="dx_session",
        max_age=14 * 24 * 3600,
        same_site="lax",
        https_only=config.cookie_secure,
    )
    app.add_middleware(_CacheAndTiming)
    app.add_middleware(_SelectiveGZip, minimum_size=800, compresslevel=5)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    # Картинки каталога, скачанные с поставщика к себе (админка → Загрузка каталога)
    from . import catalog_job
    catalog_job.load_image_index(config)
    app.mount("/media", StaticFiles(directory=str(catalog_job.images_dir(config))), name="media")
    app.include_router(api.router)
    from . import compat
    app.include_router(compat.router)
    app.include_router(web.router)
    app.include_router(admin.router)

    @app.exception_handler(api.ApiError)
    async def _api_error(request: Request, exc: api.ApiError):
        return api.api_error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        message = f"Неверный запрос: {where} — {first.get('msg', '')}".strip(" —")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": message, "code": "validation_error"}, status_code=400)
        return render(request, "error.html", {"message": message}, 400)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return render(request, "error.html", {"message": str(exc)}, 403)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            code = "not_found" if exc.status_code == 404 else "http_error"
            return JSONResponse({"ok": False, "error": str(exc.detail), "code": code}, status_code=exc.status_code)
        message = "Страница не найдена." if exc.status_code == 404 else str(exc.detail)
        return render(request, "error.html", {"message": message}, exc.status_code)

    return app
