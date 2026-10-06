"""Админка → Учёт денег: все движения за период на одной странице."""
from conftest import make_client, web_login
from fastapi.testclient import TestClient

from donatix import cashiers, db, finance, periods, timez

ALI = 5550001


def _setup(conn):
    uid, _ = make_client(conn)
    cashiers.add(conn, ALI, "Али")
    now = db.now()
    conn.execute("INSERT INTO payments (user_id, method, amount_micro, pay_amount, pay_currency, status, created_at, "
                 "resolved_at) VALUES (?, 'alif', ?, '1', 'TJS', 'paid', ?, ?)", (uid, 100 * 10_000, now, now))
    for i, (status, total, cost) in enumerate((("completed", 150, 50), ("completed", 30, 25), ("failed", 20, 18))):
        conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                     "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at, completed_at) "
                     "VALUES (?, ?, 'p', 'topup', 'x', 1, '1', ?, ?, ?, ?, ?, ?, ?)",
                     (f"dx-m{i}", uid, total * 10_000, cost * 10_000, status, f"km{i}", now, now, now))
    return uid


def test_money_ledger_adds_up(config, conn):
    _setup(conn)
    pr = periods.resolve(timez.zone(timez.site_zone_name(conn)), "today")
    m = finance.ledger(conn, config, pr)
    s = m["total"]
    assert s["revenue"] == 180 * 10_000 and s["cost"] == 75 * 10_000
    assert s["refunds"] == 1 and s["refunded"] == 20 * 10_000
    assert s["cashiers"] and s["cashiers"][0]["share"] > 0
    # всё, что продали, разложено без остатка: поставщику + рефералам + D-коин + кассирам + вам
    assert sum(f["micro"] for f in m["flow"]) == s["revenue"]
    assert len(m["rows"]) == 1 and m["rows"][0]["revenue"] == s["revenue"]


def test_money_page_for_admin(app, config, conn):
    _setup(conn)
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/money?period=7d").text
    for text in ("Учёт денег", "Куда ушли деньги", "Поставщику", "Рефералам", "Копилка D-коина", "Кассирам",
                 "Вам чистыми", "Возвраты клиентам", "Али", "По дням"):
        assert text in page, text
    assert page.count("<tr>") >= 8                                        # 7 дней + шапка + итог
    assert "По месяцам" in admin.get("/admin/money?period=all&date_from=2025-01-01&date_to=2026-09-30").text
    client = TestClient(app)
    make_client(conn, "shop2")
    web_login(client, "shop2@example.com", "password123")
    assert client.get("/admin/money", follow_redirects=False).status_code in (303, 403, 404)
