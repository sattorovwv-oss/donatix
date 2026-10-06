"""FlashTopup: подпись запроса, список игр, поиск ника по ID."""

import hashlib
import hmac
import json

import httpx

from donatix import account_check, flashtopup


def _transport(seen):
    def handler(req: httpx.Request):
        seen.append(req)
        path = req.url.path
        if path.endswith("/products"):
            return httpx.Response(200, json={"success": True, "data": [
                {"product_code": "TOPUP_FREE_FIRE", "name": "Free Fire (Global)", "validation_code": "freefire"},
                {"product_code": "TOPUP_MLBB", "name": "Mobile Legends", "validation_code": "mlbb"}]})
        body = json.loads(req.content)
        if body["user_id"] == "000":
            return httpx.Response(422, json={"success": False, "error": {"code": "INVALID_PLAYER_ID",
                                                                         "message": "Invalid UserID"}})
        return httpx.Response(200, json={"success": True, "data": {"account_name": "ALI_PRO"}})
    return httpx.MockTransport(handler)


def test_signature_and_nick():
    seen = []
    ft = flashtopup.FlashTopup("ID1", "secret", transport=_transport(seen))
    game = ft.match("Free Fire")
    assert game["validation_code"] == "freefire"
    r = ft.check_id("freefire", "5123456789")
    assert r == {"valid": True, "player_name": "ALI_PRO", "message": ""}
    req = seen[-1]
    canonical = "\n".join(["POST", "/api/reseller/v2/check-id", req.headers["X-FT-Timestamp"],
                           req.headers["X-FT-Nonce"], hashlib.sha256(req.content).hexdigest()])
    assert req.headers["X-FT-Signature"] == hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest()
    assert ft.check_id("freefire", "000")["valid"] is False


def test_site_uses_flash_when_supplier_cannot(app, conn, supplier, monkeypatch):
    seen = []
    monkeypatch.setattr(account_check, "_flash", flashtopup.FlashTopup("ID1", "s", transport=_transport(seen)))
    monkeypatch.setattr(supplier, "validate_id_categories", lambda: [], raising=False)
    account_check.reset()
    from donatix.catalog import get_product
    p = next(get_product(conn, r[0]) for r in conn.execute(
        "SELECT id FROM products WHERE category_id = 'free_fire' LIMIT 1"))
    assert account_check.can_check(supplier, p)
    key = p["fields"][0]["key"]
    r = account_check.check(supplier, p, {key: "5123456789"})
    assert r["valid"] and r["player_name"] == "ALI_PRO" and r["strict"] is False
    bad = account_check.check(supplier, p, {key: "000"})
    assert bad["valid"] is False and bad["strict"] is False      # чужая проверка заказ не блокирует


def test_nick_saved_in_db_and_checked_again_only_when_old(app, conn, supplier, monkeypatch):
    seen = []
    monkeypatch.setattr(account_check, "_flash", flashtopup.FlashTopup("ID1", "s", transport=_transport(seen)))
    monkeypatch.setattr(supplier, "validate_id_categories", lambda: [], raising=False)
    account_check.reset()
    from donatix.catalog import get_product
    p = next(get_product(conn, r[0]) for r in conn.execute(
        "SELECT id FROM products WHERE category_id = 'free_fire' LIMIT 1"))
    key = p["fields"][0]["key"]
    def calls():
        return sum(1 for r in seen if r.url.path.endswith("/check-id"))
    assert account_check.check(supplier, p, {key: "5123456789"})["player_name"] == "ALI_PRO"
    account_check.reset()                                         # сайт перезапустили — память пуста
    again = account_check.check(supplier, p, {key: "5123456789"})
    assert again["player_name"] == "ALI_PRO" and again.get("cached") and calls() == 1   # из базы, без запроса
    conn.execute("UPDATE player_names SET checked_at = checked_at - ?", (account_check.FRESH + 10,))
    account_check.reset()
    old = account_check.check(supplier, p, {key: "5123456789"})
    assert old["player_name"] == "ALI_PRO"                        # ответ сразу из базы…
    import time
    for _ in range(100):
        row = conn.execute("SELECT hits, checked_at FROM player_names").fetchone()
        if time.time() - row["checked_at"] < 60:
            break
        time.sleep(0.05)
    assert calls() == 2 and row["hits"] >= 2                      # …а в фоне — тихая перепроверка
    assert time.time() - row["checked_at"] < 60


def test_budget_limits_outside_calls(app, conn, supplier, monkeypatch):
    seen = []
    monkeypatch.setattr(account_check, "_flash", flashtopup.FlashTopup("ID1", "s", transport=_transport(seen)))
    monkeypatch.setattr(supplier, "validate_id_categories", lambda: [], raising=False)
    monkeypatch.setattr(account_check, "BUDGET", 3)
    account_check.reset()
    from donatix.catalog import get_product
    p = next(get_product(conn, r[0]) for r in conn.execute(
        "SELECT id FROM products WHERE category_id = 'free_fire' LIMIT 1"))
    key = p["fields"][0]["key"]
    results = [account_check.check(supplier, p, {key: f"51234{i:05d}"}) for i in range(6)]
    assert sum(1 for r in seen if r.url.path.endswith("/check-id")) == 3
    assert results[-1]["valid"] is None
