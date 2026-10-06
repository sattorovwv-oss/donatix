"""Обработчики /api/v1/*.

Общий вид ответа один на все точки: {"success": true, ...} при удаче и
{"success": false, "error": {"code", "message"}} при отказе. Разбирать
два разных формата в чужом коде — лишняя работа для того, кто к нам
подключается.

Деньги везде — целые числа в дирамах (1 сомони = 100 дирам), рядом
amount_text для показа человеку. Дробных чисел в деньгах нет нарочно.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from aiohttp import web

from app import db, runtime
from app.api import catalog, hooks
from app.api import guard
from app.api.guard import Denied

log = logging.getLogger(__name__)

#: Максимум записей на страницу. Больше отдавать незачем, и это ещё
#: и защита: запрос на миллион строк положил бы бота.
MAX_LIMIT = 100
#: Каталог отдаётся страницами покрупнее: товары мелкие, а листать
#: пять тысяч позиций по сто — двести запросов подряд.
CATALOG_PAGE = 500
CATALOG_MAX = 1000

#: Тело POST больше этого не читаем.
MAX_BODY = 32 * 1024

#: Ключ идемпотентности: печатный ASCII, разумной длины.
IDEM_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")


def ok(**data) -> web.Response:
    return web.json_response({"success": True, **data}, dumps=_dumps)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def fail(status: int, code: str, message: str, **extra) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message, **extra}},
        status=status, dumps=_dumps,
    )


def caller_of(request: web.Request):
    return request["caller"]


def _page(request: web.Request) -> tuple[int, int]:
    """limit и offset из запроса, приведённые к разумным пределам."""
    def number(name: str, default: int, top: int) -> int:
        raw = request.query.get(name, "")
        try:
            value = int(raw) if raw else default
        except ValueError:
            value = default
        return max(0, min(value, top))

    return max(1, number("limit", 20, MAX_LIMIT)), number("offset", 0, 1_000_000)


def _page_of(request: web.Request, default: int, top: int) -> tuple[int, int]:
    """То же, но со своими пределами: у каталога страница крупнее."""
    def number(name: str, fallback: int, cap: int) -> int:
        raw = request.query.get(name, "")
        try:
            value = int(raw) if raw else fallback
        except ValueError:
            value = fallback
        return max(0, min(value, cap))

    return max(1, number("limit", default, top)), number("offset", 0, 1_000_000)


#: Названия периодов. Их же показывает кабинет вкладками.
PERIODS = ("today", "yesterday", "7d", "30d", "all")
#: Насколько далеко часовой пояс может отстоять от UTC, в минутах.
TZ_LIMIT = 14 * 60


def _tz(request: web.Request) -> timezone:
    """Часовой пояс клиента. Без него «сегодня» считалось бы по UTC.

    В Душанбе день начинается на пять часов раньше UTC, и утренние
    заказы попадали бы во «вчера» — ровно там, где владелец их и стал бы
    искать в первую очередь.
    """
    try:
        minutes = int(request.query.get("tz", "0"))
    except ValueError:
        minutes = 0
    return timezone(timedelta(minutes=max(-TZ_LIMIT, min(minutes, TZ_LIMIT))))


def _span(request: web.Request) -> tuple[str, str, str] | None:
    """Отрезок времени в UTC: (since, until, название). None — мусор в запросе.

    Пустые границы означают «за всё время»: так самый частый случай не
    тащит за собой лишних условий в запросе.
    """
    zone = _tz(request)
    now = datetime.now(zone)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def iso(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    raw_from = (request.query.get("from") or "").strip()
    raw_to = (request.query.get("to") or "").strip()
    if raw_from or raw_to:
        try:
            start = (datetime.strptime(raw_from, "%Y-%m-%d").replace(tzinfo=zone)
                     if raw_from else None)
            end = (datetime.strptime(raw_to, "%Y-%m-%d").replace(tzinfo=zone)
                   + timedelta(days=1) if raw_to else None)
        except ValueError:
            return None
        if start and end and end <= start:
            return None
        return (iso(start) if start else "", iso(end) if end else "", "custom")

    name = (request.query.get("period") or "all").strip().lower()
    if name not in PERIODS:
        return None
    if name == "today":
        return iso(day), iso(day + timedelta(days=1)), name
    if name == "yesterday":
        return iso(day - timedelta(days=1)), iso(day), name
    if name in ("7d", "30d"):
        days = 7 if name == "7d" else 30
        return iso(day - timedelta(days=days - 1)), iso(day + timedelta(days=1)), name
    return "", "", "all"


BAD_SPAN = ("bad_period",
            "period — today, yesterday, 7d, 30d или all; "
            "from и to — даты вида 2026-09-18.")


# ─────────────────────────────────────────────────────── товары


async def products(request: web.Request) -> web.Response:
    """Каталог страницами.

    Когда отдаётся каталог поставщика целиком, товаров тысячи, и полный
    ответ — это мегабайты на каждый запрос. Поэтому страница, а не всё
    сразу: total говорит, сколько всего нашлось, и разработчик сам решает,
    забирать ли остальное.
    """
    items = await catalog.listing(
        request["conn"], request.app["provider"], request.query.get("type", "")
    )
    game = request.query.get("game", "")
    if game:
        items = [item for item in items if item.get("game_id") == game]

    total = len(items)
    limit, offset = _page_of(request, CATALOG_PAGE, CATALOG_MAX)
    page = items[offset:offset + limit]
    # Курс отдаём рядом с товарами: по нему разработчик может пересчитать
    # любую нашу сумму сам и сверить с ценами, которые ему называют в
    # долларах.
    return ok(count=len(page), total=total, limit=limit, offset=offset,
              usd_rate=catalog.money(catalog.usd_rate()),
              products=[catalog.public(item) for item in page])


async def games(request: web.Request) -> web.Response:
    """Список игр без пакетов: 297 названий вместо пяти тысяч строк.

    С полным каталогом поставщика разобраться по /products тяжело —
    разработчику сперва нужен список игр, а пакеты он заберёт по одной.
    """
    items = await catalog.listing(request["conn"], request.app["provider"],
                                  "game")
    found: dict[str, dict] = {}
    for item in items:
        row = found.setdefault(item["game_id"], {
            "id": item["game_id"], "name": item["game"],
            "fields": item["fields"], "packs": 0,
        })
        row["packs"] += 1
    out = sorted(found.values(), key=lambda row: row["name"].lower())
    return ok(count=len(out), games=out)


async def product(request: web.Request) -> web.Response:
    item = await catalog.find(request["conn"], request.app["provider"],
                              request.match_info["product_id"])
    if item is None:
        return fail(404, "product_not_found", "Такого товара нет.")
    return ok(product=catalog.public(item),
              usd_rate=catalog.money(catalog.usd_rate()))


# ─────────────────────────────────────────────────────── аккаунт


async def balance(request: web.Request) -> web.Response:
    caller = caller_of(request)
    fresh = await db.get_user(request["conn"], caller.user_id)
    return ok(balance=catalog.money(fresh.balance if fresh else 0))


async def user(request: web.Request) -> web.Response:
    caller = caller_of(request)
    conn = request["conn"]
    fresh = await db.get_user(conn, caller.user_id)
    stats = await db.api_stats(conn, caller.user_id)
    hook = await db.get_api_hook(conn, caller.user_id)
    return ok(user={
        "id": caller.user_id,
        "username": (fresh.username if fresh else None),
        "balance": catalog.money(fresh.balance if fresh else 0),
        "created_at": fresh.created_at if fresh else None,
        # Пропуском в кабинет вошли без ключа — и рассказывать про
        # чужой ключ тут нечего.
        "key": ({
            "id": caller.key.id,
            "label": caller.key.label,
            "masked": caller.key.masked,
            "created_at": caller.key.created_at,
            "last_used_at": caller.key.last_used_at,
            "requests": caller.key.requests,
        } if caller.key else None),
        "read_only": caller.watch,
        "rate_limit_per_minute": runtime.get_int("api_rate_per_min", 60),
        "requests": stats,
        "webhook": {
            "url": (hook or {}).get("url") or "",
            "enabled": bool((hook or {}).get("enabled", 0)),
        },
    })


# ─────────────────────────────────────────── проверка ID игрока

#: Проверка стоит запросов у сторонних справочников, а у бесплатного из
#: них всего сотня в месяц. Поэтому она забирает из ведра больше обычной
#: и помнится полчаса: один и тот же ID проверяют по нескольку раз подряд.
CHECK_COST = 5.0
CHECK_TTL = 30 * 60
#: Сколько ответов помним. Ключей столько же, сколько разных игроков —
#: за месяц их набегает больше, чем стоит держать в памяти.
CHECK_KEEP = 5_000
_checked: dict[tuple, tuple[float, dict]] = {}


def forget_checks() -> None:
    _checked.clear()


async def check_id(request: web.Request) -> web.Response:
    """Ник игрока по его ID — до покупки, чтобы не платить не туда."""
    import time as _time

    from app.handlers.games import _lookup
    from app.services import suppliers

    conn, caller = request["conn"], caller_of(request)
    product_id = (request.query.get("product_id") or "").strip()
    customer = (request.query.get("customer") or "").strip()
    if not product_id or not customer:
        return fail(400, "bad_query",
                    "Нужны product_id и customer: ?product_id=…&customer=…")

    item = await catalog.find(conn, request.app["provider"], product_id)
    if item is None:
        return fail(404, "product_not_found", "Такого товара нет или он снят.")
    if item["type"] != "game":
        return fail(400, "not_checkable",
                    "Проверка ID есть только у игр. Для звёзд и Premium "
                    "получатель — юзернейм, он проверяется при заказе.")

    game = await db.get_game(conn, item["game_id"])
    if game is None:
        game = catalog._shadow_game({"category_id": item["game_id"],
                                     "name": item["game"],
                                     "fields": item.get("fields") or []})
    fields = _game_fields(game, customer)
    if fields is None:
        return fail(400, "bad_customer",
                    "Для этой игры нужно: "
                    f"{', '.join(game.field_names)} — через пробел, "
                    "запятую или знак минус.")

    key = (item["game_id"], tuple(sorted(fields.items())))
    hit = _checked.get(key)
    if hit and _time.time() - hit[0] < CHECK_TTL:
        return ok(**hit[1])

    wait = guard.take(caller.key_id or -caller.user_id, cost=CHECK_COST)
    if wait:
        return fail(429, "rate_limited",
                    "Проверка ID расходует чужие справочники и ограничена "
                    "жёстче обычного. Повторите позже.",
                    retry_after=int(wait) + 1)

    name, verdict = await _lookup(suppliers.for_games(request.app["provider"]),
                                  game, fields)
    answer = {
        "valid": verdict != "bad",
        "verdict": verdict,          # ok | bad | unknown
        "nickname": name or "",
        "game": item["game"],
        "product_id": product_id,
        "customer": customer,
    }
    if verdict == "ok":
        if len(_checked) >= CHECK_KEEP:
            stale = _time.time() - CHECK_TTL
            for old_key, (seen, _) in list(_checked.items()):
                if seen < stale:
                    _checked.pop(old_key, None)
            if len(_checked) >= CHECK_KEEP:      # свежих и так слишком много
                _checked.clear()
        # Запоминаем только удачные ответы: «не нашли» через минуту
        # вполне может смениться на «нашли», и запомнить отказ значило бы
        # испортить проверку до перезапуска.
        _checked[key] = (_time.time(), answer)
    return ok(**answer)


# ─────────────────────────────────────────── просьба о возврате


async def order_refund(request: web.Request) -> web.Response:
    """Оставить заявку на возврат. Решает владелец, не мы и не клиент."""
    conn, caller = request["conn"], caller_of(request)
    data = await _body(request)
    ref = str(data.get("order_id") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if not ref:
        return fail(400, "missing_order_id", "Укажите order_id.")
    if len(reason) < 5:
        return fail(400, "missing_reason",
                    "Опишите причину — без неё владельцу нечего решать.")

    row = await db.api_order_by_ref(conn, ref)
    if row is None or row["user_id"] != caller.user_id:
        return fail(404, "order_not_found", "Заказ не найден.")

    order = await db.get_order(conn, row["order_id"])
    if order is None:
        return fail(404, "order_not_found", "Заказ не найден.")
    if order.status == db.ORDER_REFUNDED:
        return fail(409, "already_refunded", "Деньги за этот заказ уже вернулись.")
    if order.status != db.ORDER_DELIVERED:
        return fail(409, "still_processing",
                    "Заказ ещё в работе. Если он не пройдёт, деньги "
                    "вернутся сами — заявка не нужна.")

    ask = await db.ask_refund(conn, ref=ref, user_id=caller.user_id,
                              reason=reason)
    return ok(order_id=ref, refund={
        "status": ask.get("status", "pending"),
        "reason": ask.get("reason", ""),
        "answer": ask.get("answer", ""),
        "created_at": ask.get("created_at", ""),
        "decided_at": ask.get("decided_at") or "",
    })


# ─────────────────────────────────────────────────────── заказы


def order_view(row: dict) -> dict:
    """Один заказ так, как его видит разработчик."""
    status = hooks.PUBLIC.get(row["status"], "processing")
    view = {
        "order_id": row["ref"],
        "status": status,
        "product_id": row["product_id"],
        "quantity": row.get("quantity", 1),
        "customer": row.get("customer") or "",
        "result": row.get("recipient") or "",
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at") or row["created_at"],
        **catalog.money(row.get("price", 0)),
    }
    if status == "refunded":
        view["reason"] = (row.get("error") or "")[:300]
    return view


async def orders(request: web.Request) -> web.Response:
    span = _span(request)
    if span is None:
        return fail(400, *BAD_SPAN)
    since, until, period = span

    conn, user_id = request["conn"], caller_of(request).user_id
    limit, offset = _page(request)
    rows = await db.api_orders_of(conn, user_id, limit, offset, since, until)
    total = await db.api_orders_count(conn, user_id, since, until)
    return ok(count=len(rows), total=total, limit=limit, offset=offset,
              period=period, orders=[order_view(row) for row in rows])


async def orders_summary(request: web.Request) -> web.Response:
    """Сводка за период: сколько заказов и куда ушли деньги."""
    span = _span(request)
    if span is None:
        return fail(400, *BAD_SPAN)
    since, until, period = span

    data = await db.api_summary(request["conn"], caller_of(request).user_id,
                                since, until)
    return ok(period=period,
              orders=data["orders"], completed=data["done"],
              refunded=data["refunded"], processing=data["working"],
              spent=catalog.money(data["spent"]),
              returned=catalog.money(data["returned"]))


#: Как назвать движение денег человеку. Знак суммы уже говорит сам за
#: себя, но в таблице кабинета нужна и словесная подпись.
TX_TITLES = {"deposit": "пополнение", "charge": "покупка",
             "refund": "возврат", "adjust": "правка"}


async def transactions(request: web.Request) -> web.Response:
    """Движение денег: пополнения, списания, возвраты."""
    span = _span(request)
    if span is None:
        return fail(400, *BAD_SPAN)
    since, until, period = span

    conn, user_id = request["conn"], caller_of(request).user_id
    limit, offset = _page(request)
    rows = await db.api_txs_of(conn, user_id, limit, offset, since, until)
    total = await db.api_txs_count(conn, user_id, since, until)
    return ok(count=len(rows), total=total, limit=limit, offset=offset,
              period=period,
              transactions=[{
                  "transaction_id": row["tx_id"],
                  "kind": row["kind"],
                  "kind_text": TX_TITLES.get(row["kind"], row["kind"]),
                  "order_id": row["order_ref"] or "",
                  "note": row["note"] or "",
                  "created_at": row["created_at"],
                  "balance_after": row["balance_after"],
                  **catalog.money(row["amount"]),
              } for row in rows])


async def _one_order(request: web.Request, ref: str) -> web.Response:
    conn = request["conn"]
    row = await db.api_order_by_ref(conn, ref)
    if row is None or row["user_id"] != caller_of(request).user_id:
        # Чужой заказ и несуществующий отвечают одинаково: иначе по коду
        # ответа можно было бы перебором узнать, какие номера заняты.
        return fail(404, "order_not_found", "Заказ не найден.")

    order = await db.get_order(conn, row["order_id"])
    if order is None:
        return fail(404, "order_not_found", "Заказ не найден.")
    row |= {"status": order.status, "price": order.price,
            "quantity": order.quantity, "recipient": order.recipient,
            "error": order.error, "updated_at": order.updated_at}
    return ok(**order_view(row))


async def order_by_id(request: web.Request) -> web.Response:
    return await _one_order(request, request.match_info["order_id"])


async def order_status(request: web.Request) -> web.Response:
    ref = (request.query.get("order_id") or "").strip()
    if not ref:
        return fail(400, "missing_order_id", "Укажите order_id.")
    return await _one_order(request, ref)


# ─────────────────────────────────────────────────────── покупка


async def _body(request: web.Request) -> dict:
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise Denied(413, "body_too_large", "Тело запроса слишком большое.")
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Denied(400, "bad_json", "Тело запроса — не JSON.") from None
    if not isinstance(data, dict):
        raise Denied(400, "bad_json", "Ожидается объект JSON.")
    return data


async def order_create(request: web.Request) -> web.Response:
    from app.services import delivery

    conn = request["conn"]
    caller = caller_of(request)
    data = await _body(request)

    product_id = str(data.get("product_id") or "").strip()
    if not product_id:
        return fail(400, "missing_product_id", "Укажите product_id.")

    item = await catalog.find(conn, request.app["provider"], product_id)
    if item is None:
        return fail(404, "product_not_found", "Такого товара нет или он снят.")

    quantity = data.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        return fail(400, "bad_quantity", "quantity — целое число от 1.")
    if item["variable"]:
        if not item["min_quantity"] <= quantity <= item["max_quantity"]:
            return fail(400, "bad_quantity",
                        f"quantity от {item['min_quantity']} "
                        f"до {item['max_quantity']}.")
    elif quantity != 1:
        return fail(400, "bad_quantity",
                    "У этого товара фиксированный объём, quantity = 1.")

    customer = str(data.get("customer") or "").strip()
    if item["customer_required"] and not customer:
        return fail(400, "missing_customer", f"Укажите customer: {item['customer']}")
    if len(customer) > 190:
        return fail(400, "bad_customer", "customer слишком длинный.")

    idem = str(data.get("idempotency_key")
               or request.headers.get("Idempotency-Key") or "").strip()
    if idem and not IDEM_RE.match(idem):
        return fail(400, "bad_idempotency_key",
                    "idempotency_key — 8–128 знаков: буквы, цифры, . _ : -")

    if idem:
        free, existing = await db.reserve_idem(conn, caller.key.id, idem)
        if not free:
            if not existing:
                return fail(409, "in_progress",
                            "Этот же запрос ещё выполняется. Повторите позже.")
            return await _one_order(request, existing)

    try:
        return await _place(request, caller, item, quantity, customer, idem)
    except Exception:
        # Заказ не создался — резерв надо отпустить, иначе повтор того же
        # запроса навсегда упирался бы в «уже выполняется».
        if idem:
            await db.release_idem(conn, caller.key.id, idem)
        raise


async def _place(request, caller, item, quantity, customer, idem) -> web.Response:
    """Создать и оплатить заказ.

    Порядок шагов выбран так, чтобы ни одна поломка не оставила клиента
    без денег и без товара, а нас — без следов:

      1. списываем деньги — атомарно, проверкой баланса в том же UPDATE,
         и сразу записью в журнал транзакций;
      2. заводим заказ в общей таблице orders: отчёты, возвраты и
         статистика считают его вместе с заказами из бота;
      3. связываем заказ с ключом разработчика — ДО первой выдачи,
         иначе выдача сочла бы его человеческим и написала бы роботу
         в Telegram;
      4. и только теперь зовём поставщика.

    Если поставщик откажет, деньги вернёт та же общая логика, что и в
    боте: возврат попадёт и в журнал транзакций, и в вебхук клиенту.
    """
    from app.services import delivery, suppliers
    from app.services import games as gsvc

    conn = request["conn"]
    price = catalog.price_of(item, quantity)
    if price <= 0:
        return fail(409, "product_unavailable", "Цена товара сейчас не задана.")

    product_type, real_quantity = catalog.order_plan(item, quantity)
    game = None
    if item["type"] == "game":
        game = await catalog.game_for(conn, item)
        if game is None:
            return fail(404, "product_not_found", "Игра снята с продажи.")
        fields = _game_fields(game, customer)
        if fields is None:
            return fail(400, "bad_customer",
                        "Для этой игры нужно: "
                        f"{', '.join(game.field_names)} — через пробел, "
                        "запятую или знак минус.")

    ref = await db.next_api_ref(conn)
    paid, tx_id = await db.charge_logged(
        conn, caller.user_id, price, order_ref=ref, note=item["name"][:180]
    )
    if not paid:
        fresh = await db.get_user(conn, caller.user_id)
        return fail(402, "insufficient_funds", "Недостаточно средств.",
                    required=price, balance=(fresh.balance if fresh else 0),
                    currency=catalog.currency())

    cost = (int(item.get("cost", 0)) if item["type"] == "game"
            else runtime.cost_of(product_type, real_quantity))
    # Деньги уже списаны. Если заказ не заведётся — база занята, диск
    # кончился, — вернуть их надо здесь и сейчас: снаружи об этом узнают
    # только по пропавшей сумме, а следа для разбора не останется.
    try:
        order = await db.create_order(
            conn, user_id=caller.user_id, product_type=product_type,
            quantity=real_quantity, recipient=customer, price=price, cost=cost,
        )
        await db.link_api_order(
            conn, ref=ref, order_id=order.id, key_id=caller.key.id,
            user_id=caller.user_id, product_id=item["id"], customer=customer,
            idem_key=idem,
        )
    except Exception:
        await db.credit_logged(conn, caller.user_id, price, kind="refund",
                               order_ref=ref, note="заказ не создался")
        raise
    if idem:
        await db.finish_idem(conn, caller.key.id, idem, ref)

    bot, provider = request.app["bot"], request.app["provider"]
    if game is not None:
        order = await gsvc.order_now(
            bot, conn, suppliers.for_games(provider), game=game,
            offer_id=catalog.offer_id_of(item), fields=fields, order=order,
        )
    else:
        # Звёзды и Premium через API — с того же счёта, что и игры:
        # деньги разработчиков в одном месте.
        order = await delivery.run(bot, conn, suppliers.for_api(provider), order)

    row = {"ref": ref, "product_id": item["id"], "customer": customer,
           "status": order.status, "price": order.price,
           "quantity": order.quantity, "recipient": order.recipient,
           "error": order.error, "created_at": order.created_at,
           "updated_at": order.updated_at}
    return ok(transaction_id=tx_id, **order_view(row))


def _game_fields(game, customer: str) -> dict[str, str] | None:
    """Разобрать customer на поля аккаунта. None — данных не хватило.

    Разбираем тем же кодом, что и ввод в боте: у части игр аккаунт задан
    парой чисел, и два разных разбора разошлись бы на первой же такой
    игре — в боте заказ проходил бы, через API нет.
    """
    from app.handlers.games import parse_ids

    names = list(game.field_names)
    values = parse_ids(customer, len(names))
    if values is None:
        return None
    return dict(zip(names, values))
