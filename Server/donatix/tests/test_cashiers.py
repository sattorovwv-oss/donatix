from datetime import datetime, timezone

from conftest import add_receipt, balance, make_client

from donatix import cashiers, finance, tgbot

ADMIN_CHAT = "777"
ALI = 5550001
VALI = 5550002


def _bot(config, calls):
    config.alert_telegram_chat_id = ADMIN_CHAT
    return tgbot.AdminBot(config, api=lambda m, **p: calls.append((m, p)) or {})


def _press(bot, conn, chat_id, data, message_id=10, from_id=None):
    bot.handle(conn, {"update_id": 1, "callback_query": {
        "id": "c", "data": data, "from": {"id": from_id or chat_id},
        "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}, "caption": "чек",
                    "photo": [{}]}}})


def _say(bot, conn, chat_id, text, **extra):
    bot.handle(conn, {"update_id": 2, "message": {"message_id": 1, "text": text, "from": {"id": chat_id},
                                                  "chat": {"id": chat_id, "type": "private"}, **extra}})


def _payment(conn, uid, method="alif", usd=50):
    c = conn.execute("INSERT INTO payments (user_id, method, amount_micro, pay_amount, pay_currency, created_at) "
                     "VALUES (?, ?, ?, '545', 'TJS', '2026-09-26T08:00:00')", (uid, method, usd * 10_000))
    add_receipt(conn, c.lastrowid)
    return c.lastrowid


def _texts(calls, method="sendMessage"):
    return "\n".join(str(p.get("text", "")) for m, p in calls if m == method)


def test_admin_adds_cashier_in_bot_and_picks_banks(config, conn):
    calls = []
    bot = _bot(config, calls)
    _say(bot, conn, ALI, "/start")                       # незнакомец узнаёт свой ID
    assert f"<code>{ALI}</code>" in _texts(calls)

    _press(bot, conn, int(ADMIN_CHAT), "ks:new:0")
    _say(bot, conn, int(ADMIN_CHAT), "abc")              # не цифры — просим ещё раз
    assert "числовой Telegram ID" in calls[-1][1]["text"]
    _say(bot, conn, int(ADMIN_CHAT), str(ALI))
    _say(bot, conn, int(ADMIN_CHAT), "Али")
    c = cashiers.get(conn, ALI)
    assert c["name"] == "Али" and c["methods"] == cashiers.ALL and c["percent"] == 20
    assert any(m == "sendMessage" and p["chat_id"] == str(ALI) for m, p in calls)   # бот поздоровался

    banks = [m["code"] for m in cashiers.bank_methods(conn, config)]
    assert "alif" in banks and "usdt_trc20" not in [m["code"] for m in cashiers.bank_methods(conn, config)
                                                     if m.get("auto")]
    _press(bot, conn, int(ADMIN_CHAT), f"ks:all:{ALI}")                  # снять «все банки»
    _press(bot, conn, int(ADMIN_CHAT), f"ks:b{banks.index('alif')}:{ALI}")   # оставить только Алиф
    _press(bot, conn, int(ADMIN_CHAT), f"ks:pup:{ALI}")
    c = cashiers.get(conn, ALI)
    assert cashiers.codes_of(c) == ["alif"] and c["percent"] == 25
    assert cashiers.handles(conn, config, c, "alif") and not cashiers.handles(conn, config, c, "dc")


def test_receipt_goes_to_admin_and_bank_cashier_and_is_decided_once(config, conn, monkeypatch):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    cashiers.add(conn, VALI, "Вали")
    conn.execute("UPDATE cashiers SET methods = '[\"dc\"]' WHERE tg_id = ?", (VALI,))   # у Вали только DC
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin_file",
                        lambda cfg, caption, buttons, path, photo: sent.append(("admin", caption)) or 100)
    monkeypatch.setattr("donatix.cashiers.send_file",
                        lambda cfg, chat, caption, buttons, path, photo: sent.append((chat, caption)) or 201)
    pid = _payment(conn, uid, "alif")
    config.alert_telegram_chat_id = ADMIN_CHAT
    tgbot.send_receipt(conn, config, pid)
    assert [who for who, _ in sent] == ["admin", ALI]                    # Вали чек по Алифу не получил

    calls = []
    bot = _bot(config, calls)
    _press(bot, conn, VALI, f"pay:ok:{pid}")                             # чужой банк — нельзя
    assert "не назначен" in calls[-1][1]["text"] and balance(conn, uid) == 0
    _press(bot, conn, 999, f"pay:ok:{pid}")                              # посторонний
    assert calls[-1][1]["text"] == "Нет доступа"

    _press(bot, conn, ALI, f"pay:ok:{pid}", message_id=201)
    assert balance(conn, uid) == 500_000
    edits = [p for m, p in calls if m == "editMessageCaption"]
    assert {e["chat_id"] for e in edits} == {ADMIN_CHAT, str(ALI)}       # у админа и у кассира — итог
    assert all("Зачислено $50.0000 — Али" in e["caption"] and "reply_markup" not in e for e in edits)
    row = conn.execute("SELECT status, resolved_tg FROM payments WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "paid" and row["resolved_tg"] == str(ALI)

    calls.clear()
    _press(bot, conn, int(ADMIN_CHAT), f"pay:ok:{pid}", message_id=100)  # админ опоздал — второй раз не зачислит
    assert calls[0][1]["text"] == "Заявка уже обработана" and balance(conn, uid) == 500_000


def test_admin_reject_updates_cashier_copy(config, conn, monkeypatch):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: 100)
    monkeypatch.setattr("donatix.cashiers.send_file", lambda *a, **k: 201)
    pid = _payment(conn, uid, "alif")
    config.alert_telegram_chat_id = ADMIN_CHAT
    tgbot.send_receipt(conn, config, pid)
    calls = []
    bot = _bot(config, calls)
    _press(bot, conn, int(ADMIN_CHAT), f"pay:no:{pid}", message_id=100)
    edits = [p for m, p in calls if m == "editMessageCaption"]
    assert {e["chat_id"] for e in edits} == {ADMIN_CHAT, str(ALI)} and "Отклонено — админ" in edits[0]["caption"]
    _press(bot, conn, ALI, f"pay:ok:{pid}", message_id=201)
    assert balance(conn, uid) == 0


def test_cashier_share_of_profit(config, conn):
    uid, _ = make_client(conn)
    cashiers.add(conn, ALI, "Али")
    conn.execute("UPDATE cashiers SET methods = '[\"alif\"]' WHERE tg_id = ?", (ALI,))
    for method, usd, auto in (("alif", 60, ""), ("dc", 20, ""), ("usdt_trc20", 20, "trc20")):
        conn.execute("INSERT INTO payments (user_id, method, amount_micro, pay_amount, pay_currency, status, "
                     "created_at, resolved_at, auto_kind) VALUES (?, ?, ?, '1', 'TJS', 'paid', ?, ?, ?)",
                     (uid, method, usd * 10_000, "2026-09-26T08:00:00", "2026-09-26T08:00:00", auto))
    conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                 "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at, completed_at) "
                 "VALUES ('dx-c1', ?, 'tg-stars', 'telegram_stars', 'Stars', 1, '1', ?, ?, 'completed', 'k1', ?, ?, ?)",
                 (uid, 150 * 10_000, 50 * 10_000, *["2026-09-26T09:00:00"] * 3))
    start, end = finance.window(conn, datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc), days_back=1)
    s = finance.summary(conn, config, start, end)
    # прибыль $100, через Алиф 60% всех пополнений → 20% от $60 = $12, владельцу $88
    assert s["profit"] == 1_000_000
    assert s["cashiers"][0]["share"] == 120_000 and s["owner_profit"] == 880_000
    text = finance.report_text(s)
    assert "Кассир Али (20%" in text and "Вам: $88.00" in text

    conn.execute("UPDATE cashiers SET methods = '*' WHERE tg_id = ?", (ALI,))   # все банки = всё, кроме крипты
    s = finance.summary(conn, config, start, end)
    assert s["cashiers"][0]["received"] == 800_000 and s["cashiers"][0]["share"] == 160_000


def test_cashier_home_screen_and_resend(config, conn, monkeypatch):
    uid, _ = make_client(conn, balance="0")
    cashiers.add(conn, ALI, "Али")
    pid = _payment(conn, uid, "alif")
    sent = []
    monkeypatch.setattr("donatix.cashiers.send_file",
                        lambda cfg, chat, caption, buttons, path, photo: sent.append(chat) or 300)
    calls = []
    bot = _bot(config, calls)
    _say(bot, conn, ALI, "привет")
    home = _texts(calls)
    assert "Кассир Али" in home and "все банки" in home and "ваш доход" in home
    _press(bot, conn, ALI, "cs:pays:0")
    assert sent == [ALI] and "Прислал заявок: 1" in calls[-1][1]["text"]
    assert cashiers.is_tracked(conn, ALI, 300) and pid
    cashiers.set_active(conn, ALI, False)                                # отключён — доступа нет
    _press(bot, conn, ALI, f"pay:ok:{pid}")
    assert calls[-1][1]["text"] == "Нет доступа" and balance(conn, uid) == 0
