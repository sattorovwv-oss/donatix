import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from donatix import accounts, db, dcoin, security
from donatix_android_extension.erasure import Erasure
from donatix_android_extension.factory import PREFIX
from test_extension import setup as base_setup, login, register, event, csrf


@pytest.fixture
def setup(base_setup):
    # Keep deterministic tests fast; production always drains for 45 seconds.
    base_setup[0].state.android_erasure.grace_seconds = 0
    return base_setup


def order(conn, uid, public, status="completed", fields=None, created=None):
    now = created or db.now()
    return conn.execute("""INSERT INTO orders(public_id,user_id,product_id,kind,product_name,
        quantity,fields_json,unit_price,total_micro,cost_micro,status,supplier_idem_key,
        delivery_json,created_at,updated_at,completed_at)
        VALUES(?,?,'fixture','topup','Game package',1,?,'1.2500',12500,10000,?,?,?, ?,?,?)""",
        (public, uid, json.dumps(fields or {"uid": "12345"}), status, "supplier-" + public,
         json.dumps({"code": "private-delivery"}), now, now, now if status == "completed" else None)).lastrowid


def payment(conn, uid, status="paid", receipt=None):
    return conn.execute("""INSERT INTO payments(user_id,method,amount_micro,pay_amount,pay_currency,
        reference,receipt_file,status,created_at,receipt_hash,receipt_txn,receipt_fp)
        VALUES(?,'alif',12500,'12.5','TJS','private-note',?,?,?,'hash','payment-id','fingerprint')""",
        (uid, receipt, status, db.now())).lastrowid


def delete(client, token, **values):
    return client.post(PREFIX + "/account/deletion", headers={"X-CSRF-Token": token},
                       json={"confirmation": "УДАЛИТЬ", "password": "password123", **values})


def other(conn):
    return accounts.create_user(conn, email="other@example.com", login="other", password="password123", status="active")


def test_json_history_is_authenticated_empty_and_readonly(setup):
    app, config, _, _ = setup
    guest = TestClient(app)
    assert guest.get(PREFIX + "/orders").status_code == 401
    client, _ = login(app)
    with db.connect(config.db_path) as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM orders")]
    response = client.get(PREFIX + "/orders")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {"ok": True, "items": [], "total": 0, "page": 1, "limit": 30,
                    "period": "Всё время", "totals": {"done": 0, "failed": 0, "spent": "0.0000"}}
    with db.connect(config.db_path) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM orders")] == before
    assert guest.get(PREFIX + "/config").json()["account_deletion"] is True


def test_history_uses_original_filters_pages_and_hides_other_customers(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        stranger = other(c)
        order(c, stranger, "SECRET", fields={"uid": "private-player"})
        for i in range(31):
            order(c, uid, f"DX{i}")
        order(c, uid, "ATTENTION", "attention", {"uid": "query-player"})
        order(c, uid, "FAILED", "failed", created="2020-01-01T00:00:00")
    client, _ = login(app)
    all_rows = client.get(PREFIX + "/orders", params={"page": 2}).json()
    assert all_rows["total"] == 33 and len(all_rows["items"]) == 3
    assert all_rows["totals"] == {"done": 31, "failed": 1, "spent": "38.7500"}
    assert all(r["order_id"] != "SECRET" for r in all_rows["items"])
    active = client.get(PREFIX + "/orders", params={"status": "processing", "q": "query-player"}).json()
    assert active["total"] == 1 and active["items"][0]["status"] == "processing"
    period = client.get(PREFIX + "/orders", params={"date_from": "2020-01-01", "date_to": "2020-01-01"}).json()
    assert period["total"] == 1 and period["items"][0]["order_id"] == "FAILED"
    # The website's ordinary history route continues working.
    assert client.get("/panel/orders").status_code == 200


@pytest.mark.parametrize("field,value,status", [("password", "wrong", 403), ("confirmation", "DELETE", 422)])
def test_erasure_requires_identity_and_explicit_confirmation(setup, field, value, status):
    app, config, _, uid = setup
    client, token = login(app)
    assert delete(client, token, **{field: value}).status_code == status
    assert delete(client, "forged").status_code == 403
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["status"] == "active"
    assert client.get(PREFIX + "/session").status_code == 200


def test_old_login_requires_password_but_fresh_login_allows_google_style_confirmation(setup):
    app, config, _, uid = setup
    client, token = login(app)
    with db.connect(config.db_path) as c:
        old = (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%S")
        c.execute("UPDATE logins SET created_at=? WHERE user_id=?", (old, uid))
    assert delete(client, token, password="").status_code == 403
    with db.connect(config.db_path) as c:
        c.execute("UPDATE logins SET created_at=? WHERE user_id=?", (db.now(), uid))
    assert delete(client, token, password="").json()["state"] == "completed"


def test_full_erasure_preserves_other_users_and_anonymous_accounting(setup, tmp_path):
    app, config, sender, uid = setup
    receipt_dir = Path(config.db_path).parent / "receipts"
    receipt_dir.mkdir()
    (receipt_dir / "own.jpg").write_bytes(b"private receipt")
    (receipt_dir / "other.jpg").write_bytes(b"other customer's receipt")
    with db.connect(config.db_path) as c:
        stranger = other(c)
        c.execute("UPDATE users SET google_sub='google-owner',phone='+992111',project='Private project' WHERE id=?", (uid,))
        own_order = order(c, uid, "OWN")
        other_order = order(c, stranger, "OTHER", fields={"uid": "67890"})
        own_payment = payment(c, uid, receipt="own.jpg")
        payment(c, stranger, receipt="other.jpg")
        accounts.create_api_key(c, uid, "Mobile", config.secret_key)
        c.execute("INSERT INTO support_links VALUES(111,?,?)", (uid, db.now()))
        c.execute("INSERT INTO support_history(tg_id,role,content,created_at) VALUES(111,'user','private chat',?)", (db.now(),))
        c.execute("INSERT INTO bank_notices(source,message_id,op_code,sender,card_tail,comment,seen_at,status,payment_id,body) VALUES('fixture',1,'operation-id','private sender','1234','private comment',?,'matched',?,'private body')", (db.now(), own_payment))
        c.execute("INSERT INTO player_names(game,uid,valid,player_name,checked_at) VALUES('game','12345',1,'Own name',0),('game','67890',1,'Other name',0)")
        before_user = tuple(accounts.get_user(c, stranger))
        before_order = tuple(c.execute("SELECT * FROM orders WHERE id=?", (other_order,)).fetchone())
        before_payment = tuple(c.execute("SELECT * FROM payments WHERE user_id=?", (stranger,)).fetchone())
    client, token = login(app)
    register(client, token)
    event(config, uid)
    app.state.android_worker.scan()
    r = delete(client, token)
    assert r.status_code == 200 and r.json()["state"] == "completed"
    assert client.get(PREFIX + "/session").status_code == 401
    assert not (receipt_dir / "own.jpg").exists() and (receipt_dir / "other.jpg").exists()
    with db.connect(config.db_path) as c:
        u = accounts.get_user(c, uid)
        assert u["status"] == "blocked" and u["login"].startswith("deleted_")
        assert u["google_sub"] is None and u["phone"] is None and u["project"] is None
        assert accounts.authenticate(c, "user@example.com", "password123") is None
        for table in ("logins", "api_keys", "push_subs", "support_links"):
            assert c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0] == 0
        assert not c.execute("SELECT 1 FROM notifications WHERE user_id=? AND (text<>'' OR link IS NOT NULL OR read_at IS NULL)", (uid,)).fetchone()
        assert not c.execute("SELECT 1 FROM support_history WHERE tg_id=111").fetchone()
        assert not c.execute("SELECT 1 FROM player_names WHERE uid='12345'").fetchone()
        assert tuple(accounts.get_user(c, stranger)) == before_user
        assert tuple(c.execute("SELECT * FROM orders WHERE id=?", (other_order,)).fetchone()) == before_order
        assert tuple(c.execute("SELECT * FROM payments WHERE user_id=?", (stranger,)).fetchone()) == before_payment
        p = c.execute("SELECT * FROM payments WHERE id=?", (own_payment,)).fetchone()
        assert p["receipt_hash"] == "hash" and p["receipt_txn"] == "payment-id" and p["receipt_fp"] == "fingerprint"
        assert p["receipt_file"] is None and p["reference"] is None and p["amount_micro"] == 12500 and p["status"] == "paid"
        o = c.execute("SELECT * FROM orders WHERE id=?", (own_order,)).fetchone()
        assert o["fields_json"] == "{}" and o["delivery_json"] is None and o["total_micro"] == 12500
        notice = c.execute("SELECT * FROM bank_notices").fetchone()
        assert notice["op_code"] == "operation-id" and not notice["sender"] and not notice["body"]
        # Reserved IDs cannot be recycled into an unrelated new account.
        assert accounts.create_user(c, email="new@example.com", login="new", password="password123", status="active") > stranger
    with app.state.android_store.connect() as c:
        for table in ("devices", "outbox", "deletions", "erasure_files"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    app.state.android_worker.dispatch()
    assert not sender.calls


def test_pending_operations_wait_for_settlement_without_losing_money(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        c.execute("UPDATE users SET balance_micro=12345 WHERE id=?", (uid,))
        order(c, uid, "PENDING", "attention")
        payment(c, uid, status="pending")
    client, token = login(app)
    r = delete(client, token)
    assert r.status_code == 200 and r.json()["state"] == "waiting" and len(r.json()["reasons"]) == 3
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["balance_micro"] == 12345
        assert accounts.get_user(c, uid)["email"] == "user@example.com"
        assert c.execute("SELECT status FROM orders").fetchone()[0] == "attention"
        # Simulate the existing site's normal operator settlement, never a live payment.
        c.execute("UPDATE orders SET status='completed' WHERE user_id=?", (uid,))
        c.execute("UPDATE payments SET status='cancelled' WHERE user_id=?", (uid,))
        c.execute("UPDATE users SET balance_micro=0 WHERE id=?", (uid,))
    restarted = Erasure(config, app.state.android_store)
    restarted.grace_seconds = 0
    restarted.run_once()
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["login"].startswith("deleted_")


def test_owner_account_cannot_be_erased(setup):
    app, config, _, uid = setup
    client, token = login(app)
    with db.connect(config.db_path) as c:
        c.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
    assert delete(client, token).status_code == 409
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["status"] == "active"


def test_private_file_cleanup_retries_and_does_not_follow_symlinks(setup, tmp_path, monkeypatch):
    app, config, _, uid = setup
    receipt_dir = Path(config.db_path).parent / "receipts"
    receipt_dir.mkdir()
    receipt = receipt_dir / "own.jpg"
    receipt.write_bytes(b"receipt")
    with db.connect(config.db_path) as c:
        payment(c, uid, receipt="own.jpg")
    original = Path.unlink
    def locked(path, *args, **kwargs):
        if path == receipt:
            raise PermissionError("temporarily locked")
        return original(path, *args, **kwargs)
    client, token = login(app)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        assert delete(client, token).json()["state"] == "waiting"
    restarted = Erasure(config, app.state.android_store)
    restarted.grace_seconds = 0
    restarted.run_once()
    assert not receipt.exists()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete")
    link = receipt_dir / "linked.jpg"
    link.symlink_to(outside)
    with app.state.android_store.connect() as c:
        c.execute("INSERT INTO erasure_files VALUES(?,'receipt','linked.jpg',0)", (uid,))
    with db.connect(config.db_path) as c:
        app.state.android_erasure._files(c, uid)
    assert outside.read_text() == "do not delete" and link.is_symlink()


def test_bot_credentials_and_files_are_removed_without_reusing_ids(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        bot = c.execute("INSERT INTO bots(user_id,token_enc,username,admin_ids,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (uid, security.seal(config.secret_key, "secret-token"), "private_bot", "111", db.now(), db.now())).lastrowid
    folder = Path(config.db_path).parent / "bots" / str(bot)
    folder.mkdir(parents=True)
    (folder / ".env").write_text("PRIVATE_BOT_TOKEN")
    client, token = login(app)
    assert delete(client, token).json()["state"] == "waiting"
    with app.state.android_store.connect() as c:
        c.execute("UPDATE erasure_files SET after_ts=0")
    app.state.android_erasure.run_once()
    assert not folder.exists()
    with db.connect(config.db_path) as c:
        row = c.execute("SELECT * FROM bots WHERE id=?", (bot,)).fetchone()
        assert security.unseal(config.secret_key, row["token_enc"]) == "" and not row["enabled"]
        assert not row["username"] and not row["admin_ids"]


def test_erasing_coins_keeps_other_customers_coin_price(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        dcoin.state(c)
        c.execute("UPDATE users SET dcoin=100 WHERE id=?", (uid,))
        dcoin._point(c, 10000, 300, "fixture", reserve=200)
        before = dcoin.price(c)
    client, token = login(app)
    assert delete(client, token).json()["state"] == "completed"
    with db.connect(config.db_path) as c:
        state = dcoin.state(c)
        assert state == {"pool": 10000, "supply": 200, "reserve": 300}
        assert dcoin.price(c) == before


def test_public_deletion_page_and_authenticated_browser_request(setup):
    app, config, _, uid = setup
    guest = TestClient(app)
    public = guest.get("/android/account-deletion")
    assert public.status_code == 200 and "удаление" in public.text.lower()
    assert "Обезличенные" in public.text
    assert guest.get("/panel/android-account-deletion", follow_redirects=False).headers["location"] == "/login"
    client, _ = login(app)
    token = csrf(client, "/panel/android-account-deletion")
    assert client.post("/panel/android-account-deletion", data={"csrf": token, "confirmation": "x" * 41}).status_code == 422
    assert client.post("/panel/android-account-deletion", data={"csrf": token, "confirmation": "УДАЛИТЬ", "password": "wrong"}).status_code == 403
    response = client.post("/panel/android-account-deletion", data={"csrf": token, "confirmation": "УДАЛИТЬ", "password": "password123"})
    assert response.status_code == 200 and "Данные удалены" in response.text


def test_shared_player_cache_remains_available_to_other_accounts(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        stranger = other(c)
        order(c, uid, "OWN_SHARED")
        order(c, stranger, "OTHER_SHARED")
        c.execute("INSERT INTO player_names(game,uid,valid,player_name,checked_at) VALUES('game','12345',1,'Shared name',0)")
    client, token = login(app)
    assert delete(client, token).json()["state"] == "completed"
    with db.connect(config.db_path) as c:
        assert c.execute("SELECT 1 FROM player_names WHERE uid='12345'").fetchone()
        assert c.execute("SELECT fields_json FROM orders WHERE public_id='OWN_SHARED'").fetchone()[0] == "{}"


def test_inflight_payment_finishes_before_identity_is_erased(setup):
    app, config, _, uid = setup
    app.state.android_erasure.grace_seconds = 45
    client, token = login(app)
    assert delete(client, token).json()["state"] == "waiting"
    assert client.get(PREFIX + "/session").status_code == 401
    with db.connect(config.db_path) as c:
        # A request accepted before the deletion finishes its pending insertion.
        payment(c, uid, status="pending")
        assert accounts.get_user(c, uid)["email"] == "user@example.com"
    with app.state.android_store.connect() as c:
        c.execute("UPDATE deletions SET requested_at=requested_at-46")
    app.state.android_erasure.run_once()
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["email"] == "user@example.com"
        c.execute("UPDATE payments SET status='cancelled' WHERE user_id=?", (uid,))
    with app.state.android_store.connect() as c:
        c.execute("UPDATE deletions SET next_try=0")
    app.state.android_erasure.run_once()
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["login"].startswith("deleted_")


def test_referral_name_is_redacted_without_changing_bonus_amount(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        referrer = other(c)
        oid = order(c, uid, "REFERRAL")
        c.execute("INSERT INTO referral_rewards(order_id,referrer_id,referred_id,amount_micro,created_at) VALUES(?,?,?,?,?)", (oid, referrer, uid, 100, db.now()))
        accounts.post_ledger(c, referrer, 100, "Реферальный бонус: заказ REFERRAL клиента user")
        c.execute("INSERT INTO notifications(user_id,text,created_at) VALUES(?,?,?)", (referrer, "🎁 Реферальный бонус $0.0100: ваш приглашённый user сделал заказ.", db.now()))
    client, token = login(app)
    assert delete(client, token).json()["state"] == "completed"
    with db.connect(config.db_path) as c:
        t = c.execute("SELECT * FROM transactions WHERE user_id=?", (referrer,)).fetchone()
        assert t["amount_micro"] == 100 and t["balance_after"] == 100
        assert "клиента user" not in t["note"]
        assert "приглашённый user" not in c.execute("SELECT text FROM notifications WHERE user_id=?", (referrer,)).fetchone()[0]


def test_erasure_keeps_push_watermarks_working_for_other_customers(setup):
    app, config, sender, uid = setup
    with db.connect(config.db_path) as c:
        stranger = other(c)
    owner, token = login(app)
    another, another_token = login(app, "other@example.com")
    assert register(another, another_token, device="b" * 32).status_code == 200
    worker = app.state.android_worker
    worker.scan()
    last_id = event(config, uid)
    worker.scan()
    assert delete(owner, token).json()["state"] == "completed"
    next_id = event(config, stranger)
    assert next_id > last_id
    worker.scan()
    worker.dispatch()
    assert len(sender.calls) == 1 and sender.calls[0][1]["user_id"] == stranger


def test_waiting_balances_do_not_starve_newer_deletions(setup):
    app, config, _, _ = setup
    manager = app.state.android_erasure
    with db.connect(config.db_path) as c, app.state.android_store.connect() as q:
        for i in range(21):
            uid = c.execute("INSERT INTO users(email,login,password_hash,created_at,balance_micro,status) VALUES(?,?,?,?,?,'blocked')",
                (f"queued{i}@example.com", f"queued{i}", "fixture", db.now(), 100 if i < 20 else 0)).lastrowid
            u = accounts.get_user(c, uid)
            q.execute("INSERT INTO deletions(user_id,identity,marker,state,requested_at) VALUES(?,?,?,'waiting',0)",
                      (uid, manager.identity(u), "deleted_fixture_" + str(i)))
    manager.run_once()
    manager.run_once()
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["login"] == "deleted_fixture_20"
        assert c.execute("SELECT COUNT(*) FROM users WHERE login LIKE 'queued%' AND balance_micro=100").fetchone()[0] == 20
