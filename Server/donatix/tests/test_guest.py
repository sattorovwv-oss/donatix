"""Гость смотрит каталог и цены без входа; купить или пополнить — после регистрации."""
from conftest import csrf_of
from fastapi.testclient import TestClient


def _product(conn, kind="topup"):
    return conn.execute("SELECT id FROM products WHERE kind = ? AND active = 1 LIMIT 1", (kind,)).fetchone()["id"]


def test_guest_sees_catalog_and_prices(app, conn):
    guest = TestClient(app)
    page = guest.get("/panel/catalog?kind=topup")
    assert page.status_code == 200 and "Регистрация" in page.text and "Войти" in page.text
    pid = _product(conn)
    buy = guest.get(f"/panel/buy/{pid}")
    assert buy.status_code == 200
    assert "Зарегистрироваться и купить" in buy.text and "$" in buy.text           # цена видна
    assert guest.get("/panel/catalog?kind=telegram").status_code == 200
    assert "Каталог и цены" in guest.get("/").text


def test_guest_cannot_buy_or_top_up(app, conn):
    guest = TestClient(app)
    pid = _product(conn)
    token = csrf_of(guest.get(f"/panel/buy/{pid}").text)
    r = guest.post(f"/panel/buy/{pid}", data={"csrf": token, "quantity": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert guest.get("/panel/balance", follow_redirects=False).headers["location"] == "/login"
    assert guest.get(f"/panel/data/check-account/{pid}").status_code == 401
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_register_returns_guest_to_the_product(app, conn):
    guest = TestClient(app)
    pid = _product(conn)
    page = guest.get(f"/register?next=/panel/buy/{pid}")
    assert "вернём вас к товару" in page.text
    r = guest.post("/register", data={"csrf": csrf_of(page.text), "email": "new@example.com", "login": "newbie",
                                      "password": "password123", "password2": "password123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/panel/buy/{pid}"
    assert "Зарегистрироваться и купить" not in guest.get(f"/panel/buy/{pid}").text   # теперь можно купить


def test_next_only_inside_panel(app):
    guest = TestClient(app)
    page = guest.get("/register?next=https://evil.example/x")
    r = guest.post("/register", data={"csrf": csrf_of(page.text), "email": "n2@example.com", "login": "newbie2",
                                      "password": "password123", "password2": "password123"}, follow_redirects=False)
    assert r.headers["location"] == "/panel"
