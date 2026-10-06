from datetime import datetime, timedelta, timezone
from itertools import pairwise

from conftest import balance, web_login
from fastapi.testclient import TestClient

from donatix import accounts, db, dcoin, finance, orders


def _buy(conn, uid, total, cost=None):
    """Заказ в обработке → выполнен: так же, как это делает сайт."""
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, total_micro, "
        "cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"dx-{n}", uid, "p", "topup", "x", 1, "1", total, cost if cost is not None else int(total * 0.92),
         "processing", f"k{n}", ts, ts))
    assert orders.complete(conn, cur.lastrowid, {})
    return cur.lastrowid


def _user(conn, login="coiner"):
    return accounts.create_user(conn, email=f"{login}@example.com", login=login, password="password123",
                                status="active")


def test_price_starts_at_zero_and_coins_come_from_purchases(conn):
    uid = _user(conn)
    assert dcoin.price(conn) == 0.0
    oid = _buy(conn, uid, 10_000)                                  # $1, прибыль 8 центов
    assert dcoin.balance(conn, uid) == 100 * dcoin.UNIT             # 100 D за $1
    s = dcoin.state(conn)
    assert s["pool"] == 80                                          # 10% прибыли = 0.8 цента
    assert 0 < dcoin.price(conn) <= dcoin.START_PRICE                 # старт: не дороже 0.00109 с.
    orders.complete(conn, oid, {})                                  # повторно — ничего
    assert dcoin.balance(conn, uid) == 100 * dcoin.UNIT
    conn.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (oid,))
    assert dcoin.award(conn, oid) == 0                              # за заказ — один раз


def test_bigger_order_moves_up_smaller_moves_down(conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    _buy(conn, uid, 10_000)
    p1 = dcoin.price(conn)
    _buy(conn, uid, 5_000)                                          # мельче обычного
    p2 = dcoin.price(conn)
    _buy(conn, uid, 30_000)                                         # крупнее обычного
    p3 = dcoin.price(conn)
    assert p2 < p1 < p3


def test_never_more_than_pool_and_owner_keeps_most(conn):
    uid = _user(conn)
    for total in (10_000, 3_000, 90_000, 1_000, 50_000):
        _buy(conn, uid, total)
    profit = conn.execute("SELECT SUM(total_micro - cost_micro) FROM orders").fetchone()[0]
    s = dcoin.state(conn)
    assert s["pool"] <= profit * 20 // 100                          # максимум 20% прибыли, остальное — наше
    everything = dcoin.quote(conn, s["supply"])
    assert everything <= s["pool"]                                  # все монеты сразу — не больше копилки


def test_zero_profit_order_gives_coins_but_not_money(conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    pool = dcoin.state(conn)["pool"]
    _buy(conn, uid, 10_000, cost=10_000)
    assert dcoin.state(conn)["pool"] == pool                          # без прибыли — ни цента в копилку


def test_exchange_opens_later_and_keeps_fee_in_pool(conn, monkeypatch):
    monkeypatch.setattr(dcoin, "START_PRICE", 1.0)                    # без запаса: цена сразу «взрослая»
    uid = _user(conn)
    for _ in range(20):
        _buy(conn, uid, 1_000_000, cost=500_000)                    # $100 с прибылью $50
    units = dcoin.balance(conn, uid)
    try:
        dcoin.exchange(conn, uid, units)
        raise AssertionError("обмен должен быть закрыт первый месяц")
    except dcoin.ExchangeError as exc:
        assert "откроется" in str(exc)
    later = datetime.now(timezone.utc) + timedelta(days=31)
    before_price, before_bal = dcoin.price(conn), balance(conn, uid)
    half = units // 2
    pay = dcoin.exchange(conn, uid, half, now=later)
    assert pay > 0 and balance(conn, uid) == before_bal + pay
    assert dcoin.price(conn) > before_price                          # 5% остались в копилке — цена выросла
    try:
        dcoin.exchange(conn, uid, 100, now=later + timedelta(hours=1))
        raise AssertionError("раз в сутки")
    except dcoin.ExchangeError as exc:
        assert "раз в сутки" in str(exc)


def test_candles_and_page(app, config, conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    _buy(conn, uid, 40_000)
    data = dcoin.candles(conn, "1m")
    assert len(data["candles"]) == dcoin.CANDLES
    last = data["candles"][-1]
    assert last[4] == dcoin.price(conn) and last[2] >= last[4]

    client = TestClient(app)
    web_login(client, "coiner@example.com", "password123")
    page = client.get("/panel/dcoin").text
    assert "dc-canvas" in page and "D-коин" in page and "Обмен откроется" in page
    j = client.get("/panel/data/dcoin?tf=5m").json()
    assert j["tf"] == "5m" and j["price"] > 0 and len(j["candles"]) == dcoin.CANDLES
    assert "dc-card" in client.get("/panel").text                     # карточка на главной кабинета


def test_finance_counts_pool_as_expense(conn, config):
    uid = _user(conn)
    _buy(conn, uid, 100_000, cost=50_000)                            # прибыль $5
    a = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    b = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert finance.net_profit(conn, a, b) == 50_000 - dcoin.state(conn)["pool"]


def test_admin_settings(app, config, conn):
    admin = TestClient(app)
    token = web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/settings").text
    assert "dcoin_pool_pct" in page
    form = {"csrf": token, "dcoin_per_usd": "50", "dcoin_pool_pct": "8", "dcoin_open_days": "7"}
    admin.post("/admin/settings", data=form)
    assert (dcoin.per_usd(conn), dcoin.pool_pct(conn), dcoin.open_days(conn)) == (50, 8, 7)


def test_each_purchase_moves_price_softly(conn):
    uid = _user(conn)
    for total, cost in ((10_000, 9_900), (500_000, 200_000), (3_000, 2_990), (90_000, 60_000), (1_000, 1_000)):
        _buy(conn, uid, total, cost)
    prices = [r[0] for r in conn.execute("SELECT price FROM dcoin_points WHERE reason != 'peak' ORDER BY id")]
    for a, b in pairwise(prices[1:]):                          # после первой покупки — шаги не больше 3%
        assert abs(b - a) / a <= 0.0301, (a, b)
    profit = conn.execute("SELECT SUM(total_micro - cost_micro) FROM orders").fetchone()[0]
    assert dcoin.state(conn)["pool"] <= profit * 20 // 100


def test_fixed_rate_and_pool_is_hidden(app, conn):
    uid = _user(conn)
    _buy(conn, uid, 5_000)                                            # $0.50 → ровно 50 D
    assert dcoin.balance(conn, uid) == 50 * dcoin.UNIT
    client = TestClient(app)
    web_login(client, "coiner@example.com", "password123")
    page = client.get("/panel/dcoin").text
    assert "Копилка" not in page and "копилк" not in page and "Всего монет" not in page
    for secret in ("Как растёт цена", "поднимают", "опускают", "цена растёт"):
        assert secret not in page                                     # как двигается цена — не рассказываем
    j = client.get("/panel/data/dcoin?tf=1s").json()
    assert j["tf"] == "1s" and j["step"] == 1
    assert "pool_micro" not in j and "supply" not in j               # конкурентам не видно


def test_launch_price_starts_near_zero_and_grows_softly(conn, monkeypatch):
    monkeypatch.setattr(dcoin, "START_PRICE", 0.000001)
    uid = _user(conn)
    assert dcoin.price(conn) == 0.0
    _buy(conn, uid, 10_000)
    first = dcoin.price(conn)
    assert abs(first - dcoin.START_PRICE) < 1e-9                      # 0.0000109 с. — коротко
    for _ in range(300):
        _buy(conn, uid, 100_000)                                       # $10 каждая
    prices = [r[0] for r in conn.execute("SELECT price FROM dcoin_points WHERE reason = 'buy' ORDER BY id")]
    assert prices[-1] > first * 3                                     # растёт по мере покупок
    for a, b in pairwise(prices[1:]):
        assert abs(b - a) / a <= 0.0301                                 # и мягко, без скачков
    s = dcoin.state(conn)
    everything = dcoin.quote(conn, s["supply"])
    assert everything <= s["pool"]                                     # людям — не больше копилки


def test_launch_keeps_coins_of_existing_holders(conn, monkeypatch):
    uid = _user(conn)
    monkeypatch.setattr(dcoin, "START_PRICE", 1.0)
    db.set_setting(conn, "dcoin.launch", "old")                        # как было до обновления
    _buy(conn, uid, 10_000)
    before = dcoin.price(conn)
    conn.execute("DELETE FROM settings WHERE key = 'dcoin.launch'")
    monkeypatch.setattr(dcoin, "START_PRICE", 0.000001)
    assert abs(dcoin.price(conn) - 0.000001) < 1e-9 < before            # график начался заново со старта
    assert dcoin.balance(conn, uid) == 100 * dcoin.UNIT                 # монеты у людей на месте
    assert conn.execute("SELECT COUNT(*) FROM dcoin_points").fetchone()[0] == 1


def test_price_follows_previous_purchase(conn):
    uid = _user(conn)
    for total in (30_000, 30_000, 30_000):
        _buy(conn, uid, total)
    p0 = dcoin.price(conn)
    _buy(conn, uid, 50_000)                                            # $5 после $3 — вверх
    p1 = dcoin.price(conn)
    _buy(conn, uid, 40_000)                                            # $4 после $5 — вниз
    p2 = dcoin.price(conn)
    _buy(conn, uid, 60_000)                                            # $6 после $4 — снова вверх
    p3 = dcoin.price(conn)
    assert p1 > p0 and p2 < p1 and p3 > p2
    for a, b in ((p0, p1), (p1, p2), (p2, p3)):
        assert 0.0099 <= abs(b - a) / a <= 0.0301                        # от 1% до 3% — видно, но мягко


def test_history_for_scrolling_back(conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    assert len(dcoin.candles(conn, "1m", 300)["candles"]) == 300          # есть что листать назад
    assert len(dcoin.candles(conn, "1m", 10_000)["candles"]) == dcoin.MAX_CANDLES
    assert dcoin.candles(conn, "1s", 50)["candles"][-1][4] == dcoin.price(conn)


def test_refund_drops_price_by_setting(conn):
    uid = _user(conn)
    for total in (30_000, 30_000):
        _buy(conn, uid, total)
    s0, p0 = dcoin.state(conn), dcoin.price(conn)
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, total_micro, "
        "cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"dx-{n}", uid, "p", "topup", "x", 1, "1", 30_000, 27_600, "processing", f"k{n}", ts, ts))
    assert orders.fail_and_refund(conn, cur.lastrowid, "не выполнен")
    p1 = dcoin.price(conn)
    assert abs(p1 / p0 - 0.98) < 0.001                                  # по умолчанию −2%
    s1 = dcoin.state(conn)
    assert (s1["pool"], s1["supply"]) == (s0["pool"], s0["supply"])    # копилка и монеты людей — как были
    assert not orders.fail_and_refund(conn, cur.lastrowid, "ещё раз")   # второй раз — ни возврата, ни падения
    assert dcoin.price(conn) == p1
    db.set_setting(conn, "dcoin.refund_pct", "0")
    assert dcoin.on_refund(conn, cur.lastrowid) is False                # 0 — цена не падает


def _processing(conn, uid, total):
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, total_micro, "
        "cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"dx-{n}", uid, "p", "topup", "x", 1, "1", total, int(total * 0.92), "processing", f"k{n}", ts, ts))
    dcoin.on_created(conn, cur.lastrowid)                               # как при оформлении покупки
    return cur.lastrowid, f"dx-{n}"


def test_history_keeps_credits_and_refund_debits(conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    oid, pid = _processing(conn, uid, 30_000)                           # $3 → +300 D сразу
    assert dcoin.balance(conn, uid) == 400 * dcoin.UNIT
    assert dcoin.waiting(conn, uid) == 300 * dcoin.UNIT                 # но ждут выполнения
    orders.fail_and_refund(conn, oid, "поставщик отклонил")
    assert dcoin.balance(conn, uid) == 100 * dcoin.UNIT and dcoin.waiting(conn, uid) == 0
    h = dcoin.history(conn, uid)
    assert h[0]["reason"] == f"Заказ {pid} не выполнен — возврат" and h[0]["amount"] == "-300.00" and not h[0]["plus"]
    assert [x["reason"] for x in h[1:]] == [f"Заказ {pid}", "Заказ dx-0"]    # зачисления остались в истории
    orders.fail_and_refund(conn, oid, "ещё раз")
    assert dcoin.balance(conn, uid) == 100 * dcoin.UNIT                  # второй раз не списывает


def test_coins_given_at_purchase_become_real_on_completion(conn):
    uid = _user(conn)
    _buy(conn, uid, 10_000)
    pool = dcoin.state(conn)["pool"]
    oid, _ = _processing(conn, uid, 20_000)
    assert orders.complete(conn, oid, {})
    assert dcoin.balance(conn, uid) == 300 * dcoin.UNIT                 # не начислено дважды
    assert dcoin.waiting(conn, uid) == 0 and dcoin.state(conn)["pool"] > pool
    assert len(dcoin.history(conn, uid)) == 2


def test_waiting_coins_cannot_be_exchanged(conn, monkeypatch):
    monkeypatch.setattr(dcoin, "START_PRICE", 1.0)
    uid = _user(conn)
    for _ in range(5):
        _buy(conn, uid, 1_000_000, cost=500_000)
    _processing(conn, uid, 1_000_000)
    later = datetime.now(timezone.utc) + timedelta(days=31)
    try:
        dcoin.exchange(conn, uid, dcoin.balance(conn, uid), now=later)
        raise AssertionError("монеты за невыполненный заказ обменивать нельзя")
    except dcoin.ExchangeError as exc:
        assert "ждёт выполнения" in str(exc)


def test_real_order_rejected_by_supplier_shows_refund_in_history(client, conn, shop, supplier):
    supplier.fail_next = "reject_other"
    r = client.post("/api/v1/orders", headers=shop["h"],
                    json={"product_id": "tg-stars", "quantity": 100, "fields": {"telegram_username": "@player_one"}})
    pid = r.json()["order"]["order_id"]
    h = dcoin.history(conn, shop["id"])
    assert h[0]["reason"] == f"Заказ {pid} не выполнен — возврат" and not h[0]["plus"]
    assert h[1]["reason"] == f"Заказ {pid}" and h[1]["plus"]
    assert dcoin.balance(conn, shop["id"]) == 0


def test_candles_have_wicks_like_an_exchange(conn):
    uid = _user(conn)
    for total in (30_000, 30_000, 60_000):                              # последняя — больше предыдущей: вверх
        _buy(conn, uid, total)
    k = dcoin.candles(conn, "1d", 10)["candles"][-1]                    # [время, откр., макс., мин., закр.]
    body_top = max(k[1], k[4])
    assert k[2] > body_top                                               # верхняя тень выше тела
    assert k[4] == dcoin.price(conn)                                     # закрытие — текущая цена, без «прострела»
    assert conn.execute("SELECT COUNT(*) FROM dcoin_points WHERE reason = 'peak'").fetchone()[0] >= 2


def test_too_many_zeros_rebased_once_with_history(conn, monkeypatch):
    uid = _user(conn)
    db.set_setting(conn, "dcoin.rebase1", "skip")                       # как было на сервере: цена в нулях
    monkeypatch.setattr(dcoin, "START_PRICE", 1e-11)
    for total in (30_000, 50_000, 40_000):
        _buy(conn, uid, total)
    before = [r[0] for r in conn.execute("SELECT price FROM dcoin_points ORDER BY id")]
    assert dcoin.price(conn) < 1e-9
    monkeypatch.setattr(dcoin, "START_PRICE", 0.000002)
    conn.execute("DELETE FROM settings WHERE key = 'dcoin.rebase1'")
    p = dcoin.price(conn)
    assert 0.0000002 <= p <= 0.00002                                     # ≈ 0.0000197 с., а не 0.000000000197
    after = [r[0] for r in conn.execute("SELECT price FROM dcoin_points ORDER BY id")]
    factor = after[-1] / before[-1]
    assert all(abs(a / b - factor) < 1e-6 for a, b in zip(after, before) if b)   # вся история сдвинута одинаково
    assert dcoin.quote(conn, dcoin.state(conn)["supply"]) <= dcoin.state(conn)["pool"]   # минуса нет
    assert dcoin.price(conn) == p                                         # второй раз не двигает


def test_change_resets_at_midnight_and_past_days_kept(conn):
    from donatix import timez
    uid = _user(conn)
    tz = timez.zone(timez.site_zone_name(conn))
    today = datetime.now(timezone.utc).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for total in (30_000, 50_000, 60_000):
        _buy(conn, uid, total)
    # первые две покупки — «вчера» (до полуночи), последняя — сегодня
    ids = [r[0] for r in conn.execute("SELECT id FROM dcoin_points ORDER BY id")]
    yesterday = (today - timedelta(hours=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    conn.execute(f"UPDATE dcoin_points SET ts = ? WHERE id IN ({','.join('?' * (len(ids) - 2))})",
                 (yesterday, *ids[:-2]))
    open_today = [r[0] for r in conn.execute("SELECT price FROM dcoin_points ORDER BY id")][-3]
    now_p = dcoin.price(conn)
    assert dcoin.change_today(conn) == round((now_p - open_today) / open_today * 100, 2)   # только с 00:00
    past = dcoin.days(conn)
    assert past and past[0]["day"].date() == (today - timedelta(days=1)).date()           # вчера в истории
    assert past[0]["high"] >= past[0]["close"] and past[0]["low"] <= past[0]["open"]
