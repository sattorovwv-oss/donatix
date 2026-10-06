from conftest import add_receipt, web_login
from fastapi.testclient import TestClient

from donatix import accounts, bots, notify, payments


class Now:
    """threading.Thread, который выполняется сразу — чтобы проверить отправку."""
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_payment_events_reach_bot_owner_in_telegram(app, config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify.threading, "Thread", Now)
    monkeypatch.setattr(notify, "_send_telegram", lambda token, chats, text: sent.append((token, chats, text)))
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: None)
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00"}
    uid = accounts.create_user(conn, email="p@example.com", login="partner1", password="password123",
                               status="active")
    bots.create(conn, config, user_id=uid, token="1:AAAAAA", admin_ids="777, 888", username="p_bot")
    user = accounts.get_user(conn, uid)

    pid = payments.create(conn, config, user, "alif", "20")
    add_receipt(conn, pid)
    payments.confirm(conn, config, pid, 1)
    assert sent and sent[-1][0] == "1:AAAAAA" and sent[-1][1] == ["777", "888"]
    assert "Баланс пополнен на $20" in sent[-1][2]

    pid = payments.create(conn, config, user, "alif", "20")
    payments.reject(conn, config, pid, 1, "Перевод не найден")
    assert "отклонен" in sent[-1][2].lower()

    pid = payments.create(conn, config, user, "alif", "20")
    c = TestClient(app)
    token = web_login(c, "p@example.com", "password123")
    c.post(f"/panel/balance/{pid}/cancel", data={"csrf": token})
    assert f"#{pid} отменена" in sent[-1][2]


def test_no_bot_no_telegram(config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send_telegram", lambda *a: sent.append(a))
    uid = accounts.create_user(conn, email="s@example.com", login="site1", password="password123", status="active")
    notify.notify(conn, config, uid, "проверка")
    assert sent == [] and conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (uid,)).fetchone()[0]
