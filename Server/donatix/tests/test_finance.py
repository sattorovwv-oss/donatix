from datetime import datetime, timezone

from conftest import make_client, web_login

from donatix import db, finance, timez

UTC = timezone.utc


def _pay(conn, uid, usd, amount, currency, when, method="dc_city", auto=""):
    conn.execute("INSERT INTO payments (user_id, method, amount_micro, pay_amount, pay_currency, status, created_at, "
                 "resolved_at, auto_kind) VALUES (?, ?, ?, ?, ?, 'paid', ?, ?, ?)",
                 (uid, method, usd * 10_000, amount, currency, when, when, auto))


def _order(conn, uid, n, total, cost, when, status="completed"):
    conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                 "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at, completed_at) "
                 "VALUES (?, ?, 'tg-stars', 'telegram_stars', 'Stars', 1, '1', ?, ?, ?, ?, ?, ?, ?)",
                 (f"dx-f{n}", uid, total * 10_000, cost * 10_000, status, f"k{n}", when, when, when))


def test_day_is_midnight_to_midnight_dushanbe(conn):
    # 23:59 по Душанбе (18:59 UTC) — ещё сегодняшние сутки; 00:00 (19:00 UTC) — уже новые
    start, end = finance.window(conn, datetime(2026, 9, 26, 18, 59, tzinfo=UTC))
    assert (start.day, start.hour, end.day, end.hour) == (26, 0, 27, 0)
    start, _ = finance.window(conn, datetime(2026, 9, 26, 19, 0, tzinfo=UTC))
    assert (start.day, start.hour) == (27, 0)


def test_profit_and_amount_for_supplier(config, conn):
    uid, _ = make_client(conn)
    _pay(conn, uid, 100, "1100.00", "TJS", "2026-09-26T08:00:00")          # 13:00 Душанбе 26.09 — в сутках
    _pay(conn, uid, 50, "550.00", "TJS", "2026-09-26T18:30:00")            # 23:30 26.09 — ещё в сутках
    _pay(conn, uid, 20, "20", "USDT", "2026-09-26T10:00:00", "usdt", "trc20")
    _pay(conn, uid, 999, "1", "TJS", "2026-09-26T19:00:00")                # 00:00 27.09 — уже следующие
    _order(conn, uid, 1, 60, 50, "2026-09-26T09:00:00")
    _order(conn, uid, 2, 40, 35, "2026-09-26T15:00:00")
    _order(conn, uid, 3, 30, 25, "2026-09-26T16:00:00", status="failed")   # возвращён — не считается
    start, end = finance.window(conn, datetime(2026, 9, 27, 8, 0, tzinfo=UTC), days_back=1)
    s = finance.summary(conn, config, start, end)
    assert s["received"] == 1_700_000 and s["to_card"] == 1_500_000 and s["crypto"] == 200_000
    assert s["orders"] == 2 and s["revenue"] == 1_000_000 and s["cost"] == 850_000
    assert s["profit"] == 150_000
    assert s["to_supplier"] == 1_550_000                                  # поступило − прибыль
    text = finance.report_text(s)
    assert "Ваша прибыль" in text and "Отправить поставщику: $155.00" in text and "2 заказа" in text and "1650.00 TJS" in text


def test_finance_report_sent_once_a_day(config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent.append(text))
    midnight = datetime(2026, 9, 26, 19, 1, tzinfo=UTC)         # 00:01 27.09 по Душанбе
    assert finance.maybe_send(conn, config, midnight)
    assert not finance.maybe_send(conn, config, midnight.replace(hour=22))
    assert "Деньги за 26.09.2026" in sent[-1]
    assert finance.maybe_send(conn, config, midnight + __import__("datetime").timedelta(days=1))


def test_admin_finance_page(app, conn):
    from fastapi.testclient import TestClient
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/finance").text
    assert "Отправить поставщику" in page and "Ваша прибыль" in page and "00:00" in page


def test_times_shown_in_visitor_timezone(client, conn):
    uid, _ = make_client(conn)
    _order(conn, uid, 9, 1, 1, "2026-09-26T10:00:00.000Z")
    web_login(client, "shop1@example.com", "password123")
    client.cookies.set("dx_tz", "Europe/Moscow")
    assert "26.09.2026 13:00" in client.get("/panel/orders").text          # Москва UTC+3
    client.cookies.set("dx_tz", "Asia/Dushanbe")
    assert "26.09.2026 15:00" in client.get("/panel/orders").text          # Душанбе UTC+5
    # Выбор вручную важнее устройства
    from conftest import csrf_of
    token = csrf_of(client.get("/panel").text)
    client.post("/panel/timezone", data={"csrf": token, "tz": "Asia/Vladivostok"})
    assert timez.user_choice(conn, uid) == "Asia/Vladivostok"
    assert "26.09.2026 20:00" in client.get("/panel/orders").text          # Владивосток UTC+10
    client.post("/panel/timezone", data={"csrf": token, "tz": "../../etc/passwd"})
    assert timez.user_choice(conn, uid) == "auto"


def test_bad_timezone_cookie_falls_back(client):
    client.cookies.set("dx_tz", "../../etc/passwd")
    assert client.get("/").status_code == 200
    assert timez.resolve(None, None, "Not/AZone")[0] == "Asia/Dushanbe"
    assert db.now()   # просто чтобы импорт использовался
