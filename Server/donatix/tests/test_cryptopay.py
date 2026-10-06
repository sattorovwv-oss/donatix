from decimal import Decimal

from conftest import balance, web_login
from fastapi.testclient import TestClient

from donatix import accounts, cryptopay, payments

ADDR = "TXyzABCDEFGHJKLMNPQRSTUVWXYZabcdef"


def _method(conn, config, auto, details):
    payments.save_methods(conn, [{"code": "usdt", "title": "USDT", "currency": "USDT", "details": details,
                                  "enabled": True, "auto": auto}])


def _client(app, conn):
    uid = accounts.create_user(conn, email="c@example.com", login="client1", password="password123",
                               status="active")
    c = TestClient(app)
    token = web_login(c, "c@example.com", "password123")
    return uid, c, token


def test_trc20_auto_credit(app, config, conn, monkeypatch):
    _method(conn, config, "trc20", f"USDT TRC20: {ADDR}")
    uid, c, token = _client(app, conn)
    r = c.post("/panel/balance", data={"csrf": token, "method": "usdt", "amount": "20"}, follow_redirects=False)
    assert r.headers["location"].endswith("/pay")
    p = conn.execute("SELECT * FROM payments ORDER BY id DESC").fetchone()
    amount = Decimal(p["pay_amount"])
    assert p["auto_kind"] == "trc20" and p["pay_address"] == ADDR and Decimal("20") < amount < Decimal("20.01")
    page = c.get(r.headers["location"]).text
    assert p["pay_amount"] in page and ADDR in page

    txs = []
    monkeypatch.setattr(cryptopay, "_get_json", lambda url, params, headers: {"data": txs})
    assert cryptopay.check_all(conn, config) == 0          # денег ещё нет
    ts = cryptopay._ms(p["created_at"]) + 5000
    txs.append({"transaction_id": "tx-wrong", "to": ADDR, "value": "20000000", "block_timestamp": ts,
                "token_info": {"address": cryptopay.USDT_TRC20, "decimals": 6}})
    txs.append({"transaction_id": "tx-ok", "to": ADDR, "value": str(int(amount * 1_000_000)), "block_timestamp": ts,
                "token_info": {"address": cryptopay.USDT_TRC20, "decimals": 6}})
    cryptopay.reset()
    assert cryptopay.check_all(conn, config) == 1
    assert balance(conn, uid) == 200_000
    assert c.get(f"/panel/data/payment/{p['id']}").json()["status"] == "paid"
    # тот же перевод второй раз не засчитывается
    cryptopay.reset()
    assert cryptopay.check_all(conn, config) == 0


def test_trc20_needs_address(conn, config):
    import pytest
    with pytest.raises(payments.PaymentError):
        _method(conn, config, "trc20", "без адреса")


def test_binance_pay(app, config, conn, monkeypatch):
    config.binance_pay_key, config.binance_pay_secret = "key", "secret"
    _method(conn, config, "binance", "")
    uid = accounts.create_user(conn, email="b@example.com", login="botshop", password="password123",
                               status="active")
    key = accounts.create_api_key(conn, uid, "bot")
    state = {"status": "INITIAL"}
    calls = []

    def fake(cfg, path, body):
        calls.append(path)
        if path.endswith("/order"):
            return {"checkoutUrl": "https://pay.binance.com/checkout/abc", "prepayId": "1"}
        return {"status": state["status"]}
    monkeypatch.setattr(cryptopay, "_binance_post", fake)
    api = TestClient(app)
    r = api.post("/api/v1/payments", headers={"X-API-Key": key}, json={"method": "usdt", "amount_usd": "15"}).json()
    pay = r["payment"]
    assert pay["auto"] == "binance" and pay["pay_url"].startswith("https://pay.binance.com")
    ext = conn.execute("SELECT ext_id FROM payments WHERE id = ?", (pay["id"],)).fetchone()[0]
    # фальшивое уведомление «оплачено» ничего не даёт — статус спрашиваем у Binance
    TestClient(app).post("/pay/binance/webhook", json={"data": {"merchantTradeNo": ext}})
    assert balance(conn, uid) == 0
    state["status"] = "PAID"
    TestClient(app).post("/pay/binance/webhook", json={"data": {"merchantTradeNo": ext}})
    assert balance(conn, uid) == 150_000


def test_binance_error_cancels(app, config, conn, monkeypatch):
    config.binance_pay_key, config.binance_pay_secret = "key", "secret"
    _method(conn, config, "binance", "")

    def boom(cfg, path, body):
        raise cryptopay.CryptoPayError("Binance Pay: invalid key")
    monkeypatch.setattr(cryptopay, "_binance_post", boom)
    uid, c, token = _client(app, conn)
    r = c.post("/panel/balance", data={"csrf": token, "method": "usdt", "amount": "20"})
    assert "invalid key" in r.text
    assert conn.execute("SELECT status FROM payments").fetchone()[0] == "cancelled"


def test_bybit_uid_auto_credit(app, config, conn, monkeypatch):
    config.bybit_key, config.bybit_secret = "k", "s"
    _method(conn, config, "bybit", "Bybit UID 123456789")
    uid, c, token = _client(app, conn)
    r = c.post("/panel/balance", data={"csrf": token, "method": "usdt", "amount": "30"}, follow_redirects=False)
    p = conn.execute("SELECT * FROM payments ORDER BY id DESC").fetchone()
    assert p["auto_kind"] == "bybit" and p["pay_address"] == "123456789"
    assert "По UID" in c.get(r.headers["location"]).text
    ts = cryptopay._ms(p["created_at"]) + 3000
    internal = [{"id": "555", "amount": p["pay_amount"], "status": 2, "createdTime": str(ts), "coin": "USDT"}]

    def fake(cfg, path, params):
        return {"rows": internal if "internal" in path else []}
    monkeypatch.setattr(cryptopay, "_bybit_get", fake)
    assert cryptopay.check_all(conn, config) == 1
    assert balance(conn, uid) == 300_000


def test_bybit_needs_uid(conn, config):
    import pytest
    with pytest.raises(payments.PaymentError):
        _method(conn, config, "bybit", "без номера")
