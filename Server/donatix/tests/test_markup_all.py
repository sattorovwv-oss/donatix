"""Наценка из настроек действует на всех: и по уровням, и с личной наценкой — если её убрать."""
import re

from conftest import csrf_of, web_login
from fastapi.testclient import TestClient

from donatix import accounts


def _settings_form(admin):
    page = admin.get("/admin/settings").text
    form = dict(re.findall(r'name="([a-z_0-9]+)"[^>]*value="([^"]*)"', page))
    form["csrf"] = csrf_of(page)
    return page, form


def _price(client, pid):
    return re.findall(r"\$(\d+\.\d+)", client.get(f"/panel/buy/{pid}").text)


def test_nine_percent_for_everyone(app, config, conn):
    pid = conn.execute("SELECT id, base_price FROM products WHERE kind = 'topup' LIMIT 1").fetchone()
    uid = accounts.create_user(conn, email="p@example.com", login="personal", password="password123", status="active")
    gold = accounts.create_user(conn, email="g@example.com", login="golden", password="password123", status="active")
    conn.execute("UPDATE users SET markup_override = '3' WHERE id = ?", (uid,))
    conn.execute("UPDATE users SET tier = 'gold' WHERE id = ?", (gold,))
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page, form = _settings_form(admin)
    assert "личная наценка" in page and "personal (3%)" in page
    form.update({"markup_bronze": "9", "same_for_all": "1", "reset_personal": "1"})
    assert "сохранены" in admin.post("/admin/settings", data=form).text
    assert {str(v) for v in config.markups.values()} == {"9"}                 # все уровни — 9%
    assert conn.execute("SELECT markup_override FROM users WHERE id = ?", (uid,)).fetchone()[0] is None
    for email in ("p@example.com", "g@example.com"):
        c = TestClient(app)
        web_login(c, email, "password123")
        expected = f"{float(pid['base_price']) * 1.09:.4f}"
        shown = _price(c, pid["id"])
        assert any(abs(float(p) - float(expected)) < 0.0002 for p in shown), (email, shown)
    assert "клиент Bronze платит <b>$10.90" in admin.get("/admin/settings").text
