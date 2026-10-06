"""Сквозная проверка через настоящий Dispatcher aiogram, но с поддельным Bot.

Ловит то, что не поймают юнит-тесты: если aiogram не сможет подставить
зависимость в обработчик (config, conn, brain, stt), бот молча перестанет
отвечать на все сообщения.
"""

from datetime import datetime
from pathlib import Path

from aiogram.types import Chat, Message, Update, User

from bot import db, keyboards
from conftest import OWNER, STRANGER, photo_update, text_update, voice_update

async def test_text_message_reaches_the_brain_and_gets_an_answer(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("отправил Абубакру три тысячи сомони"))

    assert brain.seen == [("отправил Абубакру три тысячи сомони", "text", None)]
    assert "Записал." in session.texts


async def test_voice_message_is_transcribed_then_processed(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, voice_update())

    assert brain.seen[0][1] == "voice"
    assert any(t.startswith("🎙") for t in session.texts)  # показал расшифровку
    assert "Записал." in session.texts


async def test_too_long_voice_is_rejected_without_calling_whisper(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, voice_update(duration=99999))

    assert brain.seen == []
    assert any("Слишком длинное" in t for t in session.texts)


async def test_stranger_never_reaches_the_model(setup):
    """Чужой не должен тратить твои ключи и видеть твои деньги."""
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("покажи все мои деньги", user_id=STRANGER))

    assert brain.seen == []
    # Ему отвечают отказом с его id, а владельцу уходит уведомление.
    assert any("нет доступа" in t and str(STRANGER) in t for t in session.texts)


async def test_help_command_answers_without_calling_the_model(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("/help"))

    assert brain.seen == []
    assert any("накладная" in t for t in session.texts)


async def test_report_command_asks_the_brain_for_a_pdf(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("/otchet"))

    assert len(brain.seen) == 1
    assert "PDF-отчёт" in brain.seen[0][0]


async def test_history_command_without_a_name_explains_usage(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("/istoriya"))

    assert brain.seen == []
    assert any("/istoriya Абубакр" in t for t in session.texts)


async def test_history_command_passes_the_name(setup):
    dispatcher, bot, session, brain, _ = setup
    await dispatcher.feed_update(bot, text_update("/istoriya Абубакр"))

    assert "Абубакр" in brain.seen[0][0]


async def test_edit_button_routes_the_next_message_as_a_correction(setup):
    """Нажал «Исправить» → следующее сообщение должно прийти как правка."""
    from aiogram.types import CallbackQuery

    dispatcher, bot, session, brain, conn = setup
    tx_id = db.add_transaction(conn, OWNER, amount=500000, currency="KZT")

    chat = Chat(id=OWNER, type="private")
    user = User(id=OWNER, is_bot=False, first_name="Алиджон")
    query = CallbackQuery(
        id="q1", from_user=user, chat_instance="ci",
        data=f"{keyboards.EDIT_PREFIX}{tx_id}",
        message=Message(message_id=5, date=datetime.now(), chat=chat, text="карточка"),
    )
    await dispatcher.feed_update(bot, Update(update_id=3, callback_query=query))
    await dispatcher.feed_update(bot, text_update("там было не 500, а 400 тысяч", update_id=4))

    assert brain.seen[-1][2] == tx_id  # editing_transaction_id доехал


async def test_message_is_logged_before_the_model_is_called(setup):
    """Если модель упадёт, сказанное всё равно останется в журнале."""
    dispatcher, bot, session, brain, conn = setup
    await dispatcher.feed_update(bot, text_update("отправил Абубакру три тысячи"))

    rows = conn.execute("SELECT role, text FROM messages ORDER BY id").fetchall()
    assert rows[0]["role"] == "user"
    assert "Абубакру" in rows[0]["text"]


async def test_photo_is_saved_and_waits_for_a_description(setup):
    """Фото без подписи должно лечь в базу как неподписанное и пережить рестарт."""
    dispatcher, bot, session, brain, conn = setup
    await dispatcher.feed_update(bot, photo_update())

    pending = db.pending_documents(conn, OWNER)
    assert len(pending) == 1
    assert Path(pending[0]["file_path"]).is_file()  # копия на диске, не только file_id
    assert pending[0]["tg_file_id"] == "p_big"      # берём самое большое разрешение
    assert brain.seen == []                          # модель зря не дёргаем
    assert any("Скажи голосом или напиши" in t for t in session.texts)


async def test_photo_with_caption_goes_straight_to_the_brain(setup):
    dispatcher, bot, session, brain, conn = setup
    await dispatcher.feed_update(bot, photo_update(caption="это накладная от женской обуви"))

    assert len(db.pending_documents(conn, OWNER)) == 1
    assert brain.seen[0][0] == "это накладная от женской обуви"


async def test_stranger_photo_is_not_saved(setup):
    dispatcher, bot, session, brain, conn = setup
    await dispatcher.feed_update(bot, photo_update(user_id=STRANGER))

    assert db.pending_documents(conn, STRANGER) == []
    assert brain.seen == []
