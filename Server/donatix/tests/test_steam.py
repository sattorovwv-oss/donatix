import json
from decimal import ROUND_CEILING, Decimal

import httpx
from conftest import balance, csrf_of, web_login

from donatix import orders
from donatix.suppliers.fazer import FazerSupplier


def _steam(client, h, amount="500", currency="RUB", login="good_login", key=None):
    headers = dict(h)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/api/v1/orders", headers=headers, json={
        "product_id": "steam-topup",
        "fields": {"steam_login": login, "currency": currency, "amount": amount},
    })


def test_steam_price_uses_own_markup_and_rate(client, conn, shop, supplier):
    item = client.get("/api/v1/products/steam-topup", headers=shop["h"]).json()["product"]
    assert item["price_usd"] == "0.989625"  # 0.975 × 1.015 — своя наценка для Steam, а не 8%
    assert item["rates"]["RUB"] == "92.5"
    assert item["discount_percent"].startswith("1.03")

    r = _steam(client, shop["h"])
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    usd = Decimal("500") / Decimal("92.5")
    expected = (usd * Decimal("0.989625") * 10000).quantize(Decimal(1), rounding=ROUND_CEILING)
    assert balance(conn, shop["id"]) == 1_000_000 - int(expected)
    assert order["product_name"] == "Steam 500.00 RUB"
    assert order["fields"] == {"amount": "500.00", "currency": "RUB", "steam_login": "good_login"}
    orders.process_pending(conn, supplier)
    got = client.get(f"/api/v1/orders/{order['order_id']}", headers=shop["h"]).json()["order"]
    assert got["status"] == "completed"


def test_steam_bad_login_not_charged(client, conn, shop):
    r = _steam(client, shop["h"], login="bad_login")
    assert r.status_code == 400 and r.json()["code"] == "steam_login_invalid"
    assert balance(conn, shop["id"]) == 1_000_000
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_steam_validation(client, shop):
    assert _steam(client, shop["h"], currency="EUR").json()["code"] == "invalid_field"
    assert _steam(client, shop["h"], amount="10.555").json()["code"] == "invalid_field"
    assert _steam(client, shop["h"], amount="-5").json()["code"] == "invalid_field"
    r = _steam(client, shop["h"], amount="5", currency="RUB")  # ~$0.05 — меньше минимума
    assert r.json()["code"] == "invalid_quantity" and "RUB" in r.json()["error"]


def test_steam_panel_page(client, conn):
    from donatix import accounts, db
    uid = accounts.create_user(conn, email="s@example.com", login="steamer", password="password123",
                               status="active")
    with db.tx(conn):
        accounts.post_ledger(conn, uid, 500_000, "Пополнение")
    token = web_login(client, "s@example.com", "password123")
    page = client.get("/panel/buy/steam-topup").text
    assert "Пополнить Steam" in page and "KZT" in page and "Получит в USD" in page
    r = client.post("/panel/buy/steam-topup", data={
        "csrf": csrf_of(page) or token, "idem": "abc", "quantity": "1",
        "field_steam_login": "good_login", "field_amount": "10", "field_currency": "USD"}, follow_redirects=False)
    assert r.status_code == 303 and "/panel/orders/dx-" in r.headers["location"]


def test_fazer_steam_catalog_and_order():
    seen = {}

    def handler(req: httpx.Request):
        p = req.url.path.removeprefix("/api/v2")
        if p == "/steam-topup/rates":
            return httpx.Response(200, json={"ok": True, "base": "USD",
                                             "rates": {"USD": 1, "RUB": 92.5, "UAH": 41.2, "KZT": 520}})
        if p == "/steam-topup/check-login":
            return httpx.Response(200, json={"ok": True, "can_refill": json.loads(req.content)["steamLogin"] == "ok"})
        if p == "/steam-topup/order":
            seen["body"] = json.loads(req.content)
            seen["idem"] = req.headers.get("Idempotency-Key")
            return httpx.Response(200, json={"ok": True, "order": {"id": "ord-1", "status": "processing"}})
        return httpx.Response(403, json={"ok": False, "error": "no"})

    s = FazerSupplier("K", transport=httpx.MockTransport(handler), catalog_pause=0, steam_discount=Decimal("2.5"))
    items = {p.id: p for p in s.fetch_catalog()}
    steam = items["steam-topup"]
    assert steam.base_price == Decimal("0.975") and steam.supplier_ref["rates"]["RUB"] == "92.5"
    assert s.check_steam_login("ok") is True and s.check_steam_login("nope") is False
    s.create_order({"kind": "steam_topup", "supplier_ref": {}}, 1,
                   {"steam_login": "ok", "currency": "RUB", "amount": "500.00"}, "idem-9")
    assert seen == {"body": {"steamLogin": "ok", "currency": "RUB", "amount": "500.00"}, "idem": "idem-9"}
    assert s.is_idempotent("steam_topup")
