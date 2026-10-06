from donatix import accounts, analytics, db


def _order(conn, uid, status, total, kind="topup", ago_days=0):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=ago_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.execute(
        "INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, total_micro, "
        "cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"dx-{n}", uid, "p", kind, "x", 1, "1", total, int(total * 0.9), status, f"k{n}", ts, ts))


def test_period_totals_and_breakdown(conn):
    uid = accounts.create_user(conn, email="a@example.com", login="anuser", password="password123", status="active")
    other = accounts.create_user(conn, email="b@example.com", login="other", password="password123", status="active")
    for st in ("completed", "completed", "failed"):
        _order(conn, uid, st, 100_000)
    _order(conn, uid, "completed", 50_000, kind="telegram_stars", ago_days=3)
    _order(conn, uid, "completed", 70_000, ago_days=20)
    _order(conn, other, "completed", 999_000)
    accounts.post_ledger(conn, uid, 500_000, "Пополнение")

    a = analytics.build(conn, "7d", user_id=uid)
    assert (a["created"], a["done"], a["refunded"]) == (4, 3, 1)
    assert a["turnover"] == 250_000 and a["topped"] == 500_000
    assert [k["kind"] for k in a["by_kind"]] == ["topup", "telegram_stars"]
    assert len(a["series"]) in (7, 8) and sum(s["created"] for s in a["series"]) == 4
    assert a["chart"]["lines"]["created"].startswith("M")

    assert analytics.build(conn, "all", user_id=uid)["created"] == 5
    assert analytics.build(conn, "24h", user_id=uid)["series"][-1]["label"].endswith(":00")
    assert analytics.build(conn, "30d")["created"] == 6  # админ видит всех
    assert db.now().endswith("Z")


def test_admin_finance_block(app, conn):
    from conftest import web_login
    from fastapi.testclient import TestClient

    uid = accounts.create_user(conn, email="f@example.com", login="finuser", password="password123", status="active")
    _order(conn, uid, "completed", 108_000)  # закупка 97 200
    db.set_setting(conn, "supplier_balance", "50.0000")
    with db.tx(conn):
        accounts.post_ledger(conn, uid, 200_000, "Пополнение")
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/stats?period=7d").text
    assert "Ушло поставщику" in page and "$9.7200" in page and "$1.0800" in page  # закупка и прибыль
    assert "$30.0000" in page  # свободно: 50 − 20 долг клиенту
