import json
from decimal import Decimal

import httpx
import pytest

from donatix.suppliers.base import SupplierRejected
from donatix.suppliers.vendoria import VendoriaSupplier

SERVICES = [{"id": 1, "name": "Standoff 2", "icon": "so.webp", "isClosed": False},
            {"id": 2, "name": "Clash of Clans", "icon": "coc.webp", "isClosed": False},
            {"id": 3, "name": "Roblox", "icon": "rb.webp", "isClosed": False}]
CATEGORIES = [{"id": 10, "name": "Gold", "serviceId": 1}, {"id": 20, "name": "Gems", "serviceId": 2},
              {"id": 30, "name": "Robux", "serviceId": 3}]
FORMS = [
    {"id": 5, "name": "По ID", "serviceId": 1, "hasRequest": False, "hasGooglePrompt": False,
     "fields": [{"id": "player-id", "type": "text", "required": True, "label": "ID игрока", "regex": "^\\d{6,12}$"},
                {"id": "note", "type": "text", "required": False, "label": "Комментарий"}]},
    {"id": 6, "name": "Supercell ID", "serviceId": 2, "hasRequest": True, "hasGooglePrompt": False,   # нужен код
     "fields": [{"id": "email", "type": "text", "required": True, "label": "Email"}]},
    {"id": 7, "name": "Тег игрока", "serviceId": 2, "hasRequest": False, "hasGooglePrompt": False,
     "fields": [{"id": "tag", "type": "text", "required": True, "label": "Тег"},
                {"id": "region", "type": "select", "required": True, "label": "Регион", "options": ["EU", "RU"]}]},
    {"id": 8, "name": "Скриншот", "serviceId": 1, "hasRequest": False, "hasGooglePrompt": False,
     "fields": [{"id": "shot", "type": "image", "required": True, "label": "Скриншот"}]},
]
PRODUCTS = [{"id": 100, "name": "100 Gold", "icon": "g.webp", "categoryId": 10, "prices": {"5": 1.2, "8": 1.0}},
            {"id": 200, "name": "80 Gems", "icon": None, "categoryId": 20, "prices": {"6": 0.9, "7": 1.1}},
            {"id": 300, "name": "400 Robux", "icon": "r.webp", "categoryId": 30, "prices": {"9": 5}}]


def _supplier(state):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Shop 0:secret"
        path = request.url.path
        if path == "/api/services":
            return httpx.Response(200, json=SERVICES)
        if path == "/api/categories":
            return httpx.Response(200, json=CATEGORIES)
        if path == "/api/forms":
            return httpx.Response(200, json=FORMS)
        if path == "/api/products":
            return httpx.Response(200, json=PRODUCTS)
        if path == "/api/shop/balance":
            return httpx.Response(200, json={"balance": state["balance"]})
        if path == "/api/orders" and request.method == "POST":
            body = json.loads(request.content)
            state["created"].append(body)
            if state["balance"] < 1:
                return httpx.Response(402, json={"message": "Insufficient balance"})
            return httpx.Response(201, json={"id": 555, "status": "pending", "cdkey": None})
        if path == "/api/orders/555":
            return httpx.Response(200, json=state["order"])
        return httpx.Response(404, json={"message": "not found"})
    return VendoriaSupplier("0:secret", "https://v.test", only_games=["Standoff 2", "Clash of Clans"],
                            transport=httpx.MockTransport(handler))


def test_catalog_only_standoff_and_coc_and_usable_forms():
    v = _supplier({"balance": 10, "created": []})
    items = {p.id: p for p in v.fetch_catalog()}
    assert set(items) == {"vd-100-5", "vd-200-7"}          # Roblox не берём; формы с кодом и скриншотом — нет
    so = items["vd-100-5"]
    assert so.category_name == "Standoff 2" and so.category_id == "vd_1" and so.base_price == Decimal("1.2")
    assert [f["key"] for f in so.fields] == ["player-id"]  # необязательное поле не спрашиваем
    assert so.image_url == "https://v.test/api/icons/g.webp"
    coc = items["vd-200-7"]
    assert coc.fields[1]["options"] == ["EU", "RU"] and coc.image_url.endswith("coc.webp")


def test_order_flow_and_no_money():
    state = {"balance": 10, "created": [], "order": {"id": 555, "status": "completed", "cdkey": None}}
    v = _supplier(state)
    product = {"supplier_ref": {"provider": "vendoria", "product_id": 100, "form_id": 5},
               "fields": [{"key": "player-id", "label": "ID"}]}
    r = v.create_order(product, 1, {"player-id": "12345678"}, "dx-abc")
    assert r.order_id == "vd:555" and r.status == "processing"
    assert state["created"][0] == {"productId": 100, "quantity": 1, "formId": 5,
                                   "deliveryData": {"player-id": "12345678"}, "shopOrderId": "dx-abc"}
    done = v.get_order("vd:555")
    assert done.status == "completed" and done.delivery == {"message": "Зачислено на аккаунт"}
    state["order"] = {"id": 555, "status": "cancelled", "cancellationReason": {"type": "wrong_field", "field": "ID"}}
    failed = v.get_order("vd:555")
    assert failed.status == "failed" and "«ID»" in failed.message
    state["balance"] = 0
    with pytest.raises(SupplierRejected) as exc:
        v.create_order(product, 1, {"player-id": "12345678"}, "dx-abd")
    assert exc.value.code == "insufficient_balance"           # → очередь «нет денег у поставщика»
    assert v.balance() == Decimal("0") and v.handles_order_id("vd:1") and not v.handles_order_id("cd:1")


def test_select_and_format_checked_before_charging(conn):
    from donatix import orders
    product = {"kind": "topup", "supplier_ref": {}, "fields": [
        {"key": "tag", "label": "Тег", "regex": "^#[A-Z0-9]{3,12}$"},
        {"key": "region", "label": "Регион", "options": ["EU", "RU"]}]}
    assert orders._clean_fields(product, {"tag": "#ABC123", "region": "EU"}) == {"tag": "#ABC123", "region": "EU"}
    with pytest.raises(orders.OrderError):
        orders._clean_fields(product, {"tag": "abc", "region": "EU"})
    with pytest.raises(orders.OrderError):
        orders._clean_fields(product, {"tag": "#ABC123", "region": "US"})


def test_popular_prefers_vendoria_for_standoff_and_coc(conn):
    from donatix import db, popular
    rows = [("cd-standoff-1", "cd_standoff", "Standoff 2"), ("cd-standoff-2", "cd_standoff", "Standoff 2"),
            ("vd-100-5", "vd_1", "Standoff 2"), ("vd-200-7", "vd_2", "Clash of Clans")]
    for pid, cat, name in rows:
        conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, fields_json, "
                     "supplier_ref_json, updated_at) VALUES (?, 'topup', ?, ?, 'x', '1', '[]', '{}', ?)",
                     (pid, cat, name, db.now()))
    pins = {p["key"]: p for p in popular._pinned(conn)}
    assert pins["standoff2"]["category_id"] == "vd_1"       # у CoinDrop пакетов больше, но берём Vendoria
    assert pins["coc"]["category_id"] == "vd_2"


def test_delivery_time_notes():
    from donatix import catalog
    so = catalog.delivery_note("Standoff 2")
    assert "до 90 минут" in so and "11:00 до 23:00 по Душанбе" in so and "утром" in so
    coc = catalog.delivery_note("Clash of Clans")
    assert "20–90 минут" in coc and "утром" in coc
    assert catalog.delivery_note("Free Fire") == ""


def test_order_page_shows_delivery_time(client, conn, shop):
    from conftest import web_login
    conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, fields_json, "
                 "unit_price, total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES "
                 "('dx-so1', ?, 'vd-100-5', 'topup', 'Standoff 2 — Золото · 100 Золота', 1, '{}', '1', 10000, 9000, "
                 "'processing', 'kso1', '2026-09-30T04:21:09.000Z', '2026-09-30T04:21:09.000Z')", (shop["id"],))
    web_login(client, "shop1@example.com", "password123")
    page = client.get("/panel/orders/dx-so1").text
    assert "до 90 минут" in page and "11:00 до 23:00 по Душанбе" in page and "несколько секунд" not in page

