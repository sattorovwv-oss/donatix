import json

import httpx

from donatix import orders, webhooks
from donatix.security import sign_webhook


def test_webhook_signed_and_marked_sent(client, conn, shop, supplier):
    conn.execute("UPDATE users SET webhook_url = 'https://shop.example/hook', webhook_secret = 'whsec_x' "
                 "WHERE id = ?", (shop["id"],))
    client.post("/api/v1/orders", headers=shop["h"],
                json={"product_id": "tg-stars", "quantity": 50, "fields": {"telegram_username": "@player_one"}})
    orders.process_pending(conn, supplier)

    got = []

    def handler(req: httpx.Request):
        got.append(req)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert webhooks.deliver_pending(conn, http) == 1
        assert webhooks.deliver_pending(conn, http) == 0  # второй раз не шлём

    req = got[0]
    ts = req.headers["X-Donatix-Timestamp"]
    assert req.headers["X-Donatix-Signature"] == sign_webhook("whsec_x", ts, req.content)
    body = json.loads(req.content)
    assert body["event"] == "order.updated" and body["order"]["status"] == "completed"


def test_webhook_without_url_is_skipped(client, conn, shop, supplier):
    client.post("/api/v1/orders", headers=shop["h"],
                json={"product_id": "tg-stars", "quantity": 50, "fields": {"telegram_username": "@player_one"}})
    orders.process_pending(conn, supplier)
    webhooks.deliver_pending(conn, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert conn.execute("SELECT webhook_state FROM orders").fetchone()[0] == "none"
