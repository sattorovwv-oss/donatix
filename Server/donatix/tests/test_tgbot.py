"""Бот админки: кнопки выполняют действия, чужие чаты игнорируются."""

from conftest import add_receipt, balance

from donatix import accounts, payments
from donatix.tgbot import AdminBot, payment_event, summary, user_event


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, **payload):
        self.calls.append((method, payload))
        return []


def _bot(config):
    config.alert_telegram_chat_id = "777"
    config.alert_telegram_token = "t"
    api = FakeApi()
    return AdminBot(config, api=api), api


def _press(bot, conn, data, chat="777"):
    bot.handle(conn, {"update_id": 1, "callback_query": {
        "id": "cb", "data": data, "message": {"message_id": 5, "chat": {"id": int(chat)}, "text": "…"}}})


def test_buttons_confirm_payment_and_approve_user(config, conn):
    config.pay_methods = {"alif": "Алиф: +992 …"}
    bot, api = _bot(config)
    uid = accounts.create_user(conn, email="n@example.com", login="newbie", password="password123", status="pending")
    text, buttons = user_event(conn, uid)
    assert "newbie" in text and buttons[0][0][1] == f"user:ok:{uid}"
    _press(bot, conn, f"user:ok:{uid}")
    assert accounts.get_user(conn, uid)["status"] == "active"

    pid = payments.create(conn, config, accounts.get_user(conn, uid), "alif", "50")
    _press(bot, conn, f"pay:ok:{pid}")                     # без чека — не зачисляется
    assert conn.execute("SELECT status FROM payments WHERE id = ?", (pid,)).fetchone()[0] == "pending"
    add_receipt(conn, pid)
    text, buttons = payment_event(conn, pid)
    assert "545.00 TJS" in text
    _press(bot, conn, f"pay:ok:{pid}")
    _press(bot, conn, f"pay:ok:{pid}")  # повторное нажатие — без второго зачисления
    assert balance(conn, uid) == 500_000
    answers = [p["text"] for m, p in api.calls if m == "answerCallbackQuery"]
    assert answers[-2:] == ["Зачислено, клиент получил уведомление", "Заявка уже обработана"]


def test_foreign_chat_is_ignored(config, conn):
    bot, api = _bot(config)
    uid = accounts.create_user(conn, email="x@example.com", login="xuser", password="password123", status="pending")
    _press(bot, conn, f"user:ok:{uid}", chat="999")
    assert accounts.get_user(conn, uid)["status"] == "pending"
    bot.handle(conn, {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/stats"}})
    assert not [c for c in api.calls if c[0] == "sendMessage"]


def test_commands(config, conn):
    bot, api = _bot(config)
    bot.handle(conn, {"update_id": 3, "message": {"chat": {"id": 777}, "text": "/stats"}})
    assert "Сводка" in api.calls[-1][1]["text"]
    bot.handle(conn, {"update_id": 4, "message": {"chat": {"id": 777}, "text": "💳 Заявки"}})
    assert api.calls[-1][1]["text"] == "Заявок на пополнение нет."
    assert "Проблемных заказов" in summary(conn)


def _last_screen(api):
    for method, payload in reversed(api.calls):
        if method in ("editMessageText", "sendMessage") and payload.get("reply_markup"):
            return payload["text"], [b["callback_data"] for row in payload["reply_markup"].get("inline_keyboard", [])
                                     for b in row]
    return "", []


def test_admin_menu_controls_everything(config, conn):
    from donatix import bots, db, sitecfg
    bot, api = _bot(config)
    bot.on_command(conn, "/menu")
    text, cbs = _last_screen(api)
    assert "Админка" in text and {"m:clients:0", "m:bots:0", "m:rate:0", "m:set:0", "m:cat:0"} <= set(cbs)

    # клиенты: найти, заблокировать, сменить уровень
    uid = accounts.create_user(conn, email="c@example.com", login="shop1", password="password123", status="active")
    bot.on_command(conn, "/client shop1")
    assert "shop1" in _last_screen(api)[0]
    _press(bot, conn, f"cl:block:{uid}")
    assert accounts.get_user(conn, uid)["status"] == "blocked"
    _press(bot, conn, f"cl:gold:{uid}")
    assert accounts.get_user(conn, uid)["tier"] == "gold"

    # настройки: наценка и переключатели — действуют сразу
    before = config.markups["bronze"]
    _press(bot, conn, "mk:bronze:5")
    assert config.markups["bronze"] == before + 1 / __import__("decimal").Decimal(2)
    _press(bot, conn, "set:reg_open:0")
    assert not sitecfg.registration_open(conn)
    _press(bot, conn, "set:client_bots:0")
    assert not sitecfg.client_bots_enabled(conn)

    # боты клиентов: остановить
    bid = bots.create(conn, config, user_id=uid, token="1:AAAAAA", admin_ids="1", username="s_bot")
    _press(bot, conn, "m:bots:0")
    assert f"bot:view:{bid}" in _last_screen(api)[1]
    _press(bot, conn, f"bot:stop:{bid}")
    assert conn.execute("SELECT enabled FROM bots WHERE id = ?", (bid,)).fetchone()[0] == 0

    # курс: запас меняется кнопкой
    db.set_setting(conn, "pay.rate_auto", "1")
    db.set_setting(conn, "pay.rate_market", "10")
    db.set_setting(conn, "pay.rate_ts", "9999999999")
    _press(bot, conn, "rate:margin:5")
    assert payments.settings(conn, config)["tjs_rate"] == __import__("decimal").Decimal("10.15")
    assert "10.15" in _last_screen(api)[0]


def test_web_settings_page(app, config, conn):
    from conftest import web_login
    from fastapi.testclient import TestClient
    admin = TestClient(app)
    tok = web_login(admin, "admin@example.com", "adminpass123")
    assert "Наценка" in admin.get("/admin/settings").text
    r = admin.post("/admin/settings", data={"csrf": tok, "markup_bronze": "9", "markup_silver": "7",
                                            "markup_gold": "5", "markup_steam_topup": "", "markup_steam_gift": "",
                                            "support": "@help", "max_bots": "1", "client_bots": "1"})
    assert "Настройки сохранены" in r.text
    assert str(config.markups["bronze"]) == "9" and "steam_topup" not in config.kind_markups
    assert config.support_contact == "@help"
    r = TestClient(app).get("/register")
    assert "временно закрыт" in r.text  # reg_open не отмечен


def test_admin_bot_adds_pay_method_with_icon(config, conn):
    bot, api = _bot(config)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    bot.api.download = lambda file_id, max_bytes=0: png

    def say(text=None, photo=False):
        msg = {"message_id": 9, "chat": {"id": 777}}
        if photo:
            msg["photo"] = [{"file_id": "small"}, {"file_id": "big"}]
        else:
            msg["text"] = text
        bot.handle(conn, {"update_id": 2, "message": msg})

    _press(bot, conn, "pm:list:0")
    assert "pm:new:0" in _last_screen(api)[1]
    _press(bot, conn, "pm:new:0")
    say("Душанбе Сити")
    text, cbs = _last_screen(api)
    assert "шаг 2 из 4" in text and "pm:cur:0" in cbs
    _press(bot, conn, "pm:cur:0")                      # TJS
    say("5058 2700 1234 5678 · Алиджон М.")
    assert "иконки" in _last_screen(api)[0]
    say(photo=True)
    text, cbs = _last_screen(api)
    assert "Иконка загружена" in text and "pm:save:0" in cbs
    _press(bot, conn, "pm:save:0")
    m = payments.methods(conn, config)[0]
    assert m["title"] == "Душанбе Сити" and m["icon_url"].endswith(".png") and "5058" in m["details"]

    # USDT с сетью, без иконки; потом скрыть
    _press(bot, conn, "pm:new:0")
    say("USDT TRC20")
    idx = [c for c, _ in payments.CURRENCY_CHOICES].index("USDT:TRC20")
    _press(bot, conn, f"pm:cur:{idx}")
    say("TXyzABCDEFGHJKLMNPQRSTUVWXYZabcdef")
    _press(bot, conn, "pm:skip:0")
    _press(bot, conn, "pm:save:0")
    ms = payments.methods(conn, config)
    assert ms[1]["network"] == "TRC20" and ms[1]["icon_url"] == ""
    idx = [m["title"] for m in payments.settings(conn, config)["all_methods"]].index("USDT TRC20")
    _press(bot, conn, f"pm:tog:{idx}")
    assert [m["title"] for m in payments.methods(conn, config)] == ["Душанбе Сити"]


def test_stars_and_premium_markup(app, config, conn):
    from decimal import Decimal

    from conftest import web_login
    from fastapi.testclient import TestClient

    from donatix import accounts as acc
    admin = TestClient(app)
    tok = web_login(admin, "admin@example.com", "adminpass123")
    admin.post("/admin/settings", data={"csrf": tok, "markup_bronze": "8", "markup_silver": "6", "markup_gold": "4",
                                        "markup_telegram_stars": "12", "markup_telegram_premium": "",
                                        "markup_steam_topup": "", "markup_steam_gift": "", "reg_open": "1"})
    uid = acc.create_user(conn, email="s@example.com", login="shop9", password="password123", status="active")
    u = acc.get_user(conn, uid)
    assert acc.markup_for(u, config, "telegram_stars") == Decimal("12")
    assert acc.markup_for(u, config, "telegram_premium") == Decimal("8")   # пусто — как по уровню
    bot, api = _bot(config)
    _press(bot, conn, "mk:telegram_premium:5")
    assert acc.markup_for(u, config, "telegram_premium") == Decimal("8.5")


def test_admin_balance_refresh_shows_reason(app, config, conn, monkeypatch):
    from conftest import web_login
    from fastapi.testclient import TestClient

    from donatix.suppliers.base import SupplierUnavailable

    def broken():
        raise SupplierUnavailable("GET /balance: HTTP 401")
    monkeypatch.setattr(app.state.supplier, "balance", broken)
    admin = TestClient(app)
    tok = web_login(admin, "admin@example.com", "adminpass123")
    r = admin.post("/admin/supplier-balance", data={"csrf": tok})
    assert "HTTP 401" in r.text
