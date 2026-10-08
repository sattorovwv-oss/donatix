"""Публичный API для клиентов Donatix: /api/v1.

Формат как у FazerCards, чтобы клиентам было легко переехать:
успех — {"ok": true, ...}, ошибка — {"ok": false, "error": "...", "code": "..."}."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import account_check, accounts, catalog, db, orders
from .config import Config
from .deps import get_config, get_conn
from .money import fmt

router = APIRouter(prefix="/api/v1", tags=["API"])


class ApiError(Exception):
    def __init__(self, message: str, code: str, http_status: int = 400, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.headers = headers or {}


def api_error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": str(exc), "code": exc.code}, status_code=exc.http_status, headers=exc.headers
    )


def _limit(request: Request, category: str, key: str) -> None:
    wait = request.app.state.limiter.hit(category, key)
    if wait is not None:
        raise ApiError("Слишком много запросов.", "rate_limited", 429, {"Retry-After": str(int(wait) + 1)})


def api_user(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> sqlite3.Row:
    key = (x_api_key or "").strip()
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    key = key.strip('"\'').strip()   # ключ, вставленный вместе с кавычками, тоже подходит
    if not key:
        raise ApiError("Нет API-ключа. Передайте заголовок X-API-Key.", "unauthorized", 401)
    user = accounts.user_by_api_key(conn, key)
    if user is None:
        raise ApiError(accounts.api_key_problem(conn, key), "unauthorized", 401)
    if user["status"] == "blocked":
        raise ApiError("Аккаунт заблокирован.", "account_blocked", 403)
    return user


def _page(page: int, limit: int) -> tuple[int, int]:
    return max(page, 1), min(max(limit, 1), 100)


# ── Аккаунт ──────────────────────────────────────────────────


@router.get("/me")
def me(request: Request, user=Depends(api_user), conn=Depends(get_conn)) -> dict[str, Any]:
    _limit(request, "account", str(user["id"]))
    summary = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_micro), 0) AS spent FROM orders "
        "WHERE user_id = ? AND status = 'completed'",
        (user["id"],),
    ).fetchone()
    return {
        "ok": True,
        "login": user["login"],
        "email": user["email"],
        "status": user["status"],
        "tier": user["tier"],
        "balance": fmt(user["balance_micro"]),
        "currency": "USD",
        "summary": {"totalSpent": fmt(summary["spent"]), "totalOrders": summary["n"]},
        "createdAt": user["created_at"],
    }


@router.get("/balance")
def balance(request: Request, user=Depends(api_user)) -> dict[str, Any]:
    _limit(request, "account", str(user["id"]))
    return {"ok": True, "balance": fmt(user["balance_micro"]), "currency": "USD"}


def _tx_view(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    order_public = None
    if row["order_id"]:
        o = conn.execute("SELECT public_id FROM orders WHERE id = ?", (row["order_id"],)).fetchone()
        order_public = o["public_id"] if o else None
    return {
        "id": accounts.tx_public_id(row["id"]),
        "type": row["type"],
        "amount": fmt(row["amount_micro"]),
        "balanceBefore": fmt(row["balance_before"]),
        "balanceAfter": fmt(row["balance_after"]),
        "note": row["note"],
        "orderId": order_public,
        "createdAt": row["created_at"],
    }


@router.get("/transactions")
def transactions(
    request: Request, page: int = 1, limit: int = 20, user=Depends(api_user), conn=Depends(get_conn)
) -> dict[str, Any]:
    _limit(request, "account", str(user["id"]))
    page, limit = _page(page, limit)
    total = conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ?", (user["id"],)).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user["id"], limit, (page - 1) * limit),
    ).fetchall()
    return {"ok": True, "items": [_tx_view(r, conn) for r in rows], "total": total, "page": page, "limit": limit}


@router.get("/transactions/{transaction_id}")
def transaction(transaction_id: str, request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    _limit(request, "account", str(user["id"]))
    tx_id = accounts.parse_tx_id(transaction_id)
    row = conn.execute(
        "SELECT * FROM transactions WHERE id = ? AND user_id = ?", (tx_id or -1, user["id"])
    ).fetchone()
    if row is None:
        raise ApiError("Транзакция не найдена.", "not_found", 404)
    return {"ok": True, "transaction": _tx_view(row, conn)}


# ── Каталог ──────────────────────────────────────────────────


@router.get("/categories")
def categories(request: Request, user=Depends(api_user), conn=Depends(get_conn)) -> dict[str, Any]:
    _limit(request, "catalog", str(user["id"]))
    items = [
        {"kind": r["kind"], "category_id": r["category_id"], "name": r["category_name"], "products": r["n"]}
        for r in catalog.categories(conn)
    ]
    return {"ok": True, "items": items}


@router.get("/products")
def products(
    request: Request,
    kind: str = "",
    category_id: str = "",
    q: str = Query(default="", max_length=100),
    limit: int = 100,
    offset: int = 0,
    user=Depends(api_user),
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    _limit(request, "catalog", str(user["id"]))
    limit = min(max(limit, 1), 500)
    items = catalog.list_products(conn, kind=kind, category_id=category_id, q=q, limit=limit, offset=max(offset, 0))
    views = [catalog.public_view(p, accounts.markup_for(user, config, p["kind"])) for p in items]
    return {"ok": True, "items": views, "limit": limit, "offset": offset}


@router.get("/products/{product_id}")
def product(product_id: str, request: Request, user=Depends(api_user), conn=Depends(get_conn),
            config: Config = Depends(get_config)) -> dict[str, Any]:
    _limit(request, "catalog", str(user["id"]))
    p = catalog.get_product(conn, product_id)
    if p is None:
        raise ApiError("Товар не найден.", "product_not_found", 404)
    view = catalog.public_view(p, accounts.markup_for(user, config, p["kind"]))
    view["account_check"] = account_check.can_check(request.app.state.supplier, p)
    return {"ok": True, "product": view}


class AccountCheckIn(BaseModel):
    product_id: str = Field(max_length=64)
    fields: dict[str, str] = Field(default_factory=dict)


@router.post("/accounts/check")
def check_account(body: AccountCheckIn, request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    """Проверить аккаунт игрока до заказа: вернёт ник, если поставщик умеет проверять эту игру."""
    _limit(request, "status", str(user["id"]))
    p = catalog.get_product(conn, body.product_id)
    if p is None:
        raise ApiError("Товар не найден.", "product_not_found", 404)
    return {"ok": True, **account_check_view(request.app.state.supplier, p, body.fields)}


def account_check_view(supplier, product: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    if not account_check.can_check(supplier, product):
        return {"supported": False, "valid": None, "player_name": None}
    keys = [f["key"] for f in product["fields"]]
    clean = {k: str(fields.get(k, "")).strip()[:100] for k in keys}
    if not all(clean.values()):
        raise ApiError("Заполните все поля получателя.", "invalid_field")
    r = account_check.check(supplier, product, clean)
    return {"supported": True, "valid": r["valid"], "player_name": r["player_name"], "region": r["region"],
            "message": r["message"]}


# ── Заказы ───────────────────────────────────────────────────


class OrderIn(BaseModel):
    product_id: str = Field(max_length=64)
    quantity: int = 1
    fields: dict[str, str] = Field(default_factory=dict)


@router.post("/orders", status_code=201)
def create_order(
    body: OrderIn,
    request: Request,
    idempotency_key: str | None = Header(default=None),
    user=Depends(api_user),
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
):
    _limit(request, "orders", str(user["id"]))
    try:
        order, replay = orders.create_order(
            conn, config, request.app.state.supplier, user,
            product_id=body.product_id, quantity=body.quantity, fields=body.fields,
            client_idem_key=idempotency_key, source="api",
        )
    except orders.OrderError as exc:
        raise ApiError(str(exc), exc.code, exc.http_status) from None
    return JSONResponse({"ok": True, "order": orders.public_view(order)}, status_code=200 if replay else 201)


@router.get("/orders")
def list_orders(
    request: Request, page: int = 1, limit: int = 20, status: str = "",
    user=Depends(api_user), conn=Depends(get_conn),
) -> dict[str, Any]:
    _limit(request, "status", str(user["id"]))
    page, limit = _page(page, limit)
    where, args = "user_id = ?", [user["id"]]
    if status == "processing":
        where += " AND status IN ('processing', 'attention')"
    elif status:
        where += " AND status = ?"
        args.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM orders WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, (page - 1) * limit]
    ).fetchall()
    return {"ok": True, "items": [orders.public_view(r) for r in rows], "total": total, "page": page, "limit": limit}


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request, user=Depends(api_user), conn=Depends(get_conn)):
    _limit(request, "status", str(user["id"]))
    row = orders.find_user_order(conn, user["id"], order_id)
    if row is None:
        raise ApiError("Заказ не найден.", "not_found", 404)
    row = orders.refresh_if_stale(conn, request.app.state.supplier, row)
    return {"ok": True, "order": orders.public_view(row)}


# ── Steam-гифты ──────────────────────────────────────────────


@router.get("/steam-gifts/games")
def steam_gift_games(request: Request, q: str = Query(default="", max_length=100), limit: int = 30,
                     user=Depends(api_user), conn=Depends(get_conn)) -> dict[str, Any]:
    _limit(request, "catalog", str(user["id"]))
    from . import steam_gifts
    return {"ok": True, "items": steam_gifts.search(conn, q, min(max(limit, 1), 100))}


@router.get("/steam-gifts/games/{appid}")
def steam_gift_game(appid: int, request: Request, user=Depends(api_user), conn=Depends(get_conn),
                    config: Config = Depends(get_config)) -> dict[str, Any]:
    _limit(request, "catalog", str(user["id"]))
    return steam_gift_view(request, conn, config, user, appid)


def steam_gift_view(request: Request, conn: sqlite3.Connection, config: Config, user, appid: int) -> dict[str, Any]:
    from . import steam_gifts
    from .suppliers import SupplierError

    if catalog.get_product(conn, "steam-gift") is None:
        raise ApiError("Steam-гифты сейчас недоступны.", "product_not_found", 404)
    try:
        items = steam_gifts.client_offers(request.app.state.supplier, appid,
                                          accounts.markup_for(user, config, "steam_gift"))
    except SupplierError:
        raise ApiError("Игра не найдена или поставщик не ответил.", "not_found", 404) from None
    return {"ok": True, "appid": appid, "name": steam_gifts.game_name(conn, appid), "product_id": "steam-gift",
            "cover": steam_gifts.cover_url(appid), "offers": items}


# ── Пополнение баланса (для ботов и сайтов партнёров) ──────────


class PaymentIn(BaseModel):
    method: str = Field(max_length=32)
    amount_usd: str = ""
    amount_tjs: str = ""
    reference: str = Field(default="", max_length=200)


@router.get("/payments/methods")
def payment_methods(request: Request, user=Depends(api_user), conn=Depends(get_conn),
                    config: Config = Depends(get_config)) -> dict[str, Any]:
    from . import payments, rates
    rates.refresh(conn, config, rates.PAYMENT_SECONDS)
    conf = payments.settings(conn, config)
    return {"ok": True, "methods": payments.methods(conn, config), "tjs_rate": str(conf["tjs_rate"]),
            "min_tjs": str(conf["min_tjs"]), "min_usd": str(conf["min_usd"])}


@router.post("/payments", status_code=201)
def payment_create(body: PaymentIn, request: Request, user=Depends(api_user), conn=Depends(get_conn),
                   config: Config = Depends(get_config)):
    """Заявка на пополнение: вернёт реквизиты и сумму к переводу. Потом — POST /payments/{id}/receipt с чеком."""
    from . import payments, rates
    _limit(request, "account", str(user["id"]))
    rates.refresh(conn, config, rates.PAYMENT_SECONDS)
    try:
        with db.tx(conn):   # 10 одновременных запросов — создастся одна заявка
            pid = payments.create(conn, config, user, body.method, body.amount_usd, body.reference,
                                  amount_tjs=body.amount_tjs)
        payments.start_auto(conn, config, pid)  # TRC20 / Binance Pay — ссылка или адрес для оплаты
    except payments.PaymentError as exc:
        raise ApiError(str(exc), "invalid_payment") from None
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()
    return JSONResponse({"ok": True, "payment": payments.public(conn, config, row)}, status_code=201)


@router.post("/payments/{payment_id}/receipt")
async def payment_receipt(payment_id: int, request: Request, user=Depends(api_user), conn=Depends(get_conn),
                          config: Config = Depends(get_config)):
    """Чек: multipart-поле file (фото или PDF) или сырое тело с Content-Type image/* / application/pdf."""
    from . import payments
    from .tgbot import send_receipt
    _limit(request, "account", str(user["id"]))
    ctype = request.headers.get("content-type", "")
    limit = payments.MAX_RECEIPT_BYTES + 64 * 1024
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise ApiError("Файл чека пустой или больше 10 МБ.", "invalid_receipt")
    if ctype.startswith("multipart/"):
        form = await request.form()
        up = form.get("file") or form.get("photo") or form.get("document")
        if up is None or not hasattr(up, "read"):
            raise ApiError("Приложите файл в поле file.", "missing_field")
        data = await up.read(payments.MAX_RECEIPT_BYTES + 1)
    else:
        chunks, size = [], 0
        async for chunk in request.stream():   # без Content-Length тело тоже не больше лимита
            size += len(chunk)
            if size > limit:
                raise ApiError("Файл чека пустой или больше 10 МБ.", "invalid_receipt")
            chunks.append(chunk)
        data = b"".join(chunks)

    def attach() -> None:   # ИИ и Telegram — в отдельном потоке, чтобы не стоял весь сервер
        payments.attach_receipt(conn, config, user["id"], payment_id, data)
        send_receipt(conn, config, payment_id)

    try:
        await run_in_threadpool(attach)
    except payments.PaymentError as exc:
        raise ApiError(str(exc), "invalid_receipt") from None
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return {"ok": True, "payment": payments.public(conn, config, row)}


@router.get("/payments")
def payment_list(request: Request, user=Depends(api_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)) -> dict[str, Any]:
    from . import payments
    rows = conn.execute("SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
    return {"ok": True, "items": [payments.public(conn, config, r) for r in rows]}


@router.get("/payments/{payment_id}")
def payment_one(payment_id: int, user=Depends(api_user), conn=Depends(get_conn),
                config: Config = Depends(get_config)) -> dict[str, Any]:
    from . import payments
    row = conn.execute("SELECT * FROM payments WHERE id = ? AND user_id = ?", (payment_id, user["id"])).fetchone()
    if row is None:
        raise ApiError("Заявка не найдена.", "not_found", 404)
    if row["status"] == "pending" and row["auto_kind"]:
        from . import cryptopay
        cryptopay.check_all(conn, config)  # автоплатёж: проверить поступление (не чаще раза в 20 с)
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return {"ok": True, "payment": payments.public(conn, config, row)}
