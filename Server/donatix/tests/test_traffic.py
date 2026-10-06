from datetime import datetime, timedelta, timezone

from conftest import web_login
from fastapi.testclient import TestClient

from donatix import traffic

PHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def test_recognition():
    assert traffic.device_of(PHONE) == ("Телефон", "Safari", "iOS")
    assert traffic.device_of(DESKTOP) == ("Компьютер", "Chrome", "Windows")
    assert traffic.is_bot("Googlebot/2.1") and traffic.is_bot("python-httpx/0.27") and not traffic.is_bot(PHONE)
    assert traffic.source_of("https://www.google.com/", "donatix.tj") == "Google"
    assert traffic.source_of("https://t.me/somechannel", "donatix.tj") == "Telegram"
    assert traffic.source_of("https://donatix.tj/panel", "donatix.tj") == traffic.DIRECT
    assert traffic.source_of("", "donatix.tj", "ig") == "Instagram"


def test_visits_are_counted_once_per_page_and_visitor(app, config, conn):
    phone = TestClient(app, headers={"User-Agent": PHONE})
    phone.get("/?utm_source=instagram&utm_campaign=oktyabr", headers={"Referer": "https://l.instagram.com/"})
    phone.get("/register")
    phone.get("/terms")
    phone.get("/static/donatix.css")                     # файлы не считаются
    phone.get("/api/v1/products")                        # API не считается
    TestClient(app, headers={"User-Agent": "Googlebot/2.1"}).get("/")   # роботы не считаются
    desk = TestClient(app, headers={"User-Agent": DESKTOP})
    desk.get("/", headers={"Referer": "https://www.google.com/"})
    traffic.flush(config.db_path, force=True)

    rows = conn.execute("SELECT * FROM visits ORDER BY id").fetchall()
    assert [r["path"] for r in rows] == ["/", "/register", "/terms", "/"]
    assert len({r["visitor"] for r in rows}) == 2 and len({r["session"] for r in rows}) == 2
    assert rows[0]["source"] == "Instagram" and rows[0]["utm_campaign"] == "oktyabr" and rows[0]["is_new"] == 1
    assert rows[1]["source"] == "" and rows[1]["is_new"] == 0      # внутри визита источник не меняется
    assert rows[3]["source"] == "Google" and rows[3]["device"] == "Компьютер"

    t = traffic.report(conn, "7d", tz_hours=config.tz_offset)
    assert t["core"]["visitors"] == 2 and t["core"]["sessions"] == 2 and t["core"]["views"] == 4
    assert t["core"]["bounce"] == 50 and t["core"]["pages_per_session"] == 2.0
    assert {s["name"] for s in t["sources"]} == {"Instagram", "Google"}
    assert t["campaigns"][0]["name"] == "oktyabr"
    assert t["online"] == 2
    assert t["visits_heat"]["total"] == 2 and t["visits_heat"]["best"] is not None


def test_admin_page_and_admin_not_counted(app, config, conn):
    admin = TestClient(app, headers={"User-Agent": DESKTOP})
    web_login(admin, "admin@example.com", "adminpass123")
    admin.get("/")                                        # админ на сайте — не посетитель
    TestClient(app, headers={"User-Agent": PHONE}).get("/")
    page = admin.get("/admin/traffic?period=30d")
    assert page.status_code == 200
    for text in ("Посещаемость", "Сейчас на сайте", "Когда заходят", "Когда покупают", "Воронка",
                 "Источники", "Рекламные кампании", "Больше всего заходят"):
        assert text in page.text, text
    t = traffic.report(conn, "30d", tz_hours=config.tz_offset)
    assert t["core"]["visitors"] == 1


def test_buying_hours_from_orders(conn, config):
    # Заказ в 20:xx по местному времени → пик покупок в 20:00
    local = datetime.now(timezone(timedelta(hours=config.tz_offset))).replace(hour=20, minute=15) - timedelta(days=1)
    ts = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                 "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at) "
                 "VALUES ('dx-1', ?, 'tg-stars', 'telegram_stars', 'Stars', 1, '1', 10000, 9000, 'completed', "
                 "'k1', ?, ?)",
                 (uid, ts, ts))
    t = traffic.report(conn, "7d", tz_hours=config.tz_offset)
    assert t["buys_heat"]["top_hour"] == 20 and t["biz"]["orders"] == 1 and t["biz"]["revenue"] == 10000
