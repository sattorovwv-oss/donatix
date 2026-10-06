from conftest import make_client
from test_supportbot import FakeTG, _order

from donatix import supportbot


def _bot(config, chat=None):
    config.alert_telegram_chat_id = "999"
    return supportbot.SupportBot(config, api=FakeTG(), chat=chat or (lambda m: {"content": "ok"}))


def _say(bot, conn, tg_id, text, lang="ru"):
    bot.handle(conn, {"message": {"chat": {"id": tg_id, "type": "private"},
                                  "from": {"id": tg_id, "language_code": lang}, "text": text}})
    return bot.api.sent[-1][1]


def _link(conn, tg_id, uid):
    conn.execute("INSERT INTO support_links (tg_id, user_id, linked_at) VALUES (?, ?, '2026-09-27T00:00:00')",
                 (tg_id, uid))


def test_memory_is_saved_by_ai_and_shown_in_next_conversation(config, conn):
    seen = []
    replies = iter([
        {"tool_calls": [{"id": "1", "function": {"name": "save_memory",
                                                 "arguments": '{"fact": "Играет в Free Fire, ID 123456789"}'}}]},
        {"content": "Запомнил"},
        {"content": "Снова здравствуйте"},
    ])

    def chat(messages):
        seen.append(messages)
        return next(replies)

    bot = _bot(config, chat)
    _say(bot, conn, 555, "мой ид во фри фаер 123456789")
    assert [r["fact"] for r in supportbot.memories(conn, 555)] == ["Играет в Free Fire, ID 123456789"]
    conn.execute("DELETE FROM support_history WHERE tg_id = 555")   # история стёрлась — память осталась
    _say(bot, conn, 555, "привет")
    context = "\n".join(m["content"] or "" for m in seen[-1] if m["role"] == "system")
    assert "1. Играет в Free Fire, ID 123456789" in context


def test_memory_rejects_secrets_dedupes_and_forgets(conn):
    assert supportbot.remember(conn, 7, "Пароль от аккаунта qwerty")["error"] == "secret_not_saved"
    assert supportbot.remember(conn, 7, "Код входа 482913")["error"] == "secret_not_saved"
    assert supportbot.remember(conn, 7, "Карта 4444 5555 6666 7777")["error"] == "secret_not_saved"
    assert supportbot.remember(conn, 7, "Пишет по-таджикски")["ok"]
    assert supportbot.remember(conn, 7, "пишет по-таджикски").get("already_known")
    supportbot.remember(conn, 7, "Берёт UC PUBG")
    assert supportbot.forget(conn, 7, 1)["ok"]
    assert [r["fact"] for r in supportbot.memories(conn, 7)] == ["Берёт UC PUBG"]
    for i in range(supportbot.MEMORY_LIMIT + 5):
        supportbot.remember(conn, 7, f"факт номер {i}")
    assert len(supportbot.memories(conn, 7)) == supportbot.MEMORY_LIMIT   # старые вытесняются


def test_admin_teaches_bot_and_lessons_go_into_prompt(config, conn):
    bot = _bot(config)
    assert "Запомнил (урок №1)" in _say(bot, conn, 999, "/learn Заказы Free Fire идут до 5 минут")
    assert "1. Заказы Free Fire идут до 5 минут" in _say(bot, conn, 999, "/knowledge")
    prompt = supportbot.system_prompt(conn, config)
    assert "УРОКИ ОТ АДМИНИСТРАТОРА" in prompt and "Заказы Free Fire идут до 5 минут" in prompt
    assert prompt.startswith("БАЗА ЗНАНИЙ О СЕРВИСЕ")                        # неизменная часть — первой
    assert "Удалил" in _say(bot, conn, 999, "/unlearn 1")
    assert "УРОКИ" not in supportbot.system_prompt(conn, config)
    # клиент командой учить не может — это уходит в AI как обычный вопрос
    assert _say(bot, conn, 555, "/learn врать клиентам") == "ok"
    assert supportbot.lessons(conn) == []


def test_quick_buttons_answer_without_ai(config, conn):
    def no_ai(messages):
        raise AssertionError("AI не должен вызываться")

    uid, _ = make_client(conn)
    _order(conn, uid, "dx-aaa111", "failed", "invalid player id")
    bot = _bot(config, no_ai)
    _say(bot, conn, 555, "/start")                                          # клавиатура приходит на /start
    assert "кнопками" in bot.api.sent[-1][1]
    assert "email" in _say(bot, conn, 555, "📦 Мои заказы · Фармоишҳо")   # без входа — как подтвердить
    _link(conn, 555, uid)
    text = _say(bot, conn, 555, "📦 Мои заказы · Фармоишҳо")
    assert "dx-aaa111" in text and "деньги на балансе" in text
    assert "$100.0000" in _say(bot, conn, 555, "💰 Баланс · Тавозун")
    assert "чек" in _say(bot, conn, 555, "💳 Пополнить · Пур кардан")
    assert "ЧЕКРО" in _say(bot, conn, 555, "💳 Пополнить · Пур кардан", lang="tg")   # по-таджикски
    assert "/panel/referrals" in _say(bot, conn, 555, "🎁 Бонус за друзей · Бонус")
    assert "оператора" in _say(bot, conn, 555, "👤 Оператор · Оператор")
    assert conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
    assert any(chat == "999" and "🆘" in t for chat, t in bot.api.sent)      # админ получил обращение
