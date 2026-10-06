from datetime import datetime, timedelta, timezone

from conftest import make_client, web_login
from fastapi.testclient import TestClient

from donatix import orders, periods, timez

UTC = timezone.utc
DUSHANBE = timez.zone("Asia/Dushanbe")


def _order(conn, uid, n, when, status="completed", total=10):
    ts = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                 "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at, completed_at) "
                 "VALUES (?, ?, 'tg-stars', 'telegram_stars', 'Stars', 1, '1', ?, ?, ?, ?, ?, ?, ?)",
                 (f"dx-p{n}", uid, total * 10_000, total * 9_000, status, f"kp{n}", ts, ts, ts))


def test_day_is_0000_to_2359_local():
    # 23:59 по Душанбе (18:59 UTC) — ещё «сегодня»; в 00:00 (19:00 UTC) начинаются новые сутки
    late = datetime(2026, 9, 26, 18, 59, tzinfo=UTC)
    p = periods.resolve(DUSHANBE, "today", now=late)
    assert (p.start.day, p.start.hour, p.end.day, p.end.hour) == (26, 0, 27, 0)
    assert p.a == "2026-09-25T19:00:00" and p.b == "2026-09-26T19:00:00"
    p = periods.resolve(DUSHANBE, "today", now=late + timedelta(minutes=1))
    assert p.start.day == 27
    y = periods.resolve(DUSHANBE, "yesterday", now=late)
    assert (y.start.day, y.end.day) == (25, 26)
    week = periods.resolve(DUSHANBE, "7d", now=late)
    assert (week.start.day, week.end.day) == (20, 27)                  # 20…26 — семь полных суток
    c = periods.resolve(DUSHANBE, date_from="2026-09-10", date_to="2026-09-01")   # перепутали — поменяем местами
    assert (c.key, c.start.day, c.end.day, c.label) == ("custom", 1, 11, "01.09.2026 — 10.09.2026")
    assert periods.resolve(DUSHANBE, "").key == "all" and periods.resolve(DUSHANBE, "").sql("x") == ("1=1", [])


def test_today_counter_starts_from_zero_at_midnight(conn):
    uid, _ = make_client(conn)
    today0 = periods.midnight(DUSHANBE)
    _order(conn, uid, 1, today0 + timedelta(minutes=5))          # сегодня 00:05
    _order(conn, uid, 2, today0 - timedelta(minutes=5))          # вчера 23:55 — уже не сегодня
    assert orders.stats(conn, 1)["orders"] == 1
    assert orders.stats(conn, 30)["orders"] == 2
    days = orders.daily(conn, 2)
    assert [d["orders"] for d in days] == [1, 1] and days[-1]["day"] == today0.date().isoformat()


def test_orders_filter_by_period_and_dates(app, conn):
    uid, _ = make_client(conn)
    today0 = periods.midnight(DUSHANBE)
    _order(conn, uid, 1, today0 + timedelta(hours=1), total=10)
    _order(conn, uid, 2, today0 + timedelta(hours=2), status="failed", total=5)
    _order(conn, uid, 3, today0 - timedelta(hours=3), total=20)                 # вчера
    _order(conn, uid, 4, today0 - timedelta(days=10), total=40)                 # 10 дней назад
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")

    page = admin.get("/admin/orders?period=today").text
    assert "dx-p1" in page and "dx-p2" in page and "dx-p3" not in page
    assert "Сегодня · заказов" in page and "$10.0000" in page                  # выполнено на $10
    page = admin.get("/admin/orders?period=yesterday").text
    assert "dx-p3" in page and "dx-p1" not in page
    d = (today0 - timedelta(days=10)).date().isoformat()
    page = admin.get(f"/admin/orders?date_from={d}&date_to={d}").text
    assert "dx-p4" in page and "dx-p1" not in page and "$40.0000" in page
    assert "dx-p4" in admin.get("/admin/orders").text                           # без фильтра — всё

    client = TestClient(app)
    web_login(client, "shop1@example.com", "password123")
    page = client.get("/panel/orders?period=today").text
    assert "dx-p1" in page and "dx-p3" not in page and "Сегодня: 2" in page
    assert "dx-p4" in client.get("/panel/orders?period=30d").text


def test_stars_markup_applies_to_whole_project(config, conn):
    from decimal import Decimal

    from donatix import accounts, sitecfg
    uid, _ = make_client(conn, login="vipshop")
    conn.execute("UPDATE users SET markup_override = '3' WHERE id = ?", (uid,))   # личная наценка 3%
    user = accounts.get_user(conn, uid)
    assert accounts.markup_for(user, config, "telegram_stars") == Decimal("3")
    sitecfg.save(conn, config, {"markup_telegram_stars": "50"})
    assert accounts.markup_for(user, config, "telegram_stars") == Decimal("50")    # звёзды — для всех 50%
    assert accounts.markup_for(user, config, "topup") == Decimal("3")              # остальное — по личной
