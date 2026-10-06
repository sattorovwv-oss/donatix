from conftest import RECEIPT, RECEIPT_PNG, balance, web_login
from fastapi.testclient import TestClient

from donatix import accounts, cashiers, payments, tgbot


def _client(app, config, conn, email="c@example.com", login="client1"):
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00 (Али)", "dc": "DC: 1234"}
    uid = accounts.create_user(conn, email=email, login=login, password="password123", status="active")
    client = TestClient(app)
    token = web_login(client, email, "password123")
    return uid, client, token


def _send(client, token, receipt=RECEIPT, amount="50"):
    return client.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": amount}, files=receipt)


def test_one_request_at_a_time_with_waiting_spinner(app, config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: sent.append(a))
    uid, client, token = _client(app, config, conn)
    assert "Заявка #1 создана" in _send(client, token).text
    page = client.get("/panel/balance").text
    assert 'id="wait"' in page and "wait-spin" in page and "Заявка #1 на проверке" in page
    assert 'id="payform"' not in page                                   # формы нет, пока ждём админа
    again = _send(client, token, {"receipt": ("b.png", RECEIPT_PNG + b"2", "image/png")})
    assert "Заявка #1 на проверке" in again.text and len(sent) == 1        # вторая не создана, админу — один чек
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    r = client.post("/panel/balance/1/cancel", data={"csrf": token})
    assert "отменить её нельзя" in r.text                                 # чек отправлен — отменить нельзя
    assert client.get("/panel/data/payment/1").json()["status"] == "pending"

    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    admin.post("/admin/payments/1/confirm", data={"csrf": atoken, "credit": "50"})
    assert balance(conn, uid) == 500_000
    assert client.get("/panel/data/payment/1").json()["status"] == "paid"   # страница клиента обновится сама
    page = client.get("/panel/balance").text
    assert 'id="payform"' in page and 'id="wait"' not in page               # снова можно пополнять
    assert "решил: <b>админ (сайт: admin)</b>" in admin.get("/admin/payments?status=paid").text


def test_same_receipt_cannot_be_used_twice(app, config, conn, monkeypatch):
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: None)
    uid, client, token = _client(app, config, conn)
    _send(client, token)
    _, other, otoken = _client(app, config, conn, "d@example.com", "client2")
    r = _send(other, otoken)                                             # тот же файл чека с другого аккаунта
    assert "Чек не прошёл проверку" in r.text
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_draft_without_receipt_does_not_block(config, conn):
    uid = accounts.create_user(conn, email="e@example.com", login="bot1", password="password123", status="active")
    config.pay_methods = {"alif": "Алиф"}
    user = accounts.get_user(conn, uid)
    first = payments.create(conn, config, user, "alif", "10")            # бот создал, а чек так и не прислал
    second = payments.create(conn, config, user, "alif", "20")
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, resolved_who FROM payments")}
    assert rows[first]["status"] == "cancelled" and rows[first]["resolved_who"] == "заменена новой"
    assert rows[second]["status"] == "pending"


def test_admin_sees_who_decided_each_request(app, config, conn, monkeypatch):
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: None)
    monkeypatch.setattr("donatix.cashiers.send_file", lambda *a, **k: 5)
    cashiers.add(conn, 5550001, "Исом")
    uid, client, token = _client(app, config, conn)
    _send(client, token)
    config.alert_telegram_chat_id = "777"
    calls = []
    bot = tgbot.AdminBot(config, api=lambda m, **p: calls.append((m, p)) or {})
    bot.handle(conn, {"update_id": 1, "callback_query": {                 # подделанная кнопка — не под чеком
        "id": "c", "data": "pay:ok:1", "from": {"id": 5550001},
        "message": {"message_id": 99, "chat": {"id": 5550001, "type": "private"}, "text": "любое"}}})
    assert balance(conn, uid) == 0
    bot.handle(conn, {"update_id": 1, "callback_query": {
        "id": "c", "data": "pay:ok:1", "from": {"id": 5550001},
        "message": {"message_id": 5, "chat": {"id": 5550001, "type": "private"}, "caption": "чек", "photo": [{}]}}})
    assert balance(conn, uid) == 500_000

    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get(f"/admin/users/{uid}").text
    assert "Заявки на пополнение" in page and "кассир Исом" in page and "Зачислено" in page
    text, _ = tgbot.screen_client(conn, config, uid)                      # и в админ-боте в карточке клиента
    assert "✅ #1" in text and "кассир Исом" in text


def test_boost_after_10_minutes_resends_and_drops_old_message(app, config, conn, monkeypatch):
    sent, dropped = [], []
    monkeypatch.setattr("donatix.worker.notify_admin_file",
                        lambda cfg, caption, buttons, path, photo: sent.append(caption) or 100 + len(sent))
    real_drop = cashiers.drop_messages

    def drop(c, cfg, pid):
        dropped.append([r["message_id"] for r in c.execute(
            "SELECT message_id FROM payment_msgs WHERE payment_id = ?", (pid,))])
        return real_drop(c, cfg, pid)
    monkeypatch.setattr("donatix.cashiers.drop_messages", drop)
    config.alert_telegram_chat_id = "777"
    uid, client, token = _client(app, config, conn)
    _send(client, token)
    page = client.get("/panel/balance").text
    assert "появится кнопка «⚡ Ускорить»" in page and "boost-btn" not in page     # рано — только подсказка
    r = client.post("/panel/balance/1/boost", data={"csrf": token})
    assert "Ускорить можно через" in r.text and len(sent) == 1

    conn.execute("UPDATE payments SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = 1")   # ждёт давно
    assert "boost-btn" in client.get("/panel/balance").text
    r = client.post("/panel/balance/1/boost", data={"csrf": token})
    assert "Напомнили администратору" in r.text
    assert len(sent) == 2 and "Клиент просит ускорить" in sent[-1]                 # пришла заново, с пометкой
    assert dropped == [[101]]                                                     # старое сообщение удалено
    assert [r[0] for r in conn.execute("SELECT message_id FROM payment_msgs WHERE payment_id = 1")] == [102]
    r = client.post("/panel/balance/1/boost", data={"csrf": token})
    assert "Ускорить можно через" in r.text and len(sent) == 2                   # снова — только через 10 минут


class _Resp:
    def __init__(self, ok, result=None):
        self._b = {"ok": ok, "result": result or {}, "description": "" if ok else "bad"}
        self.text = str(self._b)

    def json(self):
        return self._b


def test_boost_deletes_every_copy_first_then_sends(app, config, conn, monkeypatch):
    """Ускорить: все старые копии (чек у админа, у кассира, из списка /payments) удаляются ДО новой."""
    config.alert_telegram_token, config.alert_telegram_chat_id = "T", "777"
    log = []

    def post(url, **kw):
        method = url.rsplit("/", 1)[1]
        body = kw.get("json") or kw.get("data") or {}
        log.append((method, str(body.get("message_id", ""))))
        if method == "deleteMessage":
            return _Resp(body["message_id"] != 12)               # 12 — старше 48 ч, удалить нельзя
        if method in ("sendPhoto", "sendDocument"):
            return _Resp(True, {"message_id": 50 + len(log)})
        return _Resp(True)
    monkeypatch.setattr("donatix.cashiers.httpx.post", post)
    uid, client, token = _client(app, config, conn)
    _send(client, token)
    cashiers.remember(conn, 1, "777", 11, "копия из /payments")
    cashiers.remember(conn, 1, "5550001", 12, "копия у кассира")
    conn.execute("UPDATE payments SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = 1")
    log.clear()
    payments.boost(conn, config, uid, 1)
    deletes = [m for m, _ in log if m == "deleteMessage"]
    first_send = next(i for i, (m, _) in enumerate(log) if m in ("sendPhoto", "sendDocument"))
    assert len(deletes) == 3 and all(m != "sendPhoto" for m, _ in log[:3])   # сначала удаление
    assert any(m == "editMessageCaption" and mid == "12" for m, mid in log[:first_send])  # не удалилось — сняли кнопки
    left = [r[0] for r in conn.execute("SELECT message_id FROM payment_msgs WHERE payment_id = 1")]
    assert 11 not in left and 12 not in left and len(left) == 1


def test_long_caption_never_breaks_html():
    long = "<b>Заявка</b>\n" + "\n".join(f"строка <code>{i}</code>" for i in range(200))
    cut = cashiers.safe_caption(long)
    assert len(cut) <= 1024 and cut.count("<code>") == cut.count("</code>")
