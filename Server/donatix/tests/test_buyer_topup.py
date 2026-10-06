"""Админ-бот: найти покупателя бота-магазина по @username или Telegram ID и пополнить ему баланс вручную."""
from conftest import balance
from test_cashiers import ADMIN_CHAT, _bot, _press, _say
from test_shopbot import TG, FakeApi, msg

from donatix import shopbot, tgbot


def _buyer(config, supplier, conn, username="ali_shop"):
    bot = shopbot.ShopBot(config, supplier, api=FakeApi())
    m = msg("/start")
    m["message"]["from"]["username"] = username
    bot.handle(conn, m)
    return shopbot.shop_user(conn, TG)


def _sent(calls):
    return [p for m, p in calls if m in ("sendMessage", "editMessageText")]


def test_username_saved_and_updated(config, supplier, conn):
    su = _buyer(config, supplier, conn)
    assert su["username"] == "ali_shop"
    su = _buyer(config, supplier, conn, username="")          # убрал username в Telegram
    assert su["username"] is None
    assert tgbot.find_buyer(conn, str(TG))["tg_id"] == TG
    _buyer(config, supplier, conn, username="Ali_Shop")
    for q in ("@ali_shop", "https://t.me/ali_shop", "ali_shop", f"tg{TG}"):
        assert tgbot.find_buyer(conn, q)["tg_id"] == TG, q
    assert tgbot.find_buyer(conn, "@nobody") is None


def test_admin_tops_up_buyer_by_username(config, supplier, conn, monkeypatch):
    told = []
    monkeypatch.setattr(shopbot, "tell_buyer", lambda c, cfg, tg, micro: told.append((tg, micro)) or True)
    su = _buyer(config, supplier, conn)
    calls = []
    bot = _bot(config, calls)
    _say(bot, conn, int(ADMIN_CHAT), "@ali_shop")
    card = _sent(calls)[-1]["text"]
    assert "Покупатель бота" in card and f'tg://user?id={TG}' in card and "https://t.me/ali_shop" in card
    assert f"<code>{TG}</code>" in card

    _press(bot, conn, int(ADMIN_CHAT), f"tb:a50:{TG}")        # +50 сомони → подтверждение
    assert "Пополнить 50 с." in _sent(calls)[-1]["text"] and balance(conn, su["user_id"]) == 0
    _press(bot, conn, int(ADMIN_CHAT), f"tb:ok:{TG}")
    rate = shopbot.tjs_rate(conn, config)
    assert shopbot.money(balance(conn, su["user_id"]), rate) == "50 с."
    assert told == [(TG, balance(conn, su["user_id"]))] and "получил сообщение" in _sent(calls)[-1]["text"]
    _press(bot, conn, int(ADMIN_CHAT), f"tb:ok:{TG}")        # повторное нажатие — второй раз не пополнит
    assert "устарела" in _sent(calls)[-1]["text"] and len(told) == 1

    _press(bot, conn, int(ADMIN_CHAT), f"tb:custom:{TG}")     # своя сумма в долларах
    _say(bot, conn, int(ADMIN_CHAT), "2$")
    assert "Пополнить" in _sent(calls)[-1]["text"] and "$2.0000" in _sent(calls)[-1]["text"]
    before = balance(conn, su["user_id"])
    _press(bot, conn, int(ADMIN_CHAT), f"tb:ok:{TG}")
    assert balance(conn, su["user_id"]) == before + 20_000

    _press(bot, conn, int(ADMIN_CHAT), f"tb:minus:{TG}")      # списать больше, чем есть — нельзя
    _say(bot, conn, int(ADMIN_CHAT), "100000")
    _press(bot, conn, int(ADMIN_CHAT), f"tb:ok:{TG}")
    assert "нельзя" in _sent(calls)[-1]["text"] and balance(conn, su["user_id"]) == before + 20_000


def test_topup_command_by_id_and_link_in_receipt(config, supplier, conn, monkeypatch):
    monkeypatch.setattr(shopbot, "tell_buyer", lambda *a: False)
    su = _buyer(config, supplier, conn, username="")
    calls = []
    bot = _bot(config, calls)
    _say(bot, conn, int(ADMIN_CHAT), f"/topup {TG} 20")
    assert "Пополнить 20 с." in _sent(calls)[-1]["text"] and "без имени" not in _sent(calls)[-1]["text"]
    _press(bot, conn, int(ADMIN_CHAT), f"tb:ok:{TG}")
    assert "не дошло" in _sent(calls)[-1]["text"] and balance(conn, su["user_id"]) > 0
    assert "Нет @username" in _sent(calls)[-1]["text"]

    c = conn.execute("INSERT INTO payments (user_id, method, amount_micro, pay_amount, pay_currency, created_at) "
                     "VALUES (?, 'alif', 10000, '10.9', 'TJS', '2026-10-05T08:00:00')", (su["user_id"],))
    text, _ = tgbot.payment_event(conn, c.lastrowid, config)
    assert f'Telegram: <a href="tg://user?id={TG}">' in text
