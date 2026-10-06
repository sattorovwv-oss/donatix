from conftest import make_client, web_login

from donatix import packs


def test_russian_names_and_order():
    assert packs.full("100 Diamonds") == "💎 100 алмазов"
    assert packs.full("22 Diamonds") == "💎 22 алмаза"
    assert packs.full("100+10 Diamonds") == "💎 100 + 10 алмазов"
    assert packs.full("Weekly Membership") == "🎫 Ваучер на неделю"
    assert packs.full("Monthly Membership") == "🎫 Ваучер на месяц"
    assert packs.full("Level Up Pass") == "🚀 Прокачка уровня"
    assert packs.full("60 UC") == "🪙 60 UC"
    names = ["Level Up Pass", "Monthly Membership", "520 Diamonds", "Weekly Membership", "100 Diamonds"]
    prices = {"Level Up Pass": 3, "Monthly Membership": 9, "520 Diamonds": 5, "Weekly Membership": 2,
              "100 Diamonds": 1}
    ordered = sorted(names, key=lambda n: packs.order_key(n, prices[n]))
    assert ordered == ["100 Diamonds", "520 Diamonds", "Weekly Membership", "Monthly Membership", "Level Up Pass"]


def test_catalog_page_groups_packs(client, conn):
    now = "2026-01-01T00:00:00.000Z"
    for n, (name, price) in enumerate((("Level Up Pass", "3.5"), ("Weekly Membership", "1.9"),
                                       ("520 Diamonds", "4.6"), ("100 Diamonds", "0.95"))):
        conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, updated_at) "
                     "VALUES (?, 'topup', 'ffx', 'Free Fire X', ?, ?, ?)", (f"ffx-{n}", name, price, now))
    make_client(conn)
    web_login(client, "shop1@example.com", "password123")
    page = client.get("/panel/catalog?kind=topup&category=ffx").text
    order = [page.index(t) for t in ("100 алмазов", "520 алмазов", "Ваучер на неделю", "Прокачка уровня")]
    assert order == sorted(order)
    assert "💎 Алмазы и валюта" in page and "🎫 Ваучеры и пропуска" in page and "🚀 Прокачка" in page
    assert "100 Diamonds" not in page.split("<main")[1].split("</main>")[0].replace("title=", "")
    buy = client.get("/panel/buy/ffx-3").text
    assert "💎" in buy and "100 алмазов" in buy
    # API — названия поставщика не меняются (партнёрские боты сверяют по ним)
