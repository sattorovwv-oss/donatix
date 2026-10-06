from decimal import Decimal

from conftest import balance

from donatix import db, orders, supplier_queue


def _stars(client, h, user="@player_one"):
    return client.post("/api/v1/orders", headers=h,
                       json={"product_id": "tg-stars", "quantity": 50, "fields": {"telegram_username": user}})


def _status(conn, public_id):
    return conn.execute("SELECT status, supplier_status, supplier_order_id FROM orders WHERE public_id = ?",
                        (public_id,)).fetchone()


def test_no_money_at_supplier_orders_wait_in_queue(client, conn, shop, supplier, config, monkeypatch):
    sent_admin = []
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent_admin.append(text))
    calls = []
    real = supplier.create_order
    supplier.create_order = lambda *a, **k: calls.append(1) or real(*a, **k)

    supplier.fail_next = "reject"                                   # «недостаточно средств»
    first = _stars(client, shop["h"]).json()["order"]
    assert first["status"] == "processing"                          # не отменён, клиент видит «в обработке»
    assert balance(conn, shop["id"]) < 1_000_000                    # деньги клиента не возвращены — заказ живой
    assert supplier_queue.on_hold(conn, "mock") and len(calls) == 1

    second = _stars(client, shop["h"], "@player_two").json()["order"]
    assert second["status"] == "processing" and len(calls) == 1     # на паузе — запрос поставщику не ушёл
    assert supplier_queue.queued_count(conn) == 2
    orders.process_pending(conn, supplier)                           # обычный воркер очередь не трогает
    assert len(calls) == 1

    supplier._balance = Decimal("0")                                 # деньги ещё не появились
    supplier_queue.dispatch(conn, config, supplier, now=1000)
    assert len(calls) == 1 and "не хватает денег" in sent_admin[-1]

    supplier._balance = Decimal("100")                               # пополнили
    supplier_queue.dispatch(conn, config, supplier, now=1030)        # баланс смотрим раз в минуту — рано
    assert len(calls) == 1
    supplier_queue.dispatch(conn, config, supplier, now=1100)
    assert len(calls) == 3 and not supplier_queue.on_hold(conn, "mock")
    assert "появились деньги" in sent_admin[-1]
    for o in (first, second):
        row = _status(conn, o["order_id"])
        assert row["supplier_order_id"] and not (row["supplier_status"] or "").startswith("queued")
    assert supplier_queue.queued_count(conn) == 0


def test_queue_sends_at_most_50_per_minute_in_order(client, conn, shop, supplier, config):
    for i in range(60):
        conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, fields_json, "
                     "unit_price, total_micro, cost_micro, status, supplier_idem_key, supplier_status, created_at, "
                     "updated_at) VALUES (?, ?, 'tg-stars', 'telegram_stars', 'Stars', 50, "
                     "'{\"telegram_username\": \"@player_one\"}', '1', 1000, 900, 'processing', ?, ?, ?, ?)",
                     (f"dx-q{i}", shop["id"], f"kq{i}", supplier_queue.mark("mock"), db.now(), db.now()))
    first_ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY id LIMIT 50")]
    assert supplier_queue.dispatch(conn, config, supplier, now=5000) == 50
    sent = {r["id"] for r in conn.execute("SELECT id FROM orders WHERE supplier_order_id IS NOT NULL")}
    assert sent == set(first_ids)                                    # по порядку
    assert supplier_queue.dispatch(conn, config, supplier, now=5030) == 0   # в эту минуту — лимит
    assert supplier_queue.dispatch(conn, config, supplier, now=5061) == 10


def test_money_runs_out_again_mid_queue(client, conn, shop, supplier, config):
    supplier.fail_next = "reject"
    _stars(client, shop["h"])
    _stars(client, shop["h"], "@player_two")
    db.set_setting(conn, "supplier.hold.mock", "")                   # будто деньги появились
    supplier.fail_next = "reject"                                    # но на первом снова не хватило
    supplier_queue.dispatch(conn, config, supplier, now=9000)
    assert supplier_queue.on_hold(conn, "mock") and supplier_queue.queued_count(conn) == 2
