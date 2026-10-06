"""Регистрация и доступ — через настоящий Dispatcher aiogram.

Здесь ловится главное: чужой не должен добраться до Claude, а незарегистрированный
не должен потратить деньги голосовым до того, как представился.
"""

from datetime import datetime, timedelta, timezone


from bot import db
from conftest import OWNER, STRANGER, photo_update, text_update, voice_update

SALIM = 222


# ── незнакомец ─────────────────────────────────────────────────────────────

async def test_stranger_is_refused_and_told_their_id(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))

    assert brain.seen == []
    assert any(str(STRANGER) in t for t in session.texts)


async def test_stranger_voice_never_reaches_whisper_or_claude(env):
    """Голосовое от чужого стоило бы денег ещё до всякой проверки."""
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, voice_update(user_id=STRANGER))
    assert brain.seen == []


async def test_owner_is_notified_about_a_stranger_with_a_button(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))

    to_owner = [m for m in session.sent if getattr(m, "chat_id", None) == OWNER]
    assert to_owner, "владельцу не пришло уведомление"
    buttons = to_owner[-1].reply_markup.inline_keyboard[0]
    assert str(STRANGER) in buttons[0].callback_data


async def test_owner_is_not_spammed_about_the_same_stranger(env):
    """Чужой сканер не должен завалить владельца уведомлениями."""
    dispatcher, bot, session, brain, conn, _ = env
    for _ in range(5):
        await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))

    to_owner = [m for m in session.sent if getattr(m, "chat_id", None) == OWNER]
    assert len(to_owner) == 1


async def test_notification_repeats_after_a_day(env):
    dispatcher, bot, session, brain, conn, guard = env
    await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))
    guard._notified[STRANGER] = datetime.now(timezone.utc) - timedelta(days=2)
    await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))

    to_owner = [m for m in session.sent if getattr(m, "chat_id", None) == OWNER]
    assert len(to_owner) == 2


# ── регистрация ────────────────────────────────────────────────────────────

async def test_invited_person_registers_and_starts_working(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)

    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM))
    assert db.get_user(conn, SALIM)["status"] == "awaiting_name"
    assert any("Как тебя зовут" in t for t in session.texts)

    await dispatcher.feed_update(bot, text_update("Салим", user_id=SALIM, update_id=2))
    user = db.get_user(conn, SALIM)
    assert user["status"] == "active"
    assert user["name"] == "Салим"
    assert brain.seen == []   # имя не должно уйти в Claude как операция

    await dispatcher.feed_update(
        bot, text_update("отправил Абубакру 100 сомони", user_id=SALIM, update_id=3))
    assert brain.seen[-1][0] == "отправил Абубакру 100 сомони"


async def test_invited_person_cannot_talk_before_pressing_start(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("отправил 100 сомони", user_id=SALIM))

    assert brain.seen == []
    assert any("/start" in t for t in session.texts)


async def test_voice_is_blocked_until_the_person_gives_a_name(env):
    """Иначе первое голосовое ушло бы в Whisper и Claude мимо регистрации."""
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM))
    await dispatcher.feed_update(bot, voice_update(user_id=SALIM))

    assert brain.seen == []
    assert any("Сначала напиши" in t for t in session.texts)


async def test_photo_is_blocked_until_the_person_gives_a_name(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM))
    await dispatcher.feed_update(bot, photo_update(user_id=SALIM))

    assert db.pending_documents(conn, SALIM) == []


async def test_junk_name_is_rejected_and_asked_again(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM))
    await dispatcher.feed_update(
        bot, text_update("жми https://spam.example", user_id=SALIM, update_id=2))

    assert db.get_user(conn, SALIM)["status"] == "awaiting_name"
    assert brain.seen == []


async def test_registration_survives_a_restart(env):
    """Состояние регистрации лежит в базе, а не в памяти процесса."""
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM))

    assert db.get_user(conn, SALIM)["status"] == "awaiting_name"  # переживёт перезапуск


async def test_start_greets_a_registered_person_by_name(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/start"))
    assert any("Алиджон" in t for t in session.texts)


async def test_rename_changes_the_name(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/imya Али"))
    assert db.get_user(conn, OWNER)["name"] == "Али"


async def test_rename_rejects_junk(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/imya х"))
    assert db.get_user(conn, OWNER)["name"] == "Алиджон"


# ── блокировка ─────────────────────────────────────────────────────────────

async def test_blocked_person_is_stopped(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    db.set_status(conn, SALIM, "blocked")

    await dispatcher.feed_update(bot, text_update("отправил 100 сомони", user_id=SALIM))
    assert brain.seen == []
    assert any("Доступ закрыт" in t for t in session.texts)


async def test_unblocking_brings_the_person_back(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    db.set_status(conn, SALIM, "blocked")
    db.set_status(conn, SALIM, "active")

    await dispatcher.feed_update(bot, text_update("отправил 100 сомони", user_id=SALIM))
    assert brain.seen[-1][0] == "отправил 100 сомони"


async def test_owner_from_the_old_version_keeps_working_without_registering(env, conn):
    """Обновился на сервере — и сразу пишет боту, как раньше, без вопросов."""
    dispatcher, bot, session, brain, conn, _ = env
    db.rename_user(conn, OWNER, None)          # у пришедшего со старой версии имени нет
    db.add_transaction(conn, OWNER, item="сумки", amount=1, currency="TJS")

    await dispatcher.feed_update(bot, text_update("отправил Абубакру 100 сомони"))

    assert brain.seen[-1][0] == "отправил Абубакру 100 сомони"
    # Имя подтянулось из Телеграма молча, вопросов ему не задавали.
    assert db.get_user(conn, OWNER)["name"] == "Алиджон"
    assert not any("зовут" in t for t in session.texts)


async def test_active_person_updates_last_seen(env):
    dispatcher, bot, session, brain, conn, _ = env
    assert db.get_user(conn, OWNER)["last_seen_at"] is None
    await dispatcher.feed_update(bot, text_update("привет"))
    assert db.get_user(conn, OWNER)["last_seen_at"] is not None
