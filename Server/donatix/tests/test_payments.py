from fastapi.testclient import TestClient
from conftest import RECEIPT, balance, csrf_of, web_login

from donatix import accounts, orders


def _setup(app, config, conn):
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00 (Али)", "usdt_trc20": "TXyz…"}
    uid = accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    client = TestClient(app)
    token = web_login(client, "c@example.com", "password123")
    return uid, client, token


def test_topup_request_confirm_notify(app, config, conn):
    uid, client, token = _setup(app, config, conn)
    page = client.get("/panel/balance").text
    assert "Алиф (Alif Mobi)" in page and "+992 90 000 00 00" in page
    r = client.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": "50", "reference": "4821"},
                    files=RECEIPT)
    assert "Заявка #1 создана" in r.text and "545.00 TJS" in r.text  # 50 × 10.9

    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    assert "Заявок на пополнение: 1" in admin.get("/admin").text
    assert "client1" in admin.get("/admin/payments").text
    admin.post("/admin/payments/1/confirm", data={"csrf": atoken, "credit": "50"})
    assert balance(conn, uid) == 500_000
    admin.post("/admin/payments/1/confirm", data={"csrf": atoken, "credit": "50"})  # повтор — без второго зачисления
    assert balance(conn, uid) == 500_000

    panel = client.get("/panel").text
    assert 'class="dot-count">1<' in panel
    notes = client.get("/panel/notifications").text
    assert "Баланс пополнен на $50.0000" in notes
    assert 'class="dot-count"' not in client.get("/panel").text


def test_topup_validation_and_reject(app, config, conn):
    uid, client, token = _setup(app, config, conn)
    r = client.post("/panel/balance", data={"csrf": token, "method": "binance", "amount": "50"}, files=RECEIPT)
    assert "Выберите способ оплаты" in r.text
    r = client.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": "0"}, files=RECEIPT)
    assert "больше нуля" in r.text
    client.post("/panel/balance", data={"csrf": token, "method": "usdt_trc20", "amount": "50"}, files=RECEIPT)
    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    admin.post("/admin/payments/1/reject", data={"csrf": atoken, "reason": "Перевод не найден"})
    assert balance(conn, uid) == 0
    assert "Перевод не найден" in client.get("/panel/notifications").text


def test_refund_and_admin_credit_notify(client, conn, shop, supplier, app):
    supplier.fail_next = "reject_other"
    client.post("/api/v1/orders", headers=shop["h"],
                json={"product_id": "tg-stars", "quantity": 50, "fields": {"telegram_username": "@player_one"}})
    rows = conn.execute("SELECT text FROM notifications WHERE user_id = ?", (shop["id"],)).fetchall()
    assert any("вернулись на баланс" in r["text"] for r in rows)


def test_errors_page(client, conn, shop, supplier):
    supplier.fail_next = "reject_other"
    client.post("/api/v1/orders", headers=shop["h"],
                json={"product_id": "tg-stars", "quantity": 50, "fields": {"telegram_username": "@player_one"}})
    token = web_login(client, "admin@example.com", "adminpass123")
    page = client.get("/admin/errors").text
    assert "dx-1" in page and "Игрок не найден" in page
    assert token and orders


def test_admin_sets_pay_details(app, config, conn):
    config.pay_methods = {}
    uid, client, token = _setup(app, config, conn)
    config.pay_methods = {}
    assert "ещё не настроены" in client.get("/panel/balance").text

    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/pay-settings").text
    n = page.count('_code" value=')  - 1  # последняя строка — «новый способ»
    data = {"csrf": atoken, "n": str(n), "tjs_rate": "11", "min_tjs": "500", "low_usd": "10"}
    for i in range(n):  # стандартные способы оставляем выключенными, кроме DC
        code = page.split(f'name="m{i}_code" value="')[1].split('"')[0]
        data |= {f"m{i}_code": code, f"m{i}_title": code, f"m{i}_currency": "TJS"}
        if code == "dc":
            data |= {f"m{i}_details": "DC: 5058 **** 1234 (Али)", f"m{i}_enabled": "1"}
    # свой новый способ
    data |= {f"m{n}_title": "Humo", f"m{n}_currency": "TJS", f"m{n}_details": "Humo 9860 **** 55", f"m{n}_enabled": "1"}
    r = admin.post("/admin/pay-settings", data=data)
    assert "Реквизиты сохранены" in r.text
    page = client.get("/panel/balance").text
    assert "5058 **** 1234" in page and "Humo 9860" in page and "Алиф (Alif Mobi)" not in page
    assert "500 сомони" in page
    # минимум 500 сомони: $40 × 11 = 440 — мало, $50 × 11 = 550 — можно
    r = client.post("/panel/balance", data={"csrf": token, "method": "dc", "amount": "40"}, files=RECEIPT)
    assert "Минимальная сумма пополнения — 500 сомони" in r.text
    r = client.post("/panel/balance", data={"csrf": token, "method": "dc", "amount": "50"}, files=RECEIPT)
    assert "550.00 TJS" in r.text


def test_low_balance_warning(app, config, conn):
    from donatix import db
    uid, client, token = _setup(app, config, conn)
    with db.tx(conn):
        accounts.post_ledger(conn, uid, 120_000, "пополнение")
    assert "осталось" not in client.get("/panel").text
    with db.tx(conn):
        accounts.post_ledger(conn, uid, -50_000, "заказ")  # $12 → $7, ниже порога $10
    page = client.get("/panel").text
    assert "На балансе осталось <b>$7.0000</b>" in page
    notes = conn.execute("SELECT text FROM notifications WHERE user_id = ?", (uid,)).fetchall()
    assert sum("пополните счёт" in n[0] for n in notes) == 1
    with db.tx(conn):
        accounts.post_ledger(conn, uid, -10_000, "заказ")  # уже ниже — второй раз не пишем
    notes = conn.execute("SELECT text FROM notifications WHERE user_id = ?", (uid,)).fetchall()
    assert sum("пополните счёт" in n[0] for n in notes) == 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_api_topup_with_receipt(app, config, conn, monkeypatch):
    from donatix import tgbot
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00 (Али)"}
    uid = accounts.create_user(conn, email="bot@example.com", login="botowner", password="password123",
                               status="active")
    key = accounts.create_api_key(conn, uid, "bot")
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin_file",
                        lambda cfg, caption, buttons, path, photo: sent.append((caption, buttons, path, photo)))
    api = TestClient(app)
    h = {"X-API-Key": key}
    m = api.get("/api/v1/payments/methods", headers=h).json()
    assert m["ok"] and m["methods"][0]["code"] == "alif" and m["min_tjs"] == "0"
    small = api.post("/api/v1/payments", headers=h, json={"method": "alif", "amount_tjs": "5"}).json()
    assert small["ok"] and small["payment"]["pay_amount"] == "5.00"   # без минимума — любая сумма
    api.post(f"/api/v1/payments/{small['payment']['id']}/cancel", headers=h)
    conn.execute("UPDATE payments SET status = 'cancelled' WHERE id = ?", (small["payment"]["id"],))
    odd = api.post("/api/v1/payments", headers=h, json={"method": "alif", "amount_tjs": "600"}).json()["payment"]
    assert odd["pay_amount"] == "600.00" and odd["amount_usd"] == "55.0458"  # 600 / 10.9, вниз
    api.post(f"/api/v1/payments/{odd['id']}/receipt", headers=h, files={"file": ("x.png", PNG, "image/png")})
    again = api.post(f"/api/v1/payments/{odd['id']}/receipt", headers=h, files={"file": ("y.png", PNG + b"1",
                                                                                          "image/png")}).json()
    assert again["ok"] is False and "уже отправлен" in again["error"]      # второй чек к той же заявке — нет
    blocked = api.post("/api/v1/payments", headers=h, json={"method": "alif", "amount_tjs": "545"}).json()
    assert blocked["ok"] is False and f"#{odd['id']} на проверке" in blocked["error"]   # одна заявка за раз
    assert len(sent) == 1                                                    # админу чек пришёл один раз
    conn.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (odd["id"],))   # админ решил — можно снова
    r = api.post("/api/v1/payments", headers=h, json={"method": "alif", "amount_tjs": "545"}).json()
    pay = r["payment"]
    assert pay["pay_amount"] == "545.00" and pay["pay_currency"] == "TJS" and "+992" in pay["details"]
    assert api.post(f"/api/v1/payments/{pay['id']}/receipt", headers=h, files={"file": ("a.txt", b"hi", "text/plain")}
                    ).json()["ok"] is False
    r = api.post(f"/api/v1/payments/{pay['id']}/receipt", headers=h, files={"file": ("chek.png", PNG, "image/png")})
    assert r.json()["ok"] is False      # чек отклонённой заявки — повтор, второй раз не проходит
    r = api.post(f"/api/v1/payments/{pay['id']}/receipt", headers=h,
                 files={"file": ("chek.png", PNG + b"2", "image/png")})
    assert r.json()["payment"]["receipt"] is True
    caption, buttons, path, photo = sent[-1]
    assert "Чек приложен" in caption and photo and path.read_bytes() == PNG + b"2"
    # админ жмёт «Зачислить» под фото — баланс пополнен, подпись обновлена
    config.alert_telegram_chat_id = "777"
    calls = []
    bot = tgbot.AdminBot(config, api=lambda m, **p: calls.append((m, p)) or [])
    bot.handle(conn, {"update_id": 1, "callback_query": {"id": "c", "data": buttons[0][0][1], "message": {
        "message_id": 3, "chat": {"id": 777}, "caption": caption, "photo": [{}]}}})
    assert balance(conn, uid) == 500_000
    assert any(m == "editMessageCaption" for m, _ in calls)
    assert api.get(f"/api/v1/payments/{pay['id']}", headers=h).json()["payment"]["status"] == "paid"
    # админ может открыть чек в админке
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    assert admin.get(f"/admin/payments/{pay['id']}/receipt").content == PNG + b"2"


def test_panel_topup_with_receipt(app, config, conn, monkeypatch):
    uid, client, token = _setup(app, config, conn)
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: sent.append(a))
    r = client.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": "50"},
                    files={"receipt": ("c.png", PNG, "image/png")})
    assert "Заявка #1 создана" in r.text and len(sent) == 1


def test_usdt_network_shown_and_saved(app, config, conn):
    from donatix import payments
    uid, client, token = _setup(app, config, conn)
    page = client.get("/panel/balance").text
    assert "сеть TRC20" in page and "только в сети Tron (TRC20)" in page

    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    page = admin.get("/admin/pay-settings").text
    assert "USDT · BNB Smart Chain (BEP20)" in page
    n = page.count('_code" value=') - 1
    data = {"csrf": atoken, "n": str(n), "tjs_rate": "11", "min_tjs": "500", "low_usd": "10",
            f"m{n}_title": "USDT BEP20", f"m{n}_currency": "USDT:BEP20",
            f"m{n}_details": "0x1234567890abcdef1234567890abcdef12345678", f"m{n}_enabled": "1"}
    admin.post("/admin/pay-settings", data=data)
    ms = payments.methods(conn, config)
    assert [m["network"] for m in ms] == ["BEP20"]
    assert "BNB Smart Chain (BEP20)" in ms[0]["network_note"]
    r = client.post("/panel/balance", data={"csrf": token, "method": ms[0]["code"], "amount": "50"}, files=RECEIPT)
    assert "50.00 USDT" in r.text
    p = conn.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 1").fetchone()
    assert payments.public(conn, config, p)["network"] == "BEP20"


def test_old_500_minimum_becomes_100(app, config, conn):
    from donatix import db, payments, sitecfg
    db.set_setting(conn, "pay.min_tjs", "500")
    conn.execute("DELETE FROM settings WHERE key = 'migr.min_tjs_100'")  # как на старом сервере
    sitecfg.load(conn, config)
    assert payments.settings(conn, config)["min_tjs"] == 100
    db.set_setting(conn, "pay.min_tjs", "300")  # админ задал своё — больше не трогаем
    sitecfg.load(conn, config)
    assert payments.settings(conn, config)["min_tjs"] == 300


def test_admin_uploads_method_icon(app, config, conn):
    from donatix import payments
    uid, client, token = _setup(app, config, conn)
    admin = TestClient(app)
    atoken = web_login(admin, "admin@example.com", "adminpass123")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    data = {"csrf": atoken, "n": "0", "tjs_rate": "11", "min_tjs": "100", "low_usd": "10",
            "m0_title": "Душанбе Сити", "m0_currency": "TJS", "m0_details": "DC 5058 **** 1", "m0_enabled": "1"}
    r = admin.post("/admin/pay-settings", data=data, files={"m0_icon": ("dc.png", png, "image/png")})
    assert "Реквизиты сохранены" in r.text
    m = payments.methods(conn, config)[0]
    assert m["icon_url"].endswith(".png")
    icon = m["icon_url"].rsplit("/", 1)[1]
    assert f'/pay-icons/{icon}' in client.get("/panel/balance").text
    got = TestClient(app).get(f"/pay-icons/{icon}")
    assert got.status_code == 200 and got.content == png
    assert TestClient(app).get("/pay-icons/../t.db").status_code == 404

    # иконка сохраняется при следующем сохранении без файла
    page = admin.get("/admin/pay-settings").text
    code = page.split('name="m0_code" value="')[1].split('"')[0]
    admin.post("/admin/pay-settings", data=data | {"n": "1", "m0_code": code, "m1_title": ""})
    assert payments.methods(conn, config)[0]["icon_url"].endswith(icon)

    # не картинка — отказ
    r = admin.post("/admin/pay-settings", data=data | {"n": "1", "m0_code": code},
                   files={"m0_icon": ("x.png", b"hello", "image/png")})
    assert "PNG, JPG или WebP" in r.text



def test_receipt_is_required(client, conn, config):
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00"}
    from conftest import make_client
    make_client(conn, balance="0")
    web_login(client, "shop1@example.com", "password123")
    token = csrf_of(client.get("/panel/balance").text)
    page = client.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": "50"}).text
    assert "Прикрепите чек" in page
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    assert 'name="receipt" type="file" required' in client.get("/panel/balance").text
