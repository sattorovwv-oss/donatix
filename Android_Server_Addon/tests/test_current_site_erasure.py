"""Contracts for the customer's October 9 guest purchases and support tickets."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from donatix import accounts, db, web
from test_extension import setup as base_setup, login, event, register
from test_history_erasure import setup, delete, payment, other
from donatix_android_extension.factory import PREFIX

tickets = pytest.importorskip("donatix.tickets")
from donatix import quickbuy, housekeeping, cryptopay

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_history_json_does_not_depend_on_or_replace_website_html_renderer(setup, monkeypatch):
    app, _, _, _ = setup
    client, _ = login(app)
    def broken_template(*args, **kwargs):
        raise RuntimeError("HTML is intentionally unavailable in this contract")
    monkeypatch.setattr(web, "render", broken_template)
    response = client.get(PREFIX + "/orders", params={"page": 2**80})
    assert response.status_code == 200
    assert response.json()["page"] == 100_000
    assert response.json()["items"] == []
    assert web.render is broken_template


def test_current_account_erasure_clears_guest_intents_tickets_and_files_only_for_owner(setup, monkeypatch):
    app, config, _, uid = setup
    monkeypatch.setattr(tickets, "alert_admin", lambda *args, **kwargs: None)
    with db.connect(config.db_path) as c:
        stranger = other(c)
        own_key = quickbuy.issue_key(c, uid)
        other_key = quickbuy.issue_key(c, stranger)
        own_payment = payment(c, uid)
        other_payment = payment(c, stranger)
        own_intent = {"title": "Private player 1122", "items": [{"fields": {"uid": "1122"}}]}
        other_intent = {"title": "Other player 5566", "items": [{"fields": {"uid": "5566"}}]}
        c.execute("UPDATE payments SET intent=?,intent_order='!Private player 1122' WHERE id=?", (json.dumps(own_intent), own_payment))
        c.execute("UPDATE payments SET intent=?,intent_order='OTHER-ORDER' WHERE id=?", (json.dumps(other_intent), other_payment))
        other_ticket = tickets.create(c, config, stranger, "Other subject", "other", "OTHER-ORDER", "Other message", PNG)
        own_ticket = tickets.create(c, config, uid, "Private subject", "payment", "OWN", "Private message", PNG)
        own_file = tickets.messages(c, own_ticket)[0]["file"]
        other_file = tickets.messages(c, other_ticket)[0]["file"]
        other_rows = {table: [tuple(r) for r in c.execute(f"SELECT * FROM {table} WHERE " +
                       ("id=?" if table == "users" else "user_id=?"), (stranger,))]
                      for table in ("users", "guest_keys", "payments", "tickets")}
        other_messages = [tuple(r) for r in tickets.messages(c, other_ticket)]
        c.execute("INSERT INTO player_names(game,uid,valid,player_name,checked_at) VALUES('game','1122',1,'Own nickname',0),('game','5566',1,'Other nickname',0)")
    client, token = login(app)
    client.cookies.set("dx_guest", own_key)
    reply = delete(client, token)
    assert reply.json()["state"] == "completed"
    assert "dx_guest=\"\"" in reply.headers["set-cookie"] or "dx_guest=;" in reply.headers["set-cookie"]
    folder = Path(config.db_path).parent / "tickets"
    assert not (folder / own_file).exists() and (folder / other_file).exists()
    with db.connect(config.db_path) as c:
        assert quickbuy.code_of(c, uid) == ""
        own = c.execute("SELECT * FROM payments WHERE id=?", (own_payment,)).fetchone()
        assert own["intent"] is None and own["intent_order"] is None
        assert own["amount_micro"] == 12500 and own["status"] == "paid"
        tombstone = c.execute("SELECT * FROM tickets WHERE id=?", (own_ticket,)).fetchone()
        assert tombstone["subject"] == "" and tombstone["status"] == "closed" and tombstone["order_ref"] is None
        assert tickets.get(c, own_ticket) is None and not tickets.messages(c, own_ticket)
        for action in (lambda: tickets.add(c, config, own_ticket, "admin", "Late reply", PNG),
                       lambda: tickets.create(c, config, uid, "Late subject", "other", "", "Late data", PNG),
                       lambda: tickets.set_status(c, own_ticket, closed=False)):
            with pytest.raises(tickets.TicketError):
                action()
        assert tickets.create(c, config, stranger, "New subject", "other", "", "New message") > own_ticket
        for table, rows in other_rows.items():
            now = [tuple(r) for r in c.execute(f"SELECT * FROM {table} WHERE " +
                   ("id=?" if table == "users" else "user_id=?"), (stranger,))]
            if table == "tickets":
                now = now[:len(rows)]
            assert now == rows
        assert [tuple(r) for r in tickets.messages(c, other_ticket)] == other_messages
        assert quickbuy.code_of(c, stranger) and other_key
        assert not c.execute("SELECT 1 FROM player_names WHERE uid='1122'").fetchone()
        assert c.execute("SELECT 1 FROM player_names WHERE uid='5566'").fetchone()


def test_guest_access_is_revoked_immediately_while_paid_purchase_waits(setup):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        c.execute("UPDATE users SET email='gowner@guest.donatix.tj' WHERE id=?", (uid,))
        key = quickbuy.issue_key(c, uid)
        assert quickbuy.restore(c, key) == uid
        pid = payment(c, uid)
        c.execute("UPDATE payments SET intent=?,intent_order=NULL WHERE id=?", (json.dumps({"items": [], "title": "Private purchase"}), pid))
    client, token = login(app, "gowner@guest.donatix.tj")
    result = delete(client, token).json()
    assert result["state"] == "waiting" and any("покупка" in r.lower() for r in result["reasons"])
    with db.connect(config.db_path) as c:
        assert quickbuy.restore(c, key) is None and quickbuy.code_of(c, uid) == ""
        assert c.execute("SELECT intent FROM payments WHERE id=?", (pid,)).fetchone()[0]
        c.execute("UPDATE payments SET intent_order='!settled' WHERE id=?", (pid,))
    app.state.android_erasure.run_once()
    with db.connect(config.db_path) as c:
        assert accounts.get_user(c, uid)["login"].startswith("deleted_")
        assert c.execute("SELECT intent FROM payments WHERE id=?", (pid,)).fetchone()[0] is None


def test_erasure_retains_crypto_transfer_identifier_and_prevents_recredit(setup, monkeypatch):
    app, config, _, uid = setup
    with db.connect(config.db_path) as c:
        stranger = other(c)
        own = payment(c, uid)
        c.execute("UPDATE payments SET ext_id='settled-transfer' WHERE id=?", (own,))
        pending = payment(c, stranger, status="pending")
        c.execute("UPDATE payments SET auto_kind='trc20',pay_address='wallet' WHERE id=?", (pending,))
    client, token = login(app)
    assert delete(client, token).json()["state"] == "completed"
    monkeypatch.setattr(cryptopay, "incoming_usdt", lambda *args: [
        {"tx": "settled-transfer", "amount": Decimal("12.5"), "ts": int(time.time()*1000)}])
    with db.connect(config.db_path) as c:
        assert c.execute("SELECT ext_id FROM payments WHERE id=?", (own,)).fetchone()[0] == "settled-transfer"
        assert cryptopay.check_trc20(c, config) == 0
        assert c.execute("SELECT status FROM payments WHERE id=?", (pending,)).fetchone()[0] == "pending"
        assert accounts.get_user(c, stranger)["balance_micro"] == 0
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE payments SET ext_id='settled-transfer' WHERE id=?", (pending,))


def test_daily_site_cleanup_cannot_reuse_erased_notification_ids(setup):
    app, config, sender, uid = setup
    with db.connect(config.db_path) as c:
        stranger = other(c)
    client, token = login(app)
    other_client, other_token = login(app, "other@example.com")
    assert register(other_client, other_token, device="b"*32).status_code == 200
    worker = app.state.android_worker
    worker.scan()
    highest = event(config, uid)
    old = (datetime.now(timezone.utc)-timedelta(days=400)).isoformat()
    with db.connect(config.db_path) as c:
        c.execute("UPDATE notifications SET created_at=? WHERE id=?", (old, highest))
    worker.scan()
    assert delete(client, token).json()["state"] == "completed"
    with db.connect(config.db_path) as c:
        housekeeping.run(c)
        row = c.execute("SELECT * FROM notifications WHERE id=?", (highest,)).fetchone()
        assert row and row["text"] == "" and row["read_at"] is None
    assert event(config, stranger) > highest
    worker.scan()
    worker.dispatch()
    assert len(sender.calls) == 1 and sender.calls[0][1]["user_id"] == stranger


def test_ticket_write_and_erasure_are_serialized_for_same_owner(setup, monkeypatch):
    app, config, _, uid = setup
    monkeypatch.setattr(tickets, "alert_admin", lambda *args, **kwargs: None)
    started, release, written, finished = (threading.Event() for _ in range(4))
    original_save = tickets._save_file
    def slow_save(*args):
        started.set()
        assert release.wait(3)
        return original_save(*args)
    monkeypatch.setattr(tickets, "_save_file", slow_save)
    errors = []
    def write_ticket():
        try:
            with db.connect(config.db_path) as c:
                tickets.create(c, config, uid, "In-flight", "other", "", "Private text", PNG)
            written.set()
        except BaseException as e:
            errors.append(e)
    writer = threading.Thread(target=write_ticket)
    writer.start()
    assert started.wait(3)
    client, token = login(app)
    responses = []
    def erase():
        try:
            responses.append(delete(client, token))
        except BaseException as e:
            errors.append(e)
        finally:
            finished.set()
    deleter = threading.Thread(target=erase)
    deleter.start()
    # Deletion must wait for the file writer, not leave a late attachment.
    assert not finished.wait(0.1)
    release.set()
    writer.join(3)
    deleter.join(3)
    assert not errors and written.is_set() and finished.is_set()
    assert responses[0].json()["state"] == "completed"
    with db.connect(config.db_path) as c:
        assert not c.execute("SELECT 1 FROM ticket_messages m JOIN tickets t ON t.id=m.ticket_id WHERE t.user_id=?", (uid,)).fetchone()
    assert not list((Path(config.db_path).parent/"tickets").glob("*"))
