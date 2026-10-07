"""Native-client endpoints. All money and fulfillment stay in existing services."""
from __future__ import annotations

import hashlib
import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import accounts, catalog, db, orders
from .api import ApiError, OrderIn, _limit, api_user, expected_price
from .deps import csrf_token, get_config, get_conn, session_user
from .money import fmt

router = APIRouter(prefix="/mobile", tags=["Native app"])


class PushIn(BaseModel):
    device_id: str = Field(min_length=32, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    token: str = Field(min_length=20, max_length=4096)
    platform: Literal['android', 'ios'] = 'android'


class PushRemove(BaseModel):
    device_id: str = Field(min_length=32, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


@router.get('/push/status')
def push_status(user=Depends(api_user)):
    from . import native_push
    return {'ok':True,'configured':native_push.enabled()}


@router.post('/push/register')
def push_register(body:PushIn,request:Request,user=Depends(api_user),conn=Depends(get_conn),config=Depends(get_config)):
    from . import native_push
    _limit(request,'account',str(user['id']))
    if not request.session.get('sid') or request.session.get('user_id') != user['id']:
        raise ApiError('Войдите в мобильное приложение.', 'session_required',403)
    native_push.register(conn,config,user['id'],request.session['sid'],body.device_id,body.token,body.platform)
    return {'ok':True,'configured':native_push.enabled(),
            'binding':native_push.binding(config,request.session['sid'],user['id'],body.device_id)}


@router.post('/push/unregister')
def push_unregister(body:PushRemove,user=Depends(api_user),conn=Depends(get_conn)):
    from . import native_push
    native_push.unregister(conn,user['id'],body.device_id)
    return {'ok':True}


@router.get("/public/categories")
def public_categories(request: Request, kind: str = "", q: str = "", conn=Depends(get_conn), config=Depends(get_config)):
    from .api import mobile_categories
    from .web import _guest
    _limit(request, "catalog", request.client.host if request.client else "guest")
    return mobile_categories(request, kind, q[:100], _guest(), conn, config)


@router.get("/public/products")
def public_products(request: Request, kind: str = "", category_id: str = "", q: str = "", limit: int = 100,
                    offset: int = 0, conn=Depends(get_conn), config=Depends(get_config)):
    from .api import products
    from .web import _guest
    _limit(request, "catalog", request.client.host if request.client else "guest")
    return products(request, kind, category_id, q[:100], limit, offset, _guest(), conn, config)


@router.get("/public/products/{product_id}")
def public_product(product_id: str, request: Request, conn=Depends(get_conn), config=Depends(get_config)):
    from .api import product
    from .web import _guest
    return product(product_id, request, _guest(), conn, config)


@router.get("/public/steam-gifts/games")
def public_gift_games(request: Request, q: str = "", limit: int = 30, conn=Depends(get_conn)):
    from .api import steam_gift_games
    from .web import _guest
    return steam_gift_games(request, q[:100], limit, _guest(), conn)


@router.get("/public/steam-gifts/games/{appid}")
def public_gift_game(appid: int, request: Request, conn=Depends(get_conn), config=Depends(get_config)):
    from .api import steam_gift_game
    from .web import _guest
    return steam_gift_game(appid, request, _guest(), conn, config)


@router.get("/config")
def configuration(conn=Depends(get_conn), config=Depends(get_config)):
    from . import google_auth, sitecfg, apple_auth
    return {"ok": True, "site_name": config.site_name,
            "google_enabled": google_auth.enabled(config),
            "apple_enabled": apple_auth.enabled(),
            "registration_open": sitecfg.registration_open(conn),
            "support_contact": config.support_contact, "tg_channel": config.tg_channel,
            "version": 4, "platforms": ["android", "ios"],
            "features": ["account_deletion", "native_fcm", "ios_apns", "apple_login", "price_confirmation", "cart_recovery"]}


@router.post("/notifications/read")
def notifications_read(request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    from . import notify
    notify.mark_read(conn, user["id"])
    return {"ok": True}


@router.get("/stats")
def stats(request: Request, period: str = "30d", tz_offset: int = 5,
          user=Depends(api_user), conn=Depends(get_conn)):
    from . import analytics, timez
    from datetime import datetime, timezone
    _limit(request, "account", str(user["id"]))
    choice = timez.user_choice(conn, user["id"])
    if choice != "auto":
        tz_offset = int(datetime.now(timezone.utc).astimezone(timez.zone(choice)).utcoffset().total_seconds() // 3600)
    a = analytics.build(conn, period, user_id=user["id"], tz_hours=max(-12, min(14, tz_offset)))
    # Purchase costs and supplier profit are admin-only, including on native devices.
    a.pop("profit", None)
    for k in ("turnover", "topped"):
        a[k] = fmt(a[k])
    for row in a["by_kind"]:
        row["turnover"] = fmt(row["turnover"])
    return {"ok": True, "analytics": a}


@router.get("/timezone")
def timezone_options(user=Depends(api_user), conn=Depends(get_conn)):
    from . import timez
    return {"ok": True, "choice": timez.user_choice(conn, user["id"]), "zones": timez.ZONES}


class TimezoneIn(BaseModel):
    timezone: str = Field(max_length=64)


@router.post("/timezone")
def timezone_save(body: TimezoneIn, user=Depends(api_user), conn=Depends(get_conn)):
    from . import timez
    if body.timezone != "auto" and not timez.valid(body.timezone):
        raise ApiError("Неизвестный часовой пояс.", "invalid_timezone")
    timez.set_user_choice(conn, user["id"], body.timezone)
    return {"ok": True}


@router.get("/dcoin")
def dcoin_overview(user=Depends(api_user), conn=Depends(get_conn)):
    from . import dcoin
    d = dcoin.summary(conn, user["id"])
    public = {k: d[k] for k in ("balance_text", "waiting_text", "free", "price", "change", "per_usd",
                                "fee_pct", "open", "opens", "enabled")}
    public["worth_usd"] = fmt(d["worth_micro"])
    public["free_text"] = dcoin.fmt_d(d["free"])
    return {"ok": True, "summary": public, "history": dcoin.history(conn, user["id"], 100),
            "top": dcoin.top(conn), "days": dcoin.days(conn), "timeframes": list(dcoin.TIMEFRAMES)}


@router.get("/dcoin/chart")
def dcoin_chart(tf: str = "5s", n: int = 300, user=Depends(api_user), conn=Depends(get_conn)):
    from . import dcoin
    return {"ok": True, **dcoin.candles(conn, tf, min(300, max(12, n)))}


class AmountIn(BaseModel):
    amount: str = Field(max_length=32)


@router.post("/dcoin/exchange")
def dcoin_exchange(body: AmountIn, request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    from . import dcoin
    _limit(request, "orders", str(user["id"]))
    if not dcoin.enabled(conn):
        raise ApiError("D-коины отключены.", "disabled")
    try:
        raw = body.amount.replace(" ", "").replace(",", ".")
        if raw.lower() in ("all", "все", "всё"):
            units = dcoin.summary(conn, user["id"])["free"]
        else:
            amount = Decimal(raw)
            if not amount.is_finite() or amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
                raise ValueError
            units = int(amount * 100)
        credited = dcoin.exchange(conn, user["id"], units)
    except (ValueError, InvalidOperation, OverflowError):
        raise ApiError("Введите положительное число D-коинов, до двух знаков после точки.", "invalid_amount") from None
    except dcoin.ExchangeError as exc:
        raise ApiError(str(exc), "exchange_rejected") from None
    return {"ok": True, "credited_usd": fmt(credited), "coins": dcoin.fmt_d(units)}


@router.get("/bots")
def bot_list(user=Depends(api_user), conn=Depends(get_conn)):
    from . import bots, sitecfg
    keys = ("id", "username", "admin_ids", "enabled", "running", "warn_count", "disabled_reason", "conflict", "created_at")
    return {"ok": True, "items": [{k: b.get(k) for k in keys} for b in bots.listing(conn, user["id"])],
            "eligibility": bots.eligibility(conn, user), "max_bots": sitecfg.max_bots(conn),
            "ready": bots.runner_alive(conn) and bots.TEMPLATE_DIR.exists() and sitecfg.client_bots_enabled(conn)}


class BotIn(BaseModel):
    token: str = Field(max_length=256)
    admin_ids: str = Field(max_length=300)


@router.post("/bots")
def bot_create(body: BotIn, request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    from . import bots
    from .worker import notify_admin
    _limit(request, "orders", str(user["id"]))
    elig = bots.eligibility(conn, user)
    if not elig["ok"]:
        raise ApiError(elig["reason"], "bot_ineligible", 403)
    try:
        username = bots.check_token(body.token)
        bot_id = bots.create(conn, config, user_id=user["id"], token=body.token,
                             admin_ids=body.admin_ids, username=username)
    except bots.BotError as exc:
        raise ApiError(str(exc), "invalid_bot") from None
    if bots.RUNNER:
        bots.RUNNER.poke()
    notify_admin(config, f"🤖 Клиент {user['login']} подключил бота @{username} в конструкторе.")
    return {"ok": True, "id": bot_id, "username": username}


class BotActionIn(BaseModel):
    admin_ids: str = Field(default="", max_length=300)


@router.post("/bots/{bot_id}/{action}")
def bot_action(bot_id: int, action: str, body: BotActionIn, user=Depends(api_user), conn=Depends(get_conn)):
    from . import bots
    if not bots.owned(conn, bot_id, user["id"]):
        raise ApiError("Бот не найден.", "not_found", 404)
    try:
        if action == "stop":
            bots.set_enabled(conn, bot_id, False)
        elif action in ("start", "restart"):
            ok, why = bots.can_enable(conn, bot_id)
            if not ok:
                raise ApiError(why, "bot_disabled", 403)
            bots.set_enabled(conn, bot_id, True)
        elif action == "admins":
            bots.update_admins(conn, bot_id, body.admin_ids)
        elif action == "delete":
            bots.delete(conn, bot_id)
        else:
            raise ApiError("Неизвестное действие.", "invalid_action")
    except bots.BotError as exc:
        raise ApiError(str(exc), "invalid_bot") from None
    if bots.RUNNER:
        bots.RUNNER.poke()
    return {"ok": True}


@router.get("/keys")
def key_list(user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    rows = conn.execute("SELECT id,name,prefix,created_at,last_used_at,revoked_at,key_enc IS NOT NULL AS can_show "
                        "FROM api_keys WHERE user_id=? ORDER BY revoked_at IS NOT NULL,id DESC", (user["id"],)).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows], "webhook_url": user["webhook_url"] or "",
            "webhook_secret": user["webhook_secret"], "base_url": config.base_url, "active": user["status"] == "active"}


class KeyIn(BaseModel):
    name: str = Field(default="", max_length=64)


@router.post("/keys")
def key_create(body: KeyIn, request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    _limit(request, "account", str(user["id"]))
    try:
        key = accounts.create_api_key(conn, user["id"], body.name, config.secret_key)
    except accounts.AccountError as exc:
        raise ApiError(str(exc), "key_rejected") from None
    return {"ok": True, "key": key}


@router.post("/keys/{key_id}/{action}")
def key_action(key_id: int, action: str, request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    _limit(request, "account", str(user["id"]))
    row = conn.execute("SELECT id FROM api_keys WHERE id=? AND user_id=?", (key_id, user["id"])).fetchone()
    if row is None:
        raise ApiError("Ключ не найден.", "not_found", 404)
    if action == "revoke":
        accounts.revoke_api_key(conn, user["id"], key_id)
        return {"ok": True}
    if action in ("reveal", "test"):
        key = accounts.reveal_api_key(conn, user["id"], key_id, config.secret_key)
        if key is None:
            raise ApiError("Этот ключ нельзя показать. Создайте новый.", "not_found", 404)
        if action == "test":
            test_user = accounts.user_by_api_key(conn, key)
            return {"ok": True, "valid": test_user is not None and test_user["status"] == "active"}
        return {"ok": True, "key": key}
    raise ApiError("Неизвестное действие.", "invalid_action")


class WebhookIn(BaseModel):
    url: str = Field(default="", max_length=2048)
    rotate: bool = False


@router.post("/webhook")
def webhook_save(body: WebhookIn, user=Depends(api_user), conn=Depends(get_conn)):
    try:
        accounts.set_webhook(conn, user["id"], body.url)
        if body.rotate:
            accounts.rotate_webhook_secret(conn, user["id"])
    except accounts.AccountError as exc:
        raise ApiError(str(exc), "invalid_webhook") from None
    return {"ok": True}


@router.post("/support/link")
def support_link(request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    from . import supportbot
    _limit(request, "account", str(user["id"]))
    code = supportbot.make_link_code(conn, user["id"])
    bot = supportbot.bot_username(conn, config)
    return {"ok": True, "code": f"DX-{code}" if code else "", "bot": bot,
            "url": f"https://t.me/{bot}?start={urlquote(code)}" if code and bot else "",
            "minutes": supportbot.LINK_TTL // 60}


@router.post("/support/code")
def support_code(request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    from . import supportbot
    _limit(request, "account", str(user["id"]))
    supportbot.send_code_notification(conn, config, user["id"])
    return {"ok": True}


@router.post("/payments/{payment_id}/{action}")
def payment_action(payment_id: int, action: str, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    from . import payments
    row = conn.execute("SELECT id FROM payments WHERE id=? AND user_id=?", (payment_id, user["id"])).fetchone()
    if row is None:
        raise ApiError("Заявка не найдена.", "not_found", 404)
    try:
        if action == "boost":
            payments.boost(conn, config, user["id"], payment_id)
        elif action == "cancel":
            if not payments.cancel(conn, user["id"], payment_id, config):
                raise ApiError("Чек уже отправлен — заявку нельзя отменить.", "cannot_cancel", 409)
        else:
            raise ApiError("Неизвестное действие.", "invalid_action")
    except payments.PaymentError as exc:
        raise ApiError(str(exc), "payment_rejected") from None
    return {"ok": True}


@router.get("/gamekeys/{product_id}/regions")
def key_regions(product_id: str, request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    from . import gamekeys
    _limit(request, "catalog", str(user["id"]))
    p = catalog.get_product(conn, product_id)
    if not p or p["kind"] != "game_key":
        raise ApiError("Ключ не найден.", "not_found", 404)
    return {"ok": True, **gamekeys.regions(request.app.state.supplier, str(p["supplier_ref"].get("game_id", "")))}


@router.post("/orders/quote")
def order_quote(body: OrderIn, request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    _limit(request, "status", str(user["id"]))
    p = catalog.get_product(conn, body.product_id)
    if not p:
        raise ApiError("Товар недоступен.", "not_found", 404)
    try:
        qty = orders._clean_quantity(p, body.quantity)
        fields = orders._clean_fields(p, body.fields)
        if p["kind"] == "steam_gift":
            from . import steam_gifts
            _, units = steam_gifts.resolve(request.app.state.supplier, fields)
        else:
            units = orders._units(p, qty, fields)
        total = orders.quote(config, user, p, units)["total_micro"]
    except orders.OrderError as exc:
        raise ApiError(str(exc), exc.code, exc.http_status) from None
    except Exception as exc:
        from .suppliers import SupplierError
        if isinstance(exc, SupplierError):
            raise ApiError("Поставщик не ответил. Попробуйте ещё раз.", "supplier_unavailable", 503) from None
        raise
    return {"ok": True, "total_usd": fmt(total), "units": str(units), "fields": fields,
            "balance_usd": fmt(user["balance_micro"])}


# Browser OAuth handoff, bound to an app-held verifier. No cookies or tokens in URLs.
def _oauth_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS mobile_oauth (id TEXT PRIMARY KEY, challenge TEXT NOT NULL, "
                 "expires INTEGER NOT NULL, user_id INTEGER)")
    conn.execute("DELETE FROM mobile_oauth WHERE expires < ?", (int(time.time()),))


class OAuthPrepare(BaseModel):
    challenge: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.post("/oauth/prepare")
def oauth_prepare(body: OAuthPrepare, request: Request, conn=Depends(get_conn), config=Depends(get_config)):
    from . import google_auth
    _limit(request, "login", request.client.host if request.client else "?")
    if not google_auth.enabled(config):
        raise ApiError("Вход через Google не настроен на сервере.", "disabled", 503)
    _oauth_table(conn)
    ticket = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO mobile_oauth (id,challenge,expires) VALUES (?,?,?)",
                 (ticket, body.challenge, int(time.time()) + 600))
    return {"ok": True, "ticket": ticket, "url": config.base_url + "/api/v1/mobile/oauth/start/" + ticket}


@router.get("/oauth/start/{ticket}")
def oauth_start(ticket: str, request: Request, conn=Depends(get_conn)):
    _oauth_table(conn)
    if not conn.execute("SELECT 1 FROM mobile_oauth WHERE id=?", (ticket,)).fetchone():
        raise ApiError("Ссылка входа устарела.", "expired", 410)
    request.session["next"] = "/api/v1/mobile/oauth/confirm/" + ticket
    return RedirectResponse("/auth/google", status_code=303)


@router.get("/oauth/confirm/{ticket}")
def oauth_confirmation(ticket: str, request: Request, conn=Depends(get_conn)):
    from html import escape
    _oauth_table(conn)
    user = session_user(request, conn)
    if not user:
        return RedirectResponse("/login?next=" + urlquote(request.url.path), status_code=303)
    if not conn.execute("SELECT 1 FROM mobile_oauth WHERE id=?", (ticket,)).fetchone():
        raise ApiError("Ссылка входа устарела.", "expired", 410)
    return HTMLResponse('<!doctype html><html lang="ru"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Войти в приложение Donatix</title><body style="font:18px system-ui;padding:24px;max-width:480px;margin:auto">'
        '<h1>Войти в приложение Donatix</h1><p>Аккаунт: <b>' + escape(user["email"]) + '</b></p>'
        '<p>Подтвердите вход, если вы только что нажали «Войти через Google» в приложении.</p>'
        '<form method="post"><input type="hidden" name="csrf" value="' + escape(csrf_token(request)) + '">'
        '<button style="padding:16px;background:#5046ee;color:white;border:0;border-radius:12px">Подтвердить вход</button>'
        '</form></body></html>')


@router.post("/oauth/confirm/{ticket}")
async def oauth_confirm(ticket: str, request: Request, conn=Depends(get_conn)):
    from .deps import check_csrf
    await check_csrf(request)
    user = session_user(request, conn)
    if not user:
        raise ApiError("Войдите в аккаунт.", "unauthorized", 401)
    _oauth_table(conn)
    if conn.execute("UPDATE mobile_oauth SET user_id=? WHERE id=? AND user_id IS NULL", (user["id"], ticket)).rowcount != 1:
        raise ApiError("Ссылка входа устарела или уже использована.", "expired", 410)
    return HTMLResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>Вход подтверждён</title><h1>Вход подтверждён</h1><p>Вернитесь в приложение Donatix.</p>'
                        '<a href="donatix://oauth">Открыть приложение</a>')


class OAuthClaim(BaseModel):
    ticket: str = Field(max_length=100)
    verifier: str = Field(min_length=43, max_length=128)


@router.post("/oauth/claim")
def oauth_claim(body: OAuthClaim, request: Request, conn=Depends(get_conn)):
    _limit(request, "status", "oauth:" + body.ticket)
    _oauth_table(conn)
    with db.tx(conn):
        row = conn.execute("SELECT * FROM mobile_oauth WHERE id=?", (body.ticket,)).fetchone()
        if not row or not secrets.compare_digest(row["challenge"], hashlib.sha256(body.verifier.encode()).hexdigest()):
            raise ApiError("Ссылка входа устарела.", "expired", 410)
        if row["user_id"] is None:
            return {"ok": True, "pending": True}
        user = accounts.get_user(conn, row["user_id"])
        if not user or user["status"] == "blocked":
            raise ApiError("Аккаунт недоступен.", "blocked", 403)
        conn.execute("DELETE FROM mobile_oauth WHERE id=?", (body.ticket,))
    from .web import _record_login
    request.session.clear()
    request.session["user_id"] = user["id"]
    _record_login(conn, request, user["id"])
    return {"ok": True, "pending": False, "csrf": csrf_token(request)}


@router.get('/admin/pricelist')
def admin_pricelist(user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    if user['role'] != 'admin':
        raise ApiError('Доступ только для администратора.', 'forbidden', 403)
    from . import pricelist
    return {'ok': True, **pricelist.build(conn, config), 'site': config.base_url,
            'name': config.site_name, 'support': config.support_contact}


class CartItem(BaseModel):
    product_id: str = Field(max_length=64)
    count: int = Field(ge=1, le=20)
    expected_total_usd: str | None = Field(default=None, max_length=32)


class CartIn(BaseModel):
    items: list[CartItem] = Field(min_length=1, max_length=20)
    fields: dict[str, str] = Field(default_factory=dict)


@router.post('/cart/quote')
def cart_quote(body: CartIn, request: Request, user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    _limit(request, 'status', str(user['id']))
    if sum(i.count for i in body.items) > 20 or len({i.product_id for i in body.items}) != len(body.items):
        raise ApiError('За один раз — не больше 20 пакетов.', 'invalid_cart')
    result, signature, total = [], None, 0
    for i in body.items:
        p = catalog.get_product(conn, i.product_id)
        if not p or p['kind'] != 'topup' or p['max_qty'] != 1:
            raise ApiError('Один из пакетов недоступен.', 'invalid_cart')
        current = (p['category_id'], p.get('region') or '')
        if signature is not None and current != signature:
            raise ApiError('Выберите пакеты одной игры и региона.', 'invalid_cart')
        signature = current
        try:
            orders._clean_fields(p, body.fields)
        except orders.OrderError as exc:
            raise ApiError(str(exc), exc.code, exc.http_status) from None
        price = orders.quote(config, user, p, 1)['total_micro']
        result.append({'product_id': i.product_id, 'count': i.count, 'expected_total_usd': fmt(price)})
        total += price * i.count
    return {'ok': True, 'items': result, 'total_usd': fmt(total), 'balance_usd': fmt(user['balance_micro'])}


@router.post('/cart')
def cart_create(body: CartIn, request: Request, idempotency_key: str = Header(),
                user=Depends(api_user), conn=Depends(get_conn), config=Depends(get_config)):
    import json
    _limit(request, 'orders', str(user['id']))
    if 'api_key_id' in user.keys():
        raise ApiError('Корзина доступна после входа в приложение.', 'session_required', 403)
    if user['status'] != 'active':
        raise ApiError('Аккаунт не активирован.', 'account_inactive', 403)
    if not 16 <= len(idempotency_key) <= 100:
        raise ApiError('Неверный номер операции.', 'invalid_idempotency')
    count = sum(i.count for i in body.items)
    if count > 20 or len({i.product_id for i in body.items}) != len(body.items):
        raise ApiError('За один раз — не больше 20 пакетов, без повторяющихся строк.', 'invalid_cart')
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    conn.execute('CREATE TABLE IF NOT EXISTS mobile_carts (user_id INTEGER NOT NULL, key TEXT NOT NULL, '
                 'fingerprint TEXT NOT NULL, PRIMARY KEY(user_id,key))')
    with db.tx(conn):
        record = conn.execute('SELECT fingerprint FROM mobile_carts WHERE user_id=? AND key=?', (user['id'], idempotency_key)).fetchone()
        if record and record['fingerprint'] != fingerprint:
            raise ApiError('Номер операции уже используется другой корзиной.', 'idempotency_key_reused', 409)
        conn.execute('INSERT OR IGNORE INTO mobile_carts VALUES (?,?,?)', (user['id'], idempotency_key, fingerprint))
    cart, expected = [], None
    for i in body.items:
        p = catalog.get_product(conn, i.product_id)
        if not p or p['kind'] != 'topup' or p['max_qty'] != 1:
            raise ApiError('Один из пакетов недоступен.', 'invalid_cart')
        signature = (p['category_id'], p.get('region') or '')
        if expected is not None and expected != signature:
            raise ApiError('Выберите пакеты одной игры и региона.', 'invalid_cart')
        expected = signature
        try:
            orders._clean_fields(p, body.fields)
        except orders.OrderError as exc:
            raise ApiError(str(exc), exc.code, exc.http_status) from None
        cart.extend([(p, expected_price(i.expected_total_usd))] * i.count)
    # The remaining items need sufficient balance. Previously created items are
    # replayed by their own deterministic idempotency key, never charged again.
    remaining = sum(orders.quote(config, user, p, 1)['total_micro'] for n, (p, _) in enumerate(cart)
                    if orders._by_client_key(conn, user['id'], f'mcart-{idempotency_key}-{n}') is None)
    fresh = accounts.get_user(conn, user['id'])
    if fresh['balance_micro'] + orders.ROUNDING_MICRO < remaining:
        raise ApiError(f'Недостаточно средств. Для оставшихся пакетов нужно ${fmt(remaining)}.', 'insufficient_balance', 402)
    made, error = [], ''
    for n, (p, confirmed_price) in enumerate(cart):
        try:
            order, _ = orders.create_order(conn, config, request.app.state.supplier, accounts.get_user(conn, user['id']),
                product_id=p['id'], quantity=1, fields=body.fields,
                client_idem_key=f'mcart-{idempotency_key}-{n}', source='panel', expected_total_micro=confirmed_price)
            made.append(orders.public_view(order))
        except orders.OrderError as exc:
            error = str(exc)
            break
    return {'ok': True, 'items': made, 'requested': count, 'error': error, 'partial': len(made) != count}


from .api import AccountCheckIn


@router.post('/public/accounts/check')
def public_account_check(body: AccountCheckIn, request: Request, conn=Depends(get_conn)):
    from .api import check_account
    from .web import _guest
    _limit(request, 'status', request.client.host if request.client else 'guest')
    return check_account(body, request, _guest(), conn)


@router.get('/public/gamekeys/{product_id}/regions')
def public_key_regions(product_id: str, request: Request, conn=Depends(get_conn)):
    from .web import _guest
    return key_regions(product_id, request, _guest(), conn)
