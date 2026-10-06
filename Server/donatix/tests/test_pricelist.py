from decimal import Decimal

from conftest import web_login

from donatix import pricelist


def _ff(conn):
    now = "2026-01-01T00:00:00.000Z"
    for n, (name, price) in enumerate((("Прокачка уровня (Level Up Pass)", "3.5"), ("Ваучер на месяц", "8.9"),
                                       ("520 алмазов", "4.6"), ("Ваучер на неделю", "1.9"), ("100 алмазов", "0.95"))):
        conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, region, "
                     "updated_at) VALUES (?, 'topup', 'ff_cis', 'Free Fire', ?, ?, 'CIS', ?)",
                     (f"ff-{n}", name, price, now))


def test_order_names_and_prices(conn, config):
    _ff(conn)
    data = pricelist.build(conn, config)
    ff = next(s for s in data["sections"] if s["key"] == "ff_cis")
    assert [p["short"] for p in ff["packs"]] == ["💎 100 алмазов", "💎 520 алмазов", "🎫 Ваучер на неделю",
                                                 "🎫 Ваучер на месяц", "🚀 Прокачка уровня"]
    rate, markup = data["rate"], data["markup"]
    assert ff["packs"][0]["tjs"] == pricelist.tjs_price("0.95", markup, rate)
    assert pricelist.tjs_price("1", Decimal("10"), Decimal("10.5")) == Decimal("11.55")
    assert {s["key"] for s in data["sections"]} == {"ff_cis", "pubg"}


def test_admin_page(client, conn):
    _ff(conn)
    web_login(client, "admin@example.com", "adminpass123")
    page = client.get("/admin/pricelist?show=ff_cis&fmt=story").text
    assert "Скачать PNG" in page and "Free Fire" in page and "1080×1920" in page
    assert '"fmt": "story"' in page and '"single": true' in page and "ff_cis" in page
    assert "PUBG" in client.get("/admin/pricelist").text
