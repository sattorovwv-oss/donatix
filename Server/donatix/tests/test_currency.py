from conftest import RECEIPT, web_login
from fastapi.testclient import TestClient

from donatix import accounts, db


def test_currency_switch_shows_exact_tjs(app, config, conn):
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00"}
    uid = accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    with db.tx(conn):
        accounts.post_ledger(conn, uid, 489_302, "пополнение")   # $48.9302
    c = TestClient(app)
    token = web_login(c, "c@example.com", "password123")
    assert "$48.9302" in c.get("/panel").text
    r = c.get("/currency/TJS", headers={"referer": "https://evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/x"                          # только свои страницы
    page = c.get("/panel").text
    assert "533.34 с." in page and 'title="$48.9302"' in page    # 48.9302 × 10.9 = 533.339…
    assert 'name="amount_tjs"' in c.get("/panel/balance").text
    r = c.post("/panel/balance", data={"csrf": token, "method": "alif", "amount_tjs": "109"}, files=RECEIPT)
    assert "Переведите 109.00 TJS" in r.text
    p = conn.execute("SELECT amount_micro FROM payments").fetchone()
    assert p[0] == 100_000                                        # 109 / 10.9 = $10 ровно
    c.get("/currency/USD")
    assert "$48.9302" in c.get("/panel").text
