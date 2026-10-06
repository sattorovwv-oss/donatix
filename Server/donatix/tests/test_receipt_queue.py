"""Проверка чеков по одному («Принять / Отклонить / Следующая») и поиск заявки по номеру."""
from conftest import balance, make_client
from test_cashiers import ADMIN_CHAT, ALI, VALI, _bot, _payment, _press, _say

from donatix import cashiers, db, payments


def _cards(calls):
    """Карточки очереди, что бот прислал (текстом — файла чека в тестах нет)."""
    return [p for m, p in calls if m == "sendMessage" and "Проверка чеков" in str(p.get("text", ""))]


def _api(calls, mid=[300]):
    def api(method, **p):
        calls.append((method, p))
        mid[0] += 1
        return {"message_id": mid[0]}
    return api


def test_admin_checks_receipts_one_by_one(config, conn):
    uid, _ = make_client(conn, balance="0")
    a, b, c = (_payment(conn, uid, usd=n) for n in (10, 20, 30))
    calls = []
    bot = _bot(config, calls)
    bot.api = _api(calls)
    _say(bot, conn, int(ADMIN_CHAT), "🧾 Проверка чеков")
    card = _cards(calls)[-1]
    assert f"#{a}" in card["text"] and "1 из 3" in card["text"] and "К переводу" in card["text"]
    assert [x["callback_data"] for x in card["reply_markup"]["inline_keyboard"][0]] == [f"q:ok:{a}", f"q:no:{a}"]
    mid = int(db.get_setting(conn, f"queue.msg.{ADMIN_CHAT}"))

    _press(bot, conn, int(ADMIN_CHAT), f"q:ok:{a}", message_id=mid)        # принять → сразу следующий
    assert balance(conn, uid) == 100_000
    assert ("deleteMessage", {"chat_id": ADMIN_CHAT, "message_id": mid}) in calls
    card = _cards(calls)[-1]
    assert f"#{b}" in card["text"] and "1 из 2" in card["text"]

    mid = int(db.get_setting(conn, f"queue.msg.{ADMIN_CHAT}"))
    _press(bot, conn, int(ADMIN_CHAT), f"q:next:{b}", message_id=mid)      # пропустить
    assert f"#{c}" in _cards(calls)[-1]["text"]
    mid = int(db.get_setting(conn, f"queue.msg.{ADMIN_CHAT}"))
    _press(bot, conn, int(ADMIN_CHAT), f"q:no:{c}", message_id=mid)        # отклонить → по кругу к пропущенному
    assert conn.execute("SELECT status FROM payments WHERE id = ?", (c,)).fetchone()[0] == "rejected"
    assert f"#{b}" in _cards(calls)[-1]["text"]

    _press(bot, conn, int(ADMIN_CHAT), f"q:ok:{b}", message_id=mid)        # старая карточка — не сработает
    assert "устарела" in calls[-1][1]["text"] and balance(conn, uid) == 100_000
    mid = int(db.get_setting(conn, f"queue.msg.{ADMIN_CHAT}"))
    _press(bot, conn, int(ADMIN_CHAT), f"q:ok:{b}", message_id=mid)
    assert "Все чеки проверены" in calls[-1][1]["text"] and balance(conn, uid) == 300_000


def test_cashier_queue_only_own_banks(config, conn):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    conn.execute("UPDATE cashiers SET methods = '[\"dc\"]' WHERE tg_id = ?", (ALI,))
    _payment(conn, uid, "alif")
    dc = _payment(conn, uid, "dc")
    calls = []
    bot = _bot(config, calls)
    bot.api = _api(calls)
    _press(bot, conn, ALI, "q:start:0")
    card = _cards(calls)[-1]
    assert f"#{dc}" in card["text"] and "1 из 1" in card["text"] and card["chat_id"] == str(ALI)
    mid = int(db.get_setting(conn, f"queue.msg.{ALI}"))
    _press(bot, conn, VALI, f"q:ok:{dc}", message_id=mid)                 # не кассир
    assert calls[-1][1]["text"] == "Нет доступа"
    _press(bot, conn, ALI, f"q:ok:{dc}", message_id=mid)
    assert balance(conn, uid) == 500_000
    assert conn.execute("SELECT resolved_who FROM payments WHERE id = ?", (dc,)).fetchone()[0] == "кассир Али"
    assert "Все чеки проверены" in calls[-1][1]["text"]                    # Алиф — не его банк


def test_search_shows_receipt_and_outcome(config, conn):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    a = _payment(conn, uid, "alif")
    b = _payment(conn, uid, "alif")
    payments.reject(conn, config, b, 1, "Перевод не найден", who="кассир Вали")
    conn.execute("UPDATE payments SET receipt_txn = 'AB1234567' WHERE id = ?", (a,))
    calls = []
    bot = _bot(config, calls)
    bot.api = _api(calls)
    _say(bot, conn, ALI, str(b))                                          # кассир пишет номер
    text = calls[-1][1]["text"]
    assert f"#{b}" in text and "❌ Отклонена" in text and "кассир Вали" in text and "Перевод не найден" in text
    assert calls[-1][1].get("reply_markup") is None                       # решённая — без кнопок

    _say(bot, conn, int(ADMIN_CHAT), "AB-1234567")                        # по номеру операции из чека
    msg = calls[-1][1]
    assert f"#{a}" in msg["text"] and "⏳ Ждёт проверки" in msg["text"]
    assert msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"pay:ok:{a}"
    _say(bot, conn, int(ADMIN_CHAT), "/find 99999")
    assert "не нашёл" in calls[-1][1]["text"]


def test_cashier_finds_any_request_by_button_but_decides_only_own_bank(config, conn):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    conn.execute("UPDATE cashiers SET methods = '[\"dc\"]' WHERE tg_id = ?", (ALI,))
    alif = _payment(conn, uid, "alif")
    dc = _payment(conn, uid, "dc")
    calls = []
    bot = _bot(config, calls)
    bot.api = _api(calls)
    _press(bot, conn, ALI, "q:find:0")                                    # кнопка, а не команда
    assert "Поиск заявки" in calls[-1][1]["text"]
    _say(bot, conn, ALI, f"#{alif}")                                      # чужой банк — видно, но без кнопок
    msg = calls[-1][1]
    assert f"#{alif}" in msg["text"] and "⏳ Ждёт проверки" in msg["text"] and msg.get("reply_markup") is None
    assert "решает кассир другого банка" in msg["text"]
    _say(bot, conn, ALI, f"#{dc}")                                        # свой банк — с кнопками
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"pay:ok:{dc}"
    _press(bot, conn, ALI, "cs:home:0")
    home = [p for m, p in calls if m in ("sendMessage", "editMessageText")][-1]
    assert any(b["callback_data"] == "q:find:0" for row in home["reply_markup"]["inline_keyboard"] for b in row)
