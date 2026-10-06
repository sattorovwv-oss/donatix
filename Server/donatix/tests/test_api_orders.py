import json

from conftest import balance, make_client

from donatix import orders


def _stars(client, h, qty=100, username="@player_one", key=None):
    headers = dict(h)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/api/v1/orders", headers=headers,
                       json={"product_id": "tg-stars", "quantity": qty, "fields": {"telegram_username": username}})


def test_auth_required(client):
    r = client.get("/api/v1/balance")
    assert r.status_code == 401
    assert r.json() == {"ok": False, "error": r.json()["error"], "code": "unauthorized"}
    assert client.get("/api/v1/balance", headers={"X-API-Key": "dx_live_nope"}).status_code == 401


def test_bearer_and_balance(client, shop):
    r = client.get("/api/v1/balance", headers={"Authorization": f"Bearer {shop['key']}"})
    assert r.json() == {"ok": True, "balance": "100.0000", "currency": "USD"}


def test_products_show_client_price_not_cost(client, shop):
    r = client.get("/api/v1/products?kind=telegram_stars", headers=shop["h"]).json()
    item = r["items"][0]
    assert item["product_id"] == "tg-stars"
    assert item["price_usd"] == "0.016605"  # 0.015375 + 8%
    assert "base_price" not in item and "supplier_ref" not in item


def test_order_happy_path(client, conn, shop, supplier):
    r = _stars(client, shop["h"], key="k1")
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert order["status"] == "processing"
    assert order["total_usd"] == "1.6605"
    assert order["fields"] == {"telegram_username": "@player_one"}
    assert balance(conn, shop["id"]) == 1_000_000 - 16605

    orders.process_pending(conn, supplier)
    r = client.get(f"/api/v1/orders/{order['order_id']}", headers=shop["h"]).json()
    assert r["order"]["status"] == "completed"
    assert r["order"]["delivery"] == {"message": "Зачислено на аккаунт"}

    txs = client.get("/api/v1/transactions", headers=shop["h"]).json()
    assert [t["amount"] for t in txs["items"]] == ["-1.6605", "100.0000"]
    assert txs["items"][0]["orderId"] == order["order_id"]


def test_idempotency_same_key_charges_once(client, conn, shop):
    a = _stars(client, shop["h"], key="same")
    b = _stars(client, shop["h"], key="same")
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["order"]["order_id"] == b.json()["order"]["order_id"]
    assert balance(conn, shop["id"]) == 1_000_000 - 16605
    c = _stars(client, shop["h"], qty=200, key="same")
    assert c.status_code == 409 and c.json()["code"] == "idempotency_key_reused"


def test_same_key_from_two_bots_of_one_client(client, conn, shop):
    """У каждого бота свой API-ключ и свой «заказ №1» — ключи не сталкиваются."""
    from donatix import accounts
    second = accounts.create_api_key(conn, shop["id"], "Бот @second", "")
    a = _stars(client, shop["h"], key="bot-1-abc")
    b = _stars(client, {"X-API-Key": second}, qty=200, key="bot-1-abc")
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["order"]["order_id"] != b.json()["order"]["order_id"]
    again = _stars(client, {"X-API-Key": second}, qty=200, key="bot-1-abc")   # повтор второго бота
    assert again.status_code == 200 and again.json()["order"]["order_id"] == b.json()["order"]["order_id"]


def test_retry_finds_order_made_before_key_scoping(client, conn, shop):
    a = _stars(client, shop["h"], key="legacy-key-1")
    oid = int(a.json()["order"]["order_id"].split("-")[1])
    conn.execute("UPDATE orders SET client_idem_key = 'legacy-key-1' WHERE id = ?", (oid,))  # как было раньше
    b = _stars(client, shop["h"], key="legacy-key-1")
    assert b.status_code == 200 and b.json()["order"]["order_id"] == a.json()["order"]["order_id"]


def test_insufficient_balance(client, conn):
    uid, key = make_client(conn, "poor", balance="1")
    r = _stars(client, {"X-API-Key": key}, qty=100)
    assert r.status_code == 402 and r.json()["code"] == "insufficient_balance"
    assert balance(conn, uid) == 10_000
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_pending_account_cannot_order(client, conn):
    _, key = make_client(conn, "newbie", status="pending")
    r = _stars(client, {"X-API-Key": key})
    assert r.status_code == 403 and r.json()["code"] == "account_inactive"


def test_validation(client, shop):
    assert _stars(client, shop["h"], username="bad name!").json()["code"] == "invalid_field"
    assert _stars(client, shop["h"], qty=10).json()["code"] == "invalid_quantity"
    r = client.post("/api/v1/orders", headers=shop["h"], json={"product_id": "tg-stars", "quantity": 100})
    assert r.json()["code"] == "missing_field"
    r = client.post("/api/v1/orders", headers=shop["h"], json={"quantity": 1})
    assert r.status_code == 400 and r.json()["code"] == "validation_error"
    r = client.post("/api/v1/orders", headers=shop["h"], json={"product_id": "nope"})
    assert r.status_code == 404


def test_supplier_rejects_refund(client, conn, shop, supplier):
    supplier.fail_next = "reject_other"          # поставщик отказал (не из-за денег) — возврат
    r = _stars(client, shop["h"])
    order = r.json()["order"]
    assert order["status"] == "failed"
    assert "Игрок не найден" in order["error"]
    assert balance(conn, shop["id"]) == 1_000_000


def test_failed_at_supplier_later_refund_once(client, conn, shop, supplier):
    r = _stars(client, shop["h"])
    supplier.fail_on_poll = True
    orders.process_pending(conn, supplier)
    orders.process_pending(conn, supplier)
    oid = r.json()["order"]["order_id"]
    assert client.get(f"/api/v1/orders/{oid}", headers=shop["h"]).json()["order"]["status"] == "failed"
    assert balance(conn, shop["id"]) == 1_000_000
    refunds = conn.execute("SELECT COUNT(*) FROM transactions WHERE note LIKE 'Возврат%'").fetchone()[0]
    assert refunds == 1


def test_timeout_on_idempotent_kind_is_retried_with_same_key(client, conn, shop, supplier, monkeypatch):
    monkeypatch.setattr(orders, "IN_FLIGHT_SECONDS", 0)
    supplier.fail_next = "unavailable_after_create"  # поставщик создал заказ, но ответ потерялся
    r = client.post("/api/v1/orders", headers=shop["h"], json={"product_id": "gc-steam-10", "quantity": 2})
    order = r.json()["order"]
    assert order["status"] == "processing"
    assert balance(conn, shop["id"]) == 1_000_000 - 2 * 113400  # 10.50 + 8% = 11.34

    orders.process_pending(conn, supplier)  # повтор с тем же ключом → тот же заказ у поставщика
    assert len(supplier._orders) == 1
    orders.process_pending(conn, supplier)
    got = client.get(f"/api/v1/orders/{order['order_id']}", headers=shop["h"]).json()["order"]
    assert got["status"] == "completed"
    assert len(got["delivery"]["codes"]) == 2


def test_timeout_on_non_idempotent_kind_goes_to_admin(client, conn, shop, supplier, monkeypatch):
    monkeypatch.setattr(orders, "IN_FLIGHT_SECONDS", 0)
    supplier.fail_next = "unavailable"
    r = _stars(client, shop["h"])
    oid = r.json()["order"]["order_id"]
    orders.process_pending(conn, supplier)
    row = conn.execute("SELECT * FROM orders WHERE public_id = ?", (oid,)).fetchone()
    assert row["status"] == "attention"
    assert row["supplier_attempts"] == 1  # второй раз НЕ отправляли — Telegram-покупка без идемпотентности
    # Клиент видит «в обработке», деньги пока не возвращены.
    assert client.get(f"/api/v1/orders/{oid}", headers=shop["h"]).json()["order"]["status"] == "processing"
    assert balance(conn, shop["id"]) == 1_000_000 - 16605
    assert orders.fail_and_refund(conn, row["id"], "не прошёл") is True
    assert orders.fail_and_refund(conn, row["id"], "повтор") is False
    assert balance(conn, shop["id"]) == 1_000_000


def test_orders_list_and_other_clients_isolated(client, conn, shop):
    _stars(client, shop["h"])
    _, other_key = make_client(conn, "other")
    mine = client.get("/api/v1/orders", headers=shop["h"]).json()
    assert mine["total"] == 1
    oid = mine["items"][0]["order_id"]
    assert client.get("/api/v1/orders", headers={"X-API-Key": other_key}).json()["total"] == 0
    assert client.get(f"/api/v1/orders/{oid}", headers={"X-API-Key": other_key}).status_code == 404


def test_me(client, conn, shop):
    r = client.get("/api/v1/me", headers=shop["h"]).json()
    assert r["login"] == "shop1" and r["tier"] == "bronze" and r["balance"] == "100.0000"


def test_rate_limit(client, app, shop):
    app.state.limiter.limits["account"] = 2
    assert client.get("/api/v1/balance", headers=shop["h"]).status_code == 200
    assert client.get("/api/v1/balance", headers=shop["h"]).status_code == 200
    r = client.get("/api/v1/balance", headers=shop["h"])
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_custom_markup(client, conn, shop):
    conn.execute("UPDATE users SET markup_override = '2' WHERE id = ?", (shop["id"],))
    item = client.get("/api/v1/products/tg-premium-3", headers=shop["h"]).json()["product"]
    assert item["price_usd"] == "12.535596"  # 12.2898 × 1.02


def test_fields_stored_as_json(client, conn, shop):
    r = client.post("/api/v1/orders", headers=shop["h"],
                    json={"product_id": "topup-pubg-60", "fields": {"player_id": " 5123456789 "}})
    assert r.status_code == 201
    row = conn.execute("SELECT fields_json, quantity FROM orders").fetchone()
    assert json.loads(row["fields_json"]) == {"player_id": "5123456789"}
    assert row["quantity"] == 1


def test_supplier_refund_words_are_recognized():
    from donatix.suppliers.base import normalize_status
    for raw in ("refunded", "refund", "Refunded_to_balance", "canceled_by_system", "returned", "expired",
                "Отменён", "Возврат"):
        assert normalize_status(raw) == "failed", raw
    for raw in ("completed", "success", "delivered", "Выполнен"):
        assert normalize_status(raw) == "completed", raw
    for raw in ("processing", "pending", "in_progress", ""):
        assert normalize_status(raw) == "processing", raw


def test_attention_order_refunded_when_supplier_refunds(client, conn, shop, supplier):
    """Заказ ушёл «на проверку» (долго висел), потом поставщик вернул деньги — возвращаем и мы."""
    from donatix.suppliers.base import SupplierOrder
    r = _stars(client, shop["h"])
    oid = r.json()["order"]["order_id"]
    conn.execute("UPDATE orders SET status = 'attention', supplier_order_id = 'fz-77' WHERE public_id = ?", (oid,))
    assert client.get(f"/api/v1/orders/{oid}", headers=shop["h"]).json()["order"]["status"] == "processing"

    supplier.get_order = lambda sid: SupplierOrder(order_id=sid, status="failed", raw_status="refunded")
    assert orders.recheck_attention(conn, supplier) == 1
    assert client.get(f"/api/v1/orders/{oid}", headers=shop["h"]).json()["order"]["status"] == "failed"
    assert balance(conn, shop["id"]) == 1_000_000
    assert orders.recheck_attention(conn, supplier) == 0            # второй раз — ничего, возврат один
    refunds = conn.execute("SELECT COUNT(*) FROM transactions WHERE note LIKE 'Возврат%'").fetchone()[0]
    assert refunds == 1
