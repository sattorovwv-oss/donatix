import json

import httpx
from conftest import balance, web_login

from donatix import orders
from donatix.suppliers.fazer import FazerSupplier

INVITE = "https://s.team/p/abcd-efgh/XYZ12345"


def _gift(client, h, sub_id="354151", region="CIS", invite=INVITE, app_id="1245620"):
    return client.post("/api/v1/orders", headers=h, json={
        "product_id": "steam-gift",
        "fields": {"app_id": app_id, "sub_id": sub_id, "region": region, "invite_url": invite},
    })


def test_games_search_and_offers(client, shop):
    r = client.get("/api/v1/steam-gifts/games?q=ring", headers=shop["h"]).json()
    assert [g["appid"] for g in r["items"]] == [1245620]
    assert r["items"][0]["cover"].endswith("/1245620/header.jpg")
    assert client.get("/api/v1/steam-gifts/games?q=730", headers=shop["h"]).json()["items"][0]["name"] == \
        "Counter-Strike 2"
    g = client.get("/api/v1/steam-gifts/games/1245620", headers=shop["h"]).json()
    assert g["name"] == "ELDEN RING" and len(g["offers"]) == 2
    cis = next(r for r in g["offers"][0]["regions"] if r["region"] == "CIS")
    assert cis["price_usd"] == "41.5800"  # 38.50 + 8%


def test_gift_order_flow(client, conn, shop, supplier):
    r = _gift(client, shop["h"])
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert order["product_name"] == "Steam Gift — ELDEN RING (CIS)"
    assert balance(conn, shop["id"]) == 1_000_000 - 415_800
    row = conn.execute("SELECT cost_micro FROM orders").fetchone()
    assert row["cost_micro"] == 385_000
    orders.process_pending(conn, supplier)
    assert client.get(f"/api/v1/orders/{order['order_id']}", headers=shop["h"]).json()["order"]["status"] == \
        "completed"


def test_gift_validation(client, conn, shop):
    assert _gift(client, shop["h"], invite="https://evil.com/x").json()["code"] == "invalid_field"
    assert _gift(client, shop["h"], region="US").json()["code"] == "region_unavailable"
    assert _gift(client, shop["h"], sub_id="999").json()["code"] == "product_not_found"
    assert _gift(client, shop["h"], app_id="abc").json()["code"] == "invalid_field"
    assert balance(conn, shop["id"]) == 1_000_000


def test_gift_panel_page_and_data(client, conn):
    from donatix import accounts
    accounts.create_user(conn, email="g@example.com", login="gifter", password="password123", status="active")
    web_login(client, "g@example.com", "password123")
    page = client.get("/panel/buy/steam-gift").text
    assert "Steam Гифты (игры)" in page and "Регион и цена" in page and "Steam Invite ссылка" in page
    d = client.get("/panel/data/steam-gifts/games?q=dota").json()
    assert d["items"][0]["appid"] == 570
    assert client.get("/panel/data/steam-gifts/games/570").json()["offers"][0]["sub_id"] == 197846


def test_fazer_gift_endpoints():
    seen = {}

    def handler(req: httpx.Request):
        p = req.url.path.removeprefix("/api/v2")
        if p == "/steam-gifts/games":
            return httpx.Response(200, json={"ok": True, "games": [{"name": "Counter-Strike 2", "appid": 730}]})
        if p == "/steam-gifts/games/730":
            return httpx.Response(200, json={"ok": True, "appid": 730, "offers": [
                {"sub_id": 54029, "name": "CS2", "regions": [{"region": "CIS", "price": "0.0000"}]}]})
        if p == "/steam-gifts/order":
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"ok": True, "order": {"id": "ord-5", "status": "processing"}})
        return httpx.Response(403, json={"ok": False, "error": "no"})

    s = FazerSupplier("K", transport=httpx.MockTransport(handler), catalog_pause=0)
    assert "steam-gift" in {p.id for p in s.fetch_catalog()}
    assert s.steam_gift_games() == [{"appid": 730, "name": "Counter-Strike 2"}]
    assert s.steam_gift_offers(730)[0]["sub_id"] == 54029
    s.create_order({"kind": "steam_gift", "supplier_ref": {}}, 1,
                   {"invite_url": INVITE, "sub_id": "54029", "app_id": "730", "region": "CIS"}, "k")
    assert seen["body"] == {"invite_url": INVITE, "sub_id": 54029, "app_id": 730, "region": "CIS"}
