"""Несколько пакетов одной игры на один ID: корзина на странице покупки, заказы уходят по очереди."""
from conftest import balance, csrf_of, make_client, web_login
from fastapi.testclient import TestClient


def _packs(conn, region="GLOBAL"):
    from donatix import catalog
    ids = [r["id"] for r in conn.execute("SELECT id FROM products WHERE category_id = 'free_fire' AND active = 1 "
                                         "ORDER BY rowid").fetchall()]
    return [i for i in ids if (catalog.get_product(conn, i).get("region") or "") == region][:2]


def _login(app, conn, money="100"):
    uid, _ = make_client(conn, login="cart1", balance=money)
    conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (uid,))
    c = TestClient(app)
    web_login(c, "cart1@example.com", "password123")
    return uid, c


def _fields(conn, pid):
    import json
    raw = conn.execute("SELECT fields_json FROM products WHERE id = ?", (pid,)).fetchone()[0]
    return {f"field_{f['key']}": "123456789" for f in json.loads(raw or "[]")}


def test_cart_buys_several_packs_for_one_id(app, conn):
    a, b = _packs(conn)
    uid, c = _login(app, conn)
    page = c.get(f"/panel/buy/{a}")
    assert "pp-add" in page.text and "Корзина" in page.text
    before = balance(conn, uid)
    r = c.post(f"/panel/buy-many/{a}", data={"csrf": csrf_of(page.text), "idem": "x1", **_fields(conn, a),
                                             f"item_{a}": "2", f"item_{b}": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/panel/orders"
    rows = conn.execute("SELECT product_id, fields_json FROM orders WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
    assert [x["product_id"] for x in rows] == [a, a, b]
    assert all("123456789" in x["fields_json"] for x in rows)
    assert balance(conn, uid) < before
    # тот же запрос ещё раз (двойной клик) — новых заказов нет
    c.post(f"/panel/buy-many/{a}", data={"csrf": csrf_of(page.text), "idem": "x1", **_fields(conn, a),
                                         f"item_{a}": "2", f"item_{b}": "1"})
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,)).fetchone()[0] == 3


def test_cart_checks_money_for_everything_first(app, conn):
    a, b = _packs(conn)
    uid, c = _login(app, conn, money="0.01")
    page = c.get(f"/panel/buy/{a}")
    r = c.post(f"/panel/buy-many/{a}", data={"csrf": csrf_of(page.text), "idem": "x2", **_fields(conn, a),
                                             f"item_{a}": "3"})
    assert r.status_code == 400 and "Не хватает" in r.text
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,)).fetchone()[0] == 0


def test_cart_rejects_other_games_regions_and_too_many(app, conn):
    a, _ = _packs(conn)
    turkey = _packs(conn, "TR")[0]
    other = conn.execute("SELECT id FROM products WHERE category_id != 'free_fire' AND active = 1 "
                         "LIMIT 1").fetchone()[0]
    uid, c = _login(app, conn)
    token = csrf_of(c.get(f"/panel/buy/{a}").text)
    assert c.post(f"/panel/buy-many/{a}", data={"csrf": token, f"item_{other}": "1"}).status_code == 400
    assert c.post(f"/panel/buy-many/{a}", data={"csrf": token, **_fields(conn, a),
                                                f"item_{turkey}": "1"}).status_code == 400
    r = c.post(f"/panel/buy-many/{a}", data={"csrf": token, **_fields(conn, a), f"item_{a}": "25"})
    assert r.status_code == 400 and "не больше 20" in r.text
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,)).fetchone()[0] == 0
