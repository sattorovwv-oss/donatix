"""FazerCards-совместимый API: так с Donatix работает готовый бот (stars_bot_template)."""

from decimal import Decimal

from conftest import balance
from fastapi.testclient import TestClient

from donatix import accounts, catalog


def _client(app, conn, money=100_000_000):
    catalog.sync_catalog(conn, app.state.supplier)
    uid = accounts.create_user(conn, email="shop@example.com", login="shopbot", password="password123",
                               status="active")
    accounts.post_ledger(conn, uid, money, "test")
    key = accounts.create_api_key(conn, uid, "bot")
    return uid, TestClient(app), {"X-API-Key": key}


def test_stars_flow_like_the_bot(app, conn, supplier):
    uid, c, h = _client(app, conn)
    q = c.get("/api/v2/telegram/stars", headers=h).json()
    assert q["ok"] and Decimal(q["price_per_star"]) > Decimal("0.015") and q["min_amount"] == 50
    assert c.get("/api/v2/balance", headers=h).json() == {"ok": True, "balance": "10000.0000", "currency": "USD"}
    r = c.post("/api/v2/telegram/stars/buy", headers=h, json={"telegram_username": "durov", "quantity": 100}).json()
    oid = r["order"]["id"]
    assert r["ok"] and r["order"]["order_id"] == oid and r["order"]["status"] in ("processing", "completed")
    from donatix import orders
    for _ in range(5):
        orders.process_pending(conn, supplier)
        st = c.get(f"/api/v2/orders/{oid}", headers=h).json()["order"]["status"]
        if st == "completed":
            break
    assert st == "completed"
    assert balance(conn, uid) < 100_000_000
    premium = c.get("/api/v2/telegram/premium", headers=h).json()["plans"]
    assert [p["months"] for p in premium] == [3, 6, 12]


def test_games_flow_like_the_bot(app, conn):
    uid, c, h = _client(app, conn)
    cats = c.get("/api/v2/topups/categories?limit=500", headers=h).json()
    ids = [x["category_id"] for x in cats["items"]]
    assert "pubg_mobile" in ids and cats["meta"]["has_more"] is False
    checkable = c.get("/api/v2/topups/validate-id", headers=h).json()["items"]
    assert {x["category_id"] for x in checkable} == {"pubg_mobile", "free_fire"}
    assert checkable[0]["fields"][0]["name"]
    offers = c.get("/api/v2/topups/offers?category_id=pubg_mobile&include_ui=1", headers=h).json()["offers"]
    offer = offers[0]
    assert offer["offer_id"].startswith("topup-pubg") and Decimal(offer["price_usd"]) > 0
    fkey = checkable[[x["category_id"] for x in checkable].index("pubg_mobile")]["fields"][0]["name"]
    ok = c.post("/api/v2/topups/validate-id", headers=h,
                json={"category_id": "pubg_mobile", "fields": {fkey: "5123456789"}}).json()
    assert ok == {"ok": True, "valid": True, "player_name": "Player_6789", "region": "GLOBAL"}
    bad = c.post("/api/v2/topups/validate-id", headers=h, json={"category_id": "pubg_mobile", "fields": {fkey: "1"}})
    assert bad.status_code == 400 and "not found" in bad.json()["error"].lower()
    r = c.post("/api/v2/topups/order", headers={**h, "Idempotency-Key": "bot-1-abc"},
               json={"category_id": "pubg_mobile", "offer_id": offer["offer_id"], "fields": {fkey: "5123456789"},
                     "quantity": 1}).json()
    assert r["ok"] and r["order"]["order_id"].startswith("dx-")
    again = c.post("/api/v2/topups/order", headers={**h, "Idempotency-Key": "bot-1-abc"},
                   json={"category_id": "pubg_mobile", "offer_id": offer["offer_id"], "fields": {fkey: "5123456789"}})
    assert again.json()["order"]["order_id"] == r["order"]["order_id"]  # повтор не списывает второй раз


def test_errors_are_fazer_style(app, conn):
    uid, c, h = _client(app, conn, money=0)
    r = c.post("/api/v2/telegram/stars/buy", headers=h, json={"telegram_username": "durov", "quantity": 100})
    assert r.status_code == 402 and r.json()["ok"] is False and r.json()["code"] == "insufficient_balance"
    assert c.get("/api/v2/balance", headers={"X-API-Key": "bad"}).json()["ok"] is False
    steam = c.get("/api/v2/steam-topup/rates", headers=h).json()
    assert steam["rate"]["currency"] == "RUB" and Decimal(steam["rate"]["price_usd"]) < Decimal("0.02")
