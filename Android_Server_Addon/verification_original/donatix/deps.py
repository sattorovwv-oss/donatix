"""Общие зависимости FastAPI: база, текущий пользователь, CSRF, шаблоны."""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from typing import Any, Iterator

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from . import accounts, db
from .config import ROOT, Config
from .money import fmt, fmt_unit
from .suppliers import KIND_TITLES

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals.update(fmt=fmt, fmt_unit=fmt_unit, kind_titles=KIND_TITLES)


@pass_context
def _dt(ctx: Any, value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Время из базы (UTC) → местное время человека: {{ o.created_at | dt }}."""
    from .timez import local
    return local(value, ctx.get("tz"), fmt)


templates.env.filters["dt"] = _dt
templates.env.filters["fromjson"] = json.loads
templates.env.filters["faq_item"] = lambda qa: {
    "@type": "Question", "name": qa[0], "acceptedAnswer": {"@type": "Answer", "text": qa[1]}}


def _img(url):
    from .catalog_job import local_url
    return local_url(url)


templates.env.filters["img"] = _img


def _pack_title(name: str, kind: str = "topup") -> str:
    """Название пакета игры по-русски со значком; «Free Fire — 100 Diamonds» → «Free Fire — 💎 100 алмазов»."""
    from .packs import full
    if kind != "topup" or not name:
        return name
    head, sep, tail = str(name).rpartition(" — ")
    return f"{head}{sep}{full(tail)}" if sep else full(str(name))


templates.env.filters["pack_title"] = _pack_title


def _price(value, unit: str = "item") -> str:
    """Цена для витрины: у звёзд — как есть (доли цента), у остального — 4 знака, округление вверх,
    как при списании."""
    from decimal import ROUND_CEILING, Decimal
    d = Decimal(str(value))
    return str(d) if unit == "star" else str(d.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


templates.env.filters["price"] = _price


@pass_context
def _money(ctx, value, digits: int | None = None):
    """Сумма в валюте, которую выбрал клиент (USD или TJS). Внутри всё в долларах — это только показ.

    value — микро-доллары (int) или доллары строкой/Decimal. В сомони — по тому же курсу,
    по которому считаются оплаты; при наведении видна точная сумма в долларах.
    """
    from decimal import ROUND_HALF_UP, Decimal

    from markupsafe import Markup, escape
    usd = Decimal(int(value)) / 10_000 if isinstance(value, int) else Decimal(str(value or 0))
    usd_text = fmt(int(value)) if isinstance(value, int) else str(value)
    if ctx.get("cur_code") != "TJS" or not ctx.get("cur_rate"):
        return f"${usd_text}"
    tjs = usd * Decimal(str(ctx["cur_rate"]))
    places = Decimal("0.0001") if digits == 4 or (digits is None and 0 < abs(tjs) < 1) else Decimal("0.01")
    shown = tjs.quantize(places, rounding=ROUND_HALF_UP)
    return Markup(f'<span class="cur-tjs" title="${escape(usd_text)}">{shown} с.</span>')


templates.env.globals["money"] = _money


@pass_context
def _dprice(ctx, usd: float) -> str:
    """Цена D-коина (доли цента) в валюте клиента: 3 значащие цифры — коротко и видно каждое движение."""
    import math
    value, sign = float(usd or 0), "$"
    if ctx.get("cur_code") == "TJS" and ctx.get("cur_rate"):
        value, sign = value * float(ctx["cur_rate"]), " с."
    places = 4 if value <= 0 else max(2, min(12, 2 - math.floor(math.log10(value))))
    text = f"{value:.{places}f}"
    return f"${text}" if sign == "$" else f"{text}{sign}"


templates.env.globals["dprice"] = _dprice


def _asset_version() -> str:
    """Метка версии стилей: меняется с файлом, и браузер не держит старый CSS из кеша."""
    import hashlib

    h = hashlib.sha1()
    for name in ("donatix.css", "code.js", "logo.svg"):
        path = ROOT / "static" / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()

STATUS_TITLES = {
    "processing": "В обработке",
    "completed": "Выполнен",
    "failed": "Отменён, деньги возвращены",
    "attention": "Требует внимания",
    "pending": "Ждёт одобрения",
    "active": "Активен",
    "blocked": "Заблокирован",
}
templates.env.globals["status_titles"] = STATUS_TITLES


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    config = request.app.state.config
    pool = db.pool(config.db_path)
    conn = pool.acquire()
    try:
        from .sitecfg import refresh
        try:
            refresh(conn, config)   # настройки из админки — во всех процессах сайта
        except Exception:  # noqa: BLE001 — не мешаем запросу, попробуем в следующий раз
            logging.getLogger(__name__).exception("настройки: не удалось перечитать")
        yield conn
    finally:
        pool.release(conn)


def session_user(request: Request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = accounts.get_user(conn, int(user_id))
    sid = request.session.get("sid")
    live = sid and conn.execute("SELECT 1 FROM logins WHERE sid = ? AND user_id = ? AND ended_at IS NULL",
                                (sid, user["id"] if user else 0)).fetchone()
    if user is None or user["status"] == "blocked" or not live:
        request.session.clear()
        return None
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf", ""))
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        raise Forbidden("Форма устарела. Обновите страницу и попробуйте ещё раз.")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


#: Страницы для поисковиков (они же в sitemap.xml)
INDEXABLE = {"/": ("daily", "1.0"), "/docs": ("weekly", "0.8"), "/register": ("monthly", "0.5"),
              "/privacy": ("yearly", "0.2"), "/terms": ("yearly", "0.2")}


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200):
    ctx = dict(ctx or {})
    config: Config = request.app.state.config
    ref = request.query_params.get("ref")
    if ref:   # пришёл по реферальной ссылке — запомним до регистрации
        from .referrals import clean_code
        if clean_code(ref) and "user_id" not in request.session:
            request.session["ref"] = clean_code(ref)
    ctx.setdefault("user", None)
    ctx["site_name"] = config.site_name
    ctx["support_contact"] = config.support_contact
    ctx["tg_channel"] = config.tg_channel
    ctx["csrf"] = csrf_token(request)
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["path"] = request.url.path
    ctx["base_url"] = config.base_url
    ctx["google_verify"] = config.google_verify
    ctx["yandex_verify"] = config.yandex_verify
    ctx["google_login"] = bool(config.google_client_id and config.google_client_secret)
    ctx["canonical"] = config.base_url + request.url.path
    # В поиск попадают только публичные страницы; кабинет, админка и ошибки — нет
    ctx["noindex"] = status_code >= 400 or request.url.path not in INDEXABLE
    ctx["unread"] = 0
    ctx["low_balance_micro"] = 0
    ctx["cur_code"] = "TJS" if request.cookies.get("dx_cur") == "TJS" else "USD"
    ctx["cur_rate"] = None
    from . import timez
    ctx["tz"], ctx["tz_choice"] = timez.resolve(None, None, request.cookies.get("dx_tz"))
    if ctx.get("user") is not None:
        from .notify import unread_count
        pool = db.pool(config.db_path)
        c = pool.acquire()
        try:
            ctx["tz"], ctx["tz_choice"] = timez.resolve(c, ctx["user"], request.cookies.get("dx_tz"))
            ctx["unread"] = unread_count(c, ctx["user"]["id"])
            from .popular import services as popular_services
            ctx["popular"] = popular_services(c)
            low = db.get_setting(c, "pay.low_balance_usd") or str(config.low_balance_usd)
            ctx["low_balance_micro"] = int(float(low) * 10_000)
            if ctx["cur_code"] == "TJS":
                from .payments import settings as pay_settings
                ctx["cur_rate"] = pay_settings(c, config)["tjs_rate"]
        finally:
            pool.release(c)
    ctx["tz_label"] = timez.label(ctx["tz"])
    from datetime import datetime, timezone
    ctx["now_utc"] = datetime.now(timezone.utc)
    ctx["tz_zones"] = timez.ZONES
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
