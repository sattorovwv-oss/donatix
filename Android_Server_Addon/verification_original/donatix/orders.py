"""Заказы — самое важное место. Порядок, при котором деньги не теряются:

1. Списываем у клиента и создаём заказ «processing» — в одной транзакции.
2. Отправляем заказ поставщику со своим уникальным ключом (Idempotency-Key).
3. Поставщик точно отказал → возвращаем деньги.
   Ответ непонятен (таймаут, сеть) → деньги НЕ возвращаем, заказ остаётся
   «processing», воркер выясняет, что случилось.
4. Воркер спрашивает статус у поставщика, пока заказ не выполнится или не упадёт.
   Упал → возврат. Никто не понимает, что с заказом → статус «attention» для админа.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from . import accounts, db
from .catalog import get_product
from .config import Config
from .money import MoneyError, apply_markup, fmt, fmt_unit, order_total_micro, to_decimal
from .suppliers import Supplier, SupplierRejected, SupplierUnavailable

log = logging.getLogger(__name__)

MAX_CREATE_ATTEMPTS = 5
IN_FLIGHT_SECONDS = 30  # столько ждём, пока запрос из веба сам дойдёт до поставщика
STUCK_HOURS = 24
REFRESH_ON_READ_SECONDS = 5

TG_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
STEAM_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")


class OrderError(Exception):
    def __init__(self, message: str, code: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ── Проверка ввода ───────────────────────────────────────────


def _clean_fields(product: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, str]:
    raw = raw or {}
    result: dict[str, str] = {}
    for spec in product["fields"]:
        key = spec["key"]
        value = str(raw.get(key, "") or "").strip()
        if not value:
            raise OrderError(f"Не заполнено поле «{spec.get('label') or key}» ({key}).", "missing_field")
        if len(value) > 256:
            raise OrderError(f"Поле {key} слишком длинное.", "invalid_field")
        if key == "telegram_username":
            if not TG_USERNAME_RE.match(value):
                raise OrderError("Неверный Telegram username.", "invalid_field")
            value = "@" + value.lstrip("@")
        elif key == "steam_login":
            if not STEAM_LOGIN_RE.match(value):
                raise OrderError("Неверный логин Steam.", "invalid_field")
        elif key == "currency" and product["kind"] == "steam_topup":
            value = value.upper()
            if value not in product["supplier_ref"].get("rates", {}):
                raise OrderError("Валюта: USD, RUB, KZT или UAH.", "invalid_field")
        elif key == "invite_url":
            from .steam_gifts import INVITE_RE
            if not INVITE_RE.match(value):
                raise OrderError("Ссылка-приглашение Steam должна быть вида https://s.team/p/…/…", "invalid_field")
        elif key in ("app_id", "sub_id"):
            if not value.isdigit():
                raise OrderError(f"{key} — число.", "invalid_field")
        elif key == "region" and product["kind"] == "steam_gift":
            from .steam_gifts import REGION_RE
            if not REGION_RE.match(value):
                raise OrderError("Неверный регион.", "invalid_field")
        elif spec.get("options"):
            if value not in spec["options"]:
                raise OrderError(f"Поле «{spec.get('label') or key}»: выберите один из вариантов.", "invalid_field")
        elif spec.get("regex"):
            try:
                ok = re.search(spec["regex"], value) is not None
            except re.error:
                ok = True   # формат от поставщика не разобрали — не мешаем заказу, проверит поставщик
            if not ok:
                raise OrderError(f"Поле «{spec.get('label') or key}» заполнено в неверном формате.", "invalid_field")
        elif key == "amount":
            try:
                amount = to_decimal(value.replace(",", "."))
            except MoneyError:
                raise OrderError("Сумма — число.", "invalid_field") from None
            if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
                raise OrderError("Сумма — положительное число, не больше 2 знаков после точки.", "invalid_field")
            value = f"{amount:.2f}"
        result[key] = value
    return result


def _units(product: dict[str, Any], qty: int, fields: dict[str, str]) -> Decimal:
    """Сколько «единиц» закупаем: штук/звёзд, а для Steam — долларов, зачисляемых на аккаунт."""
    if product["kind"] != "steam_topup":
        return Decimal(qty)
    ref = product["supplier_ref"]
    rate = to_decimal(ref["rates"][fields["currency"]])
    usd = to_decimal(fields["amount"]) / rate
    lo, hi = to_decimal(ref.get("min_usd", "0.5")), to_decimal(ref.get("max_usd", "1000"))
    if not lo <= usd <= hi:
        cur = fields["currency"]
        raise OrderError(
            f"Сумма: от {lo * rate:.2f} до {hi * rate:.2f} {cur}.", "invalid_quantity"
        )
    return usd


def _clean_quantity(product: dict[str, Any], quantity: Any) -> int:
    if product["max_qty"] <= 1 and product["min_qty"] <= 1:
        return 1
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        raise OrderError("quantity — целое число.", "invalid_quantity") from None
    if not product["min_qty"] <= qty <= product["max_qty"]:
        raise OrderError(
            f"Количество: от {product['min_qty']} до {product['max_qty']}.", "invalid_quantity"
        )
    if product["stock"] is not None and qty > product["stock"]:
        raise OrderError(f"В наличии только {product['stock']} шт.", "out_of_stock", 409)
    return qty


# ── Создание ────────────────────────────────────────────────


def quote(config: Config, user: sqlite3.Row, product: dict[str, Any], units: Decimal | int) -> dict[str, Any]:
    unit = apply_markup(to_decimal(product["base_price"]), accounts.markup_for(user, config, product["kind"]))
    return {"unit_price": unit, "total_micro": order_total_micro(unit, units)}


# Сомони → доллары при пополнении и смена курса дают «копеечную» нехватку: на экране 15.61 с. и цена 15.61 с.,
# а в долларах не хватает сотой цента. Такую разницу (до 1 цента ≈ 0.1 с.) покрываем сами — только сайт и бот.
ROUNDING_MICRO = 100
ROUNDING_SOURCES = ("panel", "shopbot")


def rounding_gap(balance_micro: int, total_micro: int, source: str) -> int:
    """Сколько добавить на баланс, чтобы хватило (0 — не нужно или нехватка настоящая)."""
    gap = total_micro - balance_micro
    return gap if source in ROUNDING_SOURCES and 0 < gap <= ROUNDING_MICRO else 0


def create_order(
    conn: sqlite3.Connection,
    config: Config,
    supplier: Supplier,
    user: sqlite3.Row,
    *,
    product_id: str,
    quantity: Any = 1,
    fields: dict[str, Any] | None = None,
    client_idem_key: str | None = None,
    source: str = "api",
) -> tuple[sqlite3.Row, bool]:
    """Возвращает (заказ, повтор_ли). Повтор — тот же Idempotency-Key, новый заказ не создан."""
    if user["status"] != "active":
        raise OrderError("Аккаунт не активирован. Дождитесь одобрения.", "account_inactive", 403)
    product = get_product(conn, product_id)
    if product is None:
        raise OrderError("Товар не найден или недоступен.", "product_not_found", 404)
    qty = _clean_quantity(product, quantity)
    clean = _clean_fields(product, fields)
    fields_json = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    if client_idem_key is not None:
        client_idem_key = client_idem_key.strip()[:255] or None

    # Ключ повтора живёт в пространстве своего API-ключа: у клиента может быть хоть сто ботов,
    # и у каждого свой «заказ №1» — чужие ключи не должны сталкиваться («idempotency_key_reused»).
    api_key_id = user["api_key_id"] if "api_key_id" in user.keys() else None
    raw_key = client_idem_key
    if client_idem_key and api_key_id:
        client_idem_key = f"k{api_key_id}:{client_idem_key}"[:255]

    if client_idem_key:
        existing = _by_client_key(conn, user["id"], client_idem_key)
        if existing is None and raw_key != client_idem_key:
            # Заказ, созданный до разделения ключей, — повтор того же бота находит его по-старому
            existing = conn.execute("SELECT * FROM orders WHERE user_id = ? AND client_idem_key = ? "
                                    "AND api_key_id = ?", (user["id"], raw_key, api_key_id)).fetchone()
        if existing is not None:
            return _replay(existing, product_id, qty, fields_json), True

    display = _display_name(product, qty, clean)
    if product["kind"] == "steam_gift":
        from . import steam_gifts
        edition, units = steam_gifts.resolve(supplier, clean)
        display = f"Steam Gift — {edition} ({clean['region']})"
    else:
        units = _units(product, qty, clean)
    if product["kind"] == "steam_topup":
        _check_steam_login(supplier, clean["steam_login"])
    if product["kind"] == "topup":
        _check_account(supplier, product, clean)
    q = quote(config, user, product, units)
    cost_micro = order_total_micro(to_decimal(product["base_price"]), units)
    ts = db.now()
    try:
        with db.tx(conn):
            cur = conn.execute(
                """INSERT INTO orders (user_id, product_id, kind, product_name, quantity, fields_json,
                       unit_price, total_micro, cost_micro, status, supplier_idem_key, idempotent_supply,
                       client_idem_key, source, created_at, updated_at, api_key_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], product["id"], product["kind"], display, qty, fields_json,
                 fmt_unit(q["unit_price"]), q["total_micro"], cost_micro, f"dx-{uuid.uuid4()}",
                 _supply_idempotent(supplier, product), client_idem_key, source, ts, ts,
                 api_key_id),
            )
            order_id = int(cur.lastrowid)
            public_id = f"dx-{order_id}"
            conn.execute("UPDATE orders SET public_id = ? WHERE id = ?", (public_id, order_id))
            balance = conn.execute("SELECT balance_micro FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
            gap = rounding_gap(balance, q["total_micro"], source)
            if gap:
                accounts.post_ledger(conn, user["id"], gap, "Округление курса сомони", order_id=order_id)
            accounts.post_ledger(conn, user["id"], -q["total_micro"], f"Заказ {public_id}", order_id=order_id)
    except accounts.InsufficientBalance as exc:
        raise OrderError(
            f"Недостаточно средств: нужно ${fmt(exc.need)}, на балансе ${fmt(exc.have)}.",
            "insufficient_balance", 402,
        ) from None
    except sqlite3.IntegrityError:
        # Два одинаковых запроса пришли одновременно — второй получает первый заказ.
        existing = _by_client_key(conn, user["id"], client_idem_key) if client_idem_key else None
        if existing is None:
            raise
        return _replay(existing, product_id, qty, fields_json), True

    try:
        from . import dcoin
        dcoin.on_created(conn, order_id)      # D-коины сразу в истории — «ждут выполнения»
    except Exception:  # noqa: BLE001 — монеты не должны мешать заказу
        log.exception("D-коины при покупке %s", order_id)
    order = get_order_row(conn, order_id)
    _send_to_supplier(conn, supplier, order, product)
    return get_order_row(conn, order_id), False


def _check_steam_login(supplier: Supplier, login: str) -> None:
    """До списания денег: можно ли пополнить этот аккаунт."""
    try:
        ok = supplier.check_steam_login(login)
    except SupplierRejected as exc:
        raise OrderError(f"Проверка логина Steam: {exc}", "invalid_field") from None
    except SupplierUnavailable:
        raise OrderError("Не удалось проверить логин Steam. Попробуйте через минуту.", "supplier_unavailable",
                         503) from None
    if not ok:
        raise OrderError("Этот аккаунт Steam нельзя пополнить. Проверьте логин (не никнейм).",
                         "steam_login_invalid")


def _check_account(supplier: Supplier, product: dict[str, Any], fields: dict[str, str]) -> None:
    """До списания денег: существует ли аккаунт игрока. Если поставщик не ответил — не мешаем заказу."""
    from . import account_check
    if not fields or not account_check.can_check(supplier, product):
        return
    result = account_check.check(supplier, product, fields)
    if result["valid"] is False and result.get("strict", True):
        raise OrderError(f"Аккаунт не найден — проверьте ID. {result.get('message') or ''}".strip(),
                         "account_not_found")


def _display_name(product: dict[str, Any], qty: int, fields: dict[str, str] | None = None) -> str:
    if product["kind"] == "steam_topup" and fields:
        return f"Steam {fields['amount']} {fields['currency']}"
    if product["kind"] == "telegram_stars":
        return f"Telegram Stars {qty}"
    if product["kind"] in ("gift_card", "game_key") and qty > 1:
        return f"{product['name']} × {qty}"
    if product["category_name"] and product["category_name"] not in product["name"]:
        return f"{product['category_name']} — {product['name']}"
    return product["name"]


def _supply_idempotent(supplier: Supplier, product: dict[str, Any]) -> int:
    """1 — создание заказа можно безопасно повторить (тот же ключ у поставщика). У CoinDrop нельзя."""
    if (product.get("supplier_ref") or {}).get("provider") == "coindrop":
        return 0
    return 1 if supplier.is_idempotent(product["kind"]) else 0


def _by_client_key(conn: sqlite3.Connection, user_id: int, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND client_idem_key = ?", (user_id, key)
    ).fetchone()


def _replay(existing: sqlite3.Row, product_id: str, qty: int, fields_json: str) -> sqlite3.Row:
    if existing["product_id"] != product_id or existing["quantity"] != qty or existing["fields_json"] != fields_json:
        raise OrderError(
            "Этот Idempotency-Key уже использован для другого заказа.", "idempotency_key_reused", 409
        )
    return existing


def _send_to_supplier(
    conn: sqlite3.Connection, supplier: Supplier, order: sqlite3.Row, product: dict[str, Any] | None = None,
    *, from_queue: bool = False,
) -> None:
    from . import supplier_queue
    if product is None:
        product = get_product(conn, order["product_id"], for_sale=False)
        if product is None:
            _to_attention(conn, order["id"], "Товар пропал из каталога — проверьте вручную.")
            return
    provider = supplier_queue.provider_for(supplier, product)
    if not from_queue and supplier_queue.should_queue(conn, provider):
        # У поставщика нет денег (или перед нами очередь) — запрос не отправляем, заказ ждёт своей очереди
        supplier_queue.enqueue(conn, order["id"], provider)
        return
    conn.execute(
        "UPDATE orders SET supplier_attempts = supplier_attempts + 1, updated_at = ? WHERE id = ?",
        (db.now(), order["id"]),
    )
    try:
        result = supplier.create_order(
            product, order["quantity"], json.loads(order["fields_json"]), order["supplier_idem_key"]
        )
    except SupplierRejected as exc:
        if exc.http_status == 409 or "duplicate" in str(exc).lower():
            # Конфликт/дубликат: заказ у поставщика, возможно, уже создан прошлой попыткой.
            # Деньги не возвращаем вслепую — иначе клиент получит и товар, и возврат.
            _to_attention(conn, order["id"], f"Поставщик ответил «конфликт/дубликат»: {exc}. Проверьте заказ в "
                                             "кабинете поставщика: есть — отметьте выполненным, нет — верните деньги.")
            return
        if supplier_queue.is_no_funds(exc) and (exc.code == "insufficient_balance"
                                                or not supplier_queue.has_funds_for(supplier, provider, order)):
            # Кончились деньги у поставщика — заказ не отменяем: в очередь, уйдёт, когда пополним
            log.warning("поставщик %s: нет денег, %s в очередь", provider, order["public_id"])
            supplier_queue.start_hold(conn, provider)
            supplier_queue.enqueue(conn, order["id"], provider)
            return
        log.warning("поставщик отказал по %s: %s", order["public_id"], exc)
        fail_and_refund(conn, order["id"], _client_error(exc))
        return
    except SupplierUnavailable as exc:
        log.warning("поставщик не ответил по %s: %s", order["public_id"], exc)
        conn.execute("UPDATE orders SET error = ? WHERE id = ?", (f"поставщик: {exc}", order["id"]))
        return
    except Exception as exc:  # неожиданное — не теряем заказ, отдаём админу
        log.exception("ошибка при отправке %s", order["public_id"])
        _to_attention(conn, order["id"], f"Ошибка при отправке поставщику: {exc}")
        return
    conn.execute(
        "UPDATE orders SET supplier_order_id = ?, supplier_status = ?, error = NULL, updated_at = ? WHERE id = ?",
        (result.order_id, result.raw_status, db.now(), order["id"]),
    )
    if result.order_id is None:
        _to_attention(conn, order["id"], "Поставщик принял заказ, но не вернул его номер.")
    elif result.status == "partial":
        _to_attention(conn, order["id"], f"Поставщик выполнил заказ частично ({result.raw_status}) — проверьте.")
    elif result.status == "failed":
        fail_and_refund(conn, order["id"], result.message or "Поставщик отклонил заказ.")
    elif result.status == "completed":
        complete(conn, order["id"], result.delivery, result.raw_status)   # CoinDrop часто выдаёт сразу


def _client_error(exc: SupplierRejected) -> str:
    # Не показываем клиенту, что у нас кончились деньги у поставщика.
    if exc.code == "insufficient_balance" or "баланс" in str(exc).lower() or "balance" in str(exc).lower():
        return "Товар временно недоступен. Деньги возвращены на баланс."
    return f"Поставщик отклонил заказ: {exc}"


# ── Итоги заказа ─────────────────────────────────────────────


def fail_and_refund(conn: sqlite3.Connection, order_id: int, reason: str, *, by_admin: int | None = None) -> bool:
    """Отменить заказ и вернуть деньги. Возврат делается ровно один раз."""
    with db.tx(conn):
        changed = conn.execute(
            "UPDATE orders SET status = 'failed', error = ?, updated_at = ?, completed_at = ?, "
            "webhook_state = 'pending' WHERE id = ? AND status IN ('processing', 'attention')",
            (reason[:500], db.now(), db.now(), order_id),
        ).rowcount
        if not changed:
            return False
        order = get_order_row(conn, order_id)
        accounts.post_ledger(
            conn, order["user_id"], order["total_micro"], f"Возврат за {order['public_id']}",
            order_id=order_id, created_by=by_admin,
        )
        from .notify import notify
        notify(conn, None, order["user_id"],
               f"Заказ {order['public_id']} не выполнен, ${fmt(order['total_micro'])} вернулись на баланс.",
               f"/panel/orders/{order['public_id']}")
    try:
        from . import dcoin
        dcoin.on_refund(conn, order_id)   # возврат — цена D-коина чуть вниз
    except Exception:  # noqa: BLE001 — график не должен мешать возврату
        log.exception("D-коин: возврат %s", order_id)
    return True


def complete(conn: sqlite3.Connection, order_id: int, delivery: dict[str, Any] | None, raw_status: str = "") -> bool:
    changed = conn.execute(
        "UPDATE orders SET status = 'completed', delivery_json = ?, supplier_status = ?, error = NULL, "
        "updated_at = ?, completed_at = ?, webhook_state = 'pending' "
        "WHERE id = ? AND status IN ('processing', 'attention')",
        (json.dumps(delivery or {}, ensure_ascii=False), raw_status or "completed", db.now(), db.now(), order_id),
    ).rowcount
    if changed:
        try:
            from . import referrals
            referrals.award(conn, order_id)   # бонус пригласившему — раз за заказ
        except Exception:  # noqa: BLE001 — бонус не должен ломать выдачу заказа
            log.exception("реферальный бонус за заказ %s", order_id)
        try:
            from . import dcoin
            dcoin.award(conn, order_id)       # D-коины клиенту и доля прибыли в копилку
        except Exception:  # noqa: BLE001
            log.exception("D-коины за заказ %s", order_id)
    return bool(changed)


def _to_attention(conn: sqlite3.Connection, order_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE orders SET status = 'attention', error = ?, updated_at = ? WHERE id = ? AND status = 'processing'",
        (reason[:500], db.now(), order_id),
    )


def _age_seconds(ts: str) -> float:
    created = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def refresh(conn: sqlite3.Connection, supplier: Supplier, order: sqlite3.Row, *, from_worker: bool = True) -> None:
    """Довести заказ до конца: доотправить поставщику или спросить статус."""
    if order["status"] != "processing":
        return
    if (order["supplier_status"] or "").startswith("queued_funds"):
        return   # ждёт денег у поставщика — отправит очередь (supplier_queue.dispatch)
    if order["supplier_order_id"] is None:
        if not from_worker or _age_seconds(order["updated_at"]) < IN_FLIGHT_SECONDS:
            return
        if not order["idempotent_supply"]:
            _to_attention(
                conn, order["id"],
                "Связь с поставщиком прервалась при создании. Проверьте заказ в кабинете поставщика: "
                "если он там есть — отметьте выполненным, если нет — верните деньги.",
            )
            return
        if order["supplier_attempts"] >= MAX_CREATE_ATTEMPTS:
            _to_attention(conn, order["id"], "Поставщик не отвечает после нескольких попыток.")
            return
        _send_to_supplier(conn, supplier, order)
        return
    try:
        result = supplier.get_order(order["supplier_order_id"])
    except SupplierRejected as exc:
        _to_attention(conn, order["id"], f"Поставщик не нашёл заказ {order['supplier_order_id']}: {exc}")
        return
    except SupplierUnavailable:
        return
    conn.execute(
        "UPDATE orders SET supplier_status = ?, updated_at = ? WHERE id = ?",
        (result.raw_status, db.now(), order["id"]),
    )
    if result.status == "completed":
        complete(conn, order["id"], result.delivery, result.raw_status)
    elif result.status == "partial":
        _to_attention(conn, order["id"], f"Поставщик выполнил заказ частично ({result.raw_status}) — проверьте.")
    elif result.status == "failed":
        fail_and_refund(conn, order["id"], result.message or f"Поставщик: {result.raw_status}")
    elif _age_seconds(order["created_at"]) > STUCK_HOURS * 3600:
        _to_attention(conn, order["id"], f"Заказ в работе у поставщика больше {STUCK_HOURS} ч.")


def refresh_if_stale(conn: sqlite3.Connection, supplier: Supplier, order: sqlite3.Row) -> sqlite3.Row:
    """При чтении заказа клиентом — быстро спросить статус, не чаще раза в 5 секунд."""
    if order["status"] == "processing" and order["supplier_order_id"] and \
            _age_seconds(order["updated_at"]) >= REFRESH_ON_READ_SECONDS:
        refresh(conn, supplier, order, from_worker=False)
        return get_order_row(conn, order["id"])
    return order


def recheck_attention(conn: sqlite3.Connection, supplier: Supplier, limit: int = 20) -> int:
    """Заказы «на проверке» с номером у поставщика — спросить ещё раз.

    Раньше они ждали админа вечно: поставщик уже вернул деньги, а клиент видел
    «в обработке». Теперь: у поставщика возврат — возвращаем и мы, выполнен — выполняем.
    """
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'attention' AND supplier_order_id IS NOT NULL "
        "ORDER BY updated_at LIMIT ?", (limit,)).fetchall()
    done = 0
    for order in rows:
        try:
            result = supplier.get_order(order["supplier_order_id"])
        except (SupplierRejected, SupplierUnavailable):
            conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?", (db.now(), order["id"]))
            continue
        except Exception:
            log.exception("перепроверка: заказ %s", order["public_id"])
            continue
        conn.execute("UPDATE orders SET supplier_status = ?, updated_at = ? WHERE id = ?",
                     (result.raw_status, db.now(), order["id"]))
        if result.status == "completed":
            complete(conn, order["id"], result.delivery, result.raw_status)
            done += 1
        elif result.status == "failed":
            fail_and_refund(conn, order["id"], result.message or f"Поставщик: {result.raw_status}")
            done += 1
    return done


def process_pending(conn: sqlite3.Connection, supplier: Supplier, limit: int = 50) -> int:
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'processing' AND COALESCE(supplier_status, '') NOT LIKE 'queued_funds%' "
        "ORDER BY updated_at LIMIT ?", (limit,)
    ).fetchall()
    for order in rows:
        try:
            refresh(conn, supplier, order)
        except Exception:
            log.exception("воркер: заказ %s", order["public_id"])
    return len(rows)


# ── Действия админа ──────────────────────────────────────────


def admin_complete(conn: sqlite3.Connection, order_id: int, note: str) -> bool:
    return complete(conn, order_id, {"message": note.strip() or "Выполнено"}, "manual")


def admin_recheck(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute(
        "UPDATE orders SET status = 'processing', supplier_attempts = 0, updated_at = ? "
        "WHERE id = ? AND status = 'attention' AND supplier_order_id IS NOT NULL",
        (db.now(), order_id),
    )


# ── Чтение ───────────────────────────────────────────────────


def get_order_row(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def find_user_order(conn: sqlite3.Connection, user_id: int, public_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND public_id = ?", (user_id, public_id.strip())
    ).fetchone()


def client_status(status: str) -> str:
    # «attention» — наша внутренняя кухня; клиент видит «в обработке».
    return "processing" if status == "attention" else status


def public_view(order: sqlite3.Row) -> dict[str, Any]:
    status = client_status(order["status"])
    return {
        "order_id": order["public_id"],
        "status": status,
        "kind": order["kind"],
        "product_id": order["product_id"],
        "product_name": order["product_name"],
        "quantity": order["quantity"],
        "fields": json.loads(order["fields_json"] or "{}"),
        "unit_price_usd": order["unit_price"],
        "total_usd": fmt(order["total_micro"]),
        "delivery": json.loads(order["delivery_json"]) if order["delivery_json"] and status == "completed" else None,
        "error": order["error"] if status == "failed" else None,
        "created_at": order["created_at"],
        "completed_at": order["completed_at"],
    }


def _local_start(conn: sqlite3.Connection, days: int) -> tuple[str, Any]:
    """Начало периода: 00:00 по местному времени, days суток назад включая сегодня (1 — сегодня)."""
    from . import periods, timez
    tz = timez.zone(timez.site_zone_name(conn))
    start = periods.midnight(tz) - timedelta(days=max(days, 1) - 1)
    start = datetime(start.year, start.month, start.day, tzinfo=tz)   # полночь, даже если между — смена часов
    return periods.iso(start), tz


def stats(conn: sqlite3.Connection, since_days: int = 1) -> dict[str, Any]:
    """Выполненные заказы с 00:00 (местное время): 1 — сегодня, 30 — сегодня и 29 суток до него.
    В полночь «сегодня» начинается с нуля."""
    since, _ = _local_start(conn, since_days)
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_micro),0) AS revenue, COALESCE(SUM(total_micro - cost_micro),0) "
        "AS profit FROM orders WHERE status = 'completed' AND COALESCE(completed_at, created_at) >= ?",
        (since,),
    ).fetchone()
    return {"orders": row["n"], "revenue": row["revenue"], "profit": row["profit"]}


def daily(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    """Выручка и прибыль по дням (сутки 00:00 → 23:59 по местному времени), включая пустые дни."""
    since, tz = _local_start(conn, days)
    buckets: dict[str, dict[str, int]] = {}
    for r in conn.execute(
            "SELECT COALESCE(completed_at, created_at) AS t, total_micro, cost_micro FROM orders "
            "WHERE status = 'completed' AND COALESCE(completed_at, created_at) >= ?", (since,)):
        try:
            ts = datetime.strptime(r["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        b = buckets.setdefault(ts.astimezone(tz).date().isoformat(), {"n": 0, "revenue": 0, "profit": 0})
        b["n"] += 1
        b["revenue"] += r["total_micro"]
        b["profit"] += r["total_micro"] - r["cost_micro"]
    start = datetime.strptime(since, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz).date()
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        b = buckets.get(d, {"n": 0, "revenue": 0, "profit": 0})
        out.append({"day": d, "orders": b["n"], "revenue": b["revenue"], "profit": b["profit"]})
    return out


def top_clients(conn: sqlite3.Connection, days: int = 30, limit: int = 5) -> list[sqlite3.Row]:
    since, _ = _local_start(conn, days)
    return conn.execute(
        "SELECT u.id, u.login, u.project, COUNT(*) AS n, SUM(o.total_micro) AS revenue, "
        "SUM(o.total_micro - o.cost_micro) AS profit FROM orders o JOIN users u ON u.id = o.user_id "
        "WHERE o.status = 'completed' AND o.created_at >= ? GROUP BY u.id ORDER BY revenue DESC LIMIT ?",
        (since, limit)).fetchall()


def with_images(conn: sqlite3.Connection, rows: list) -> list[dict[str, Any]]:
    """Заказы для списков-карточек: картинка игры, название игры и получатель одной строкой."""
    ids = list({r["product_id"] for r in rows})
    pics: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for p in conn.execute(f"SELECT id, image_url, category_name FROM products WHERE id IN "
                              f"({','.join('?' * len(chunk))})", chunk):
            pics[p["id"]] = p
    out = []
    for r in rows:
        d, p = dict(r), pics.get(r["product_id"])
        d["image_url"] = p["image_url"] if p else None
        d["game"] = p["category_name"] if p else ""
        try:
            fields = json.loads(r["fields_json"] or "{}")
        except (ValueError, TypeError):
            fields = {}
        d["recipient"] = ", ".join(str(v) for v in fields.values() if v not in (None, ""))
        out.append(d)
    return out

