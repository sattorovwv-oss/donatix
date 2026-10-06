from conftest import balance, csrf_of, make_client, web_login

from donatix import cache, popular


def test_telegram_page_tabs_and_plans(client, conn):
    make_client(conn)
    web_login(client, "shop1@example.com", "password123")
    page = client.get("/panel/catalog?kind=telegram").text
    assert "Звёзды" in page and "Premium" in page and "Получатель" in page
    assert 'data-tab="stars"' in page and 'name="product_id" value="tg-stars"' in page
    assert "3 месяца" in page and "12 месяцев" in page                # планы Premium уже на странице
    assert 'aria-current="page"' in page and "<span>Telegram</span>" in page
    prem = client.get("/panel/catalog?kind=telegram&tab=premium").text
    assert 'id="buy" data-tab="premium"' in prem
    # старые адреса ведут на новую страницу
    r = client.get("/panel/catalog?kind=telegram_premium", follow_redirects=False)
    assert r.headers["location"] == "/panel/catalog?kind=telegram&tab=premium"
    r = client.get("/panel/buy/tg-stars", follow_redirects=False)
    assert r.headers["location"] == "/panel/catalog?kind=telegram"


def test_buy_stars_and_premium_from_one_page(client, conn):
    uid, _ = make_client(conn)
    web_login(client, "shop1@example.com", "password123")
    before = balance(conn, uid)
    page = client.get("/panel/catalog?kind=telegram").text
    r = client.post("/panel/telegram", data={"csrf": csrf_of(page), "idem": "a1", "tab": "stars",
                                             "product_id": "tg-stars", "quantity": "100",
                                             "field_telegram_username": "@player"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/panel/orders/")
    mid = balance(conn, uid)
    assert mid < before
    r = client.post("/panel/telegram", data={"csrf": csrf_of(page), "idem": "a2", "tab": "premium",
                                             "product_id": "tg-premium-3", "field_telegram_username": "@player"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT kind, quantity FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "telegram_premium" and row["quantity"] == 1 and balance(conn, uid) < mid
    bad = client.post("/panel/telegram", data={"csrf": csrf_of(page), "idem": "a3", "tab": "premium",
                                               "field_telegram_username": "@player"})
    assert bad.status_code == 400 and "Выберите план" in bad.text


def test_popular_in_menu_and_home(client, conn):
    cache.clear()
    items = popular.compute(conn)
    titles = [i["title"] for i in items]
    assert "PUBG Mobile" in titles and "Telegram Stars и Premium" in titles
    make_client(conn)
    web_login(client, "shop1@example.com", "password123")
    assert "Популярное" in client.get("/panel").text
    home = client.get("/").text
    assert "Популярное" in home and "Конструктор ботов" in home and "Что такое конструктор ботов?" in home


def test_popular_pins_free_fire_regions_first(conn):
    now = "2026-01-01T00:00:00+00:00"
    for pid, cat, name, region in (("ff-cis-1", "ff_cis", "Free Fire (СНГ)", None),
                                   ("ff-id-1", "free_fire_global", "Free Fire", "ID"),
                                   ("ffmax-1", "ff_max", "Free Fire MAX", "ID")):
        conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, region, "
                     "updated_at) VALUES (?, 'topup', ?, ?, '100 алмазов', '1', ?, ?)",
                     (pid, cat, name, region, now))
    items = popular.compute(conn)
    assert [i["title"] for i in items[:3]] == ["Free Fire СНГ", "Free Fire Индонезия", "PUBG Mobile"]
    assert items[0]["href"] == "/panel/catalog?kind=topup&category=ff_cis"
    assert items[1]["href"] == "/panel/catalog?kind=topup&category=free_fire_global&region=ID"


def test_popular_pins_standoff2_when_in_catalog(conn):
    cache.clear()
    now = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, updated_at) "
                 "VALUES ('cd-standoff-2-so100', 'topup', 'cd_standoff-2', 'Standoff 2', '100 Gold', '0.9', ?)",
                 (now,))
    items = popular.compute(conn)
    so = [i for i in items if i["key"] == "standoff2"]
    assert so and so[0]["title"] == "Standoff 2"
    assert so[0]["href"] == "/panel/catalog?kind=topup&category=cd_standoff-2"


def test_popular_pins_clash_of_clans_not_royale(conn):
    cache.clear()
    now = "2026-01-01T00:00:00+00:00"
    for pid, cat, name in (("coc-1", "clash_of_clans", "Clash of Clans"),
                           ("cr-1", "clash_royale", "Clash Royale")):
        conn.execute("INSERT INTO products (id, kind, category_id, category_name, name, base_price, updated_at) "
                     "VALUES (?, 'topup', ?, ?, '80 Gems', '1', ?)", (pid, cat, name, now))
    items = popular.compute(conn)
    keys = [i["key"] for i in items]
    assert "coc" in keys
    coc = next(i for i in items if i["key"] == "coc")
    assert coc["title"] == "Clash of Clans" and coc["href"].endswith("category=clash_of_clans")
    assert not any(i.get("title") == "Clash Royale" and i["key"] == "coc" for i in items)
