"""Поддельный чек: сумма списывается (можно в минус), долг закрывается из следующего пополнения."""

from conftest import add_receipt, balance, web_login, csrf_of
from fastapi.testclient import TestClient

from donatix import accounts, payments, receipt_ai, tgbot


def _paid(conn, config, uid, amount="10"):
    pid = payments.create(conn, config, accounts.get_user(conn, uid), "alif", amount)
    add_receipt(conn, pid)
    payments.confirm(conn, config, pid, 1)
    return pid


def test_fake_receipt_debt_taken_from_next_topup(app, config, conn):
    config.pay_methods = {"alif": "Алиф: 102208383"}
    uid = accounts.create_user(conn, email="f@example.com", login="faker", password="password123", status="active")
    pid = _paid(conn, config, uid, "10")
    assert balance(conn, uid) == 100_000
    accounts.post_ledger(conn, uid, -80_000, "Заказ")                 # успел потратить
    debt = payments.mark_fake(conn, config, pid, 1)
    assert balance(conn, uid) == -80_000 and debt == 80_000          # ушёл в минус — долг
    assert conn.execute("SELECT status FROM payments WHERE id = ?", (pid,)).fetchone()[0] == "fake"
    assert accounts.get_user(conn, uid)["fraud_count"] == 1
    pid2 = payments.create(conn, config, accounts.get_user(conn, uid), "alif", "20")
    text, _ = tgbot.payment_event(conn, pid2, config)
    assert "поддельный чек" in text and "Долг $8.0000" in text       # админ предупреждён
    add_receipt(conn, pid2)
    payments.confirm(conn, config, pid2, 1)
    assert balance(conn, uid) == 120_000                              # 20 − 8 долга
    note = conn.execute("SELECT text FROM notifications WHERE user_id = ? ORDER BY id DESC", (uid,)).fetchone()[0]
    assert "погашение долга" in note


def test_admin_marks_fake_on_site(app, config, conn):
    config.pay_methods = {"alif": "Алиф: 102208383"}
    uid = accounts.create_user(conn, email="g@example.com", login="faker2", password="password123", status="active")
    pid = _paid(conn, config, uid, "5")
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/payments?status=paid").text
    assert "Чек поддельный" in page
    admin.post(f"/admin/payments/{pid}/fake", data={"csrf": csrf_of(page)})
    assert balance(conn, uid) == 0
    assert "Поддельный чек" in admin.get("/admin/payments?status=fake").text


def test_receipt_recipient_and_time_checks():
    seen = {"is_receipt": True, "bank": "DC", "amount": 3253.0, "currency": "TJS", "datetime": "2026-10-04 15:25",
            "txn_id": "", "recipient": "9762000105938780", "status": "success"}
    bad = receipt_ai.summary(seen, "3253", "TJS", our_details="Алиф: 102208383",
                             created_at="2026-10-04T10:20:00.000Z")
    assert "НЕ наши реквизиты" in bad
    ok = receipt_ai.summary(seen, "3253", "TJS", our_details="DC карта 9762 0001 0593 8780",
                            created_at="2026-10-04T10:20:00.000Z")
    assert "наши реквизиты" in ok and "НЕ" not in ok
    old = receipt_ai.summary(seen, "3253", "TJS", our_details="", created_at="2026-10-05T12:00:00.000Z")
    assert "раньше заявки" in old
