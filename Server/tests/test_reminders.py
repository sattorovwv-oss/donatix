"""Напоминания: расписание, срабатывание, доставка.

Время везде передаётся параметром now — тесты не ждут реальных минут
и не зависят от системных часов.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot import db, reminders as rem
from bot import tools

OWNER, SALIM = 111, 222
TZ = ZoneInfo("Asia/Dushanbe")


def moment(text: str) -> datetime:
    """«2026-09-15 15:00» в поясе пользователя."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


@pytest.fixture
def ctx(conn, config, tmp_path):
    config.ensure_dirs()
    db.invite_user(conn, OWNER)
    db.register_user(conn, OWNER, "Алиджон")
    return tools.ToolContext(
        conn=conn, owner_id=OWNER, result=tools.TurnResult(),
        reports_dir=config.reports_dir, font_path=config.font_path,
        font_bold_path=config.font_bold_path, default_currency="TJS",
        today="2026-09-01", tz=TZ, now=moment("2026-09-01 10:00"),
    )


# ── сборка момента из даты и времени ───────────────────────────────────────

@pytest.mark.parametrize("spoken,expected_hour,expected_minute", [
    ("15:00", 15, 0),      # «в три часа дня»
    ("09:00", 9, 0),       # «утром»
    ("13:00", 13, 0),      # «в обед»
    ("19:30", 19, 30),     # «в полвосьмого вечера»
    ("7", 7, 0),           # модель прислала только час
    (None, 9, 0),          # время не назвали — берётся час по умолчанию
    ("мусор", 9, 0),       # модель прислала ерунду — не падаем
    ("99:99", 23, 59),     # и не вылезаем за границы суток
])
def test_time_from_speech_becomes_a_real_moment(spoken, expected_hour, expected_minute):
    result = rem.local_moment("2026-09-15", spoken, TZ, default_hour=9)
    assert (result.hour, result.minute) == (expected_hour, expected_minute)
    assert result.tzinfo is TZ


def test_moments_are_stored_in_utc():
    """Иначе смена TZ на сервере сдвинет уже назначенные напоминания."""
    local = moment("2026-09-15 15:00")
    stored = rem.to_utc_iso(local)
    assert stored.endswith("+00:00")
    assert rem.parse_utc(stored) == local            # тот же миг времени
    assert rem.fmt_local(stored, TZ) == "15.09.2026 в 15:00"


def test_naive_moment_is_refused():
    with pytest.raises(ValueError):
        rem.to_utc_iso(datetime(2026, 9, 15, 15, 0))


# ── продиктованное напоминание ─────────────────────────────────────────────

def test_dictated_reminder_fires_at_the_named_moment(ctx):
    """«Напомни мне 15 сентября в три часа дня позвонить Абубакру»."""
    out = tools.t_create_reminder(ctx, {
        "when": "2026-09-15", "time": "15:00", "text": "Позвонить Абубакру"})
    assert out["ok"] is True
    assert out["напоминание"]["когда"] == "15.09.2026 в 15:00"

    assert db.due_reminders(ctx.conn, rem.to_utc_iso(moment("2026-09-15 14:59"))) == []
    ready = db.due_reminders(ctx.conn, rem.to_utc_iso(moment("2026-09-15 15:00")))
    assert [r["text"] for r in ready] == ["Позвонить Абубакру"]


def test_reminder_without_a_time_uses_the_default_hour(ctx):
    tools.t_create_reminder(ctx, {"when": "2026-09-15", "text": "Забрать документы"})
    assert db.list_reminders(ctx.conn, OWNER)[0]["fire_at"] == rem.to_utc_iso(
        moment("2026-09-15 09:00"))


def test_reminder_respects_the_users_own_hour(ctx):
    db.set_reminder_hour(ctx.conn, OWNER, 7)
    tools.t_create_reminder(ctx, {"when": "2026-09-15", "text": "Забрать документы"})
    assert db.list_reminders(ctx.conn, OWNER)[0]["fire_at"] == rem.to_utc_iso(
        moment("2026-09-15 07:00"))


def test_reminder_in_the_past_is_refused(ctx):
    out = tools.t_create_reminder(ctx, {"when": "2026-08-01", "text": "Поздно"})
    assert out["ok"] is False
    assert db.list_reminders(ctx.conn, OWNER) == []


def test_reminder_without_date_or_text_is_refused(ctx):
    assert tools.t_create_reminder(ctx, {"text": "Без даты"})["ok"] is False
    assert tools.t_create_reminder(ctx, {"when": "2026-09-15", "text": "  "})["ok"] is False


def test_listing_and_cancelling(ctx):
    tools.t_create_reminder(ctx, {"when": "2026-09-15", "text": "Позвонить Абубакру"})
    listed = tools.t_list_reminders(ctx, {})
    assert listed["найдено"] == 1

    reminder_id = listed["напоминания"][0]["id"]
    assert tools.t_cancel_reminder(ctx, {"reminder_id": reminder_id})["ok"] is True
    assert tools.t_list_reminders(ctx, {})["найдено"] == 0
    assert tools.t_cancel_reminder(ctx, {"reminder_id": reminder_id})["ok"] is False


# ── денежные сроки: ставятся кодом, а не решением модели ───────────────────

def test_saving_with_a_due_date_creates_two_reminders(ctx):
    """За день и в день срока — как просил пользователь."""
    tools.t_save_transaction(ctx, {
        "counterparty": "Абубакр", "amount": 500000, "currency": "тенге",
        "item": "сумки", "due_date": "2026-09-15",
    })
    rows = db.list_reminders(ctx.conn, OWNER)
    assert [r["fire_at"] for r in rows] == [
        rem.to_utc_iso(moment("2026-09-14 09:00")),
        rem.to_utc_iso(moment("2026-09-15 09:00")),
    ]
    assert all(r["kind"] == "due" for r in rows)


def test_the_due_reminder_carries_the_amount(ctx):
    tools.t_save_transaction(ctx, {
        "counterparty": "Абубакр", "amount": 500000, "currency": "KZT",
        "item": "сумки", "due_date": "2026-09-15"})
    texts = [r["text"] for r in db.list_reminders(ctx.conn, OWNER)]
    assert "Завтра срок" in texts[0]
    assert "500 000 KZT" in texts[0]
    assert "Абубакр" in texts[0] and "сумки" in texts[0]


def test_saving_without_a_due_date_creates_nothing(ctx):
    tools.t_save_transaction(ctx, {"counterparty": "Абубакр", "amount": 100})
    assert db.list_reminders(ctx.conn, OWNER) == []


def test_changing_the_due_date_moves_the_reminders(ctx):
    tools.t_save_transaction(ctx, {"amount": 100, "currency": "TJS", "due_date": "2026-09-15"})
    tx_id = ctx.result.saved_transaction_ids[0]

    tools.t_update_transaction(ctx, {"transaction_id": tx_id, "due_date": "2026-09-20"})

    rows = db.list_reminders(ctx.conn, OWNER)
    assert len(rows) == 2   # старые отменены, не накопились
    assert rows[0]["fire_at"] == rem.to_utc_iso(moment("2026-09-19 09:00"))


def test_deleting_the_transaction_cancels_its_reminders(ctx):
    tools.t_save_transaction(ctx, {"amount": 100, "currency": "TJS", "due_date": "2026-09-15"})
    tx_id = ctx.result.saved_transaction_ids[0]

    tools.t_delete_transaction(ctx, {"transaction_id": tx_id})
    assert db.list_reminders(ctx.conn, OWNER) == []


def test_a_due_date_already_passed_creates_nothing(ctx):
    """Напоминать в прошлое бессмысленно — этим займётся проверка просрочек."""
    tools.t_save_transaction(ctx, {"amount": 100, "currency": "TJS", "due_date": "2026-08-01"})
    assert db.list_reminders(ctx.conn, OWNER) == []


def test_a_due_date_today_still_warns_if_the_hour_has_not_passed(ctx):
    ctx.now = moment("2026-09-15 07:00")
    tools.t_save_transaction(ctx, {"amount": 100, "currency": "TJS", "due_date": "2026-09-15"})
    assert len(db.list_reminders(ctx.conn, OWNER)) == 1   # только «сегодня», вчера уже поздно


# ── просрочка ──────────────────────────────────────────────────────────────

def test_overdue_transaction_gets_a_reminder(conn):
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, counterparty="Абубакр", amount=100,
                       currency="TJS", due_date="2026-09-01")

    rem.schedule_overdue(conn, OWNER, TZ, now=moment("2026-09-05 10:00"))
    rows = db.list_reminders(conn, OWNER)
    assert len(rows) == 1
    assert "Просрочено на 4 дн." in rows[0]["text"]


def test_overdue_does_not_pile_up_day_after_day(conn):
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, amount=100, currency="TJS", due_date="2026-09-01")

    for _ in range(5):
        rem.schedule_overdue(conn, OWNER, TZ, now=moment("2026-09-05 10:00"))
    assert len(db.list_reminders(conn, OWNER)) == 1


def test_a_closed_transaction_stops_nagging(conn):
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, amount=100, currency="TJS",
                       due_date="2026-09-01", note="закрыто, отдал")
    rem.schedule_overdue(conn, OWNER, TZ, now=moment("2026-09-05 10:00"))
    assert db.list_reminders(conn, OWNER) == []


# ── неподписанные фото ─────────────────────────────────────────────────────

def test_an_unsigned_photo_is_reminded_about(conn):
    db.invite_user(conn, OWNER)
    doc_id = db.add_document(conn, OWNER, tg_file_id="f1", file_path="/tmp/a.jpg")

    later = rem.parse_utc(db.get_document(conn, OWNER, doc_id)["created_at"]) + timedelta(hours=20)
    rem.schedule_photo_reminders(conn, TZ, now=later)

    rows = db.list_reminders(conn, OWNER)
    assert len(rows) == 1 and rows[0]["kind"] == "photo"


def test_a_fresh_photo_is_left_alone(conn):
    db.invite_user(conn, OWNER)
    db.add_document(conn, OWNER, tg_file_id="f1", file_path="/tmp/a.jpg")
    rem.schedule_photo_reminders(conn, TZ, now=rem.utc_now())
    assert db.list_reminders(conn, OWNER) == []


def test_a_described_photo_is_never_reminded_about(conn):
    db.invite_user(conn, OWNER)
    doc_id = db.add_document(conn, OWNER, tg_file_id="f1", file_path="/tmp/a.jpg")
    db.describe_document(conn, OWNER, doc_id, description="накладная на обувь")

    later = rem.utc_now() + timedelta(days=2)
    rem.schedule_photo_reminders(conn, TZ, now=later)
    assert db.list_reminders(conn, OWNER) == []


def test_photo_reminder_is_not_repeated(conn):
    db.invite_user(conn, OWNER)
    db.add_document(conn, OWNER, tg_file_id="f1", file_path="/tmp/a.jpg")
    later = rem.utc_now() + timedelta(days=1)

    for _ in range(4):
        rem.schedule_photo_reminders(conn, TZ, now=later)
    assert len(db.list_reminders(conn, OWNER)) == 1


# ── изоляция ───────────────────────────────────────────────────────────────

def test_reminders_do_not_leak_between_people(conn):
    for who in (OWNER, SALIM):
        db.invite_user(conn, who)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 10:00")),
                    text="моё напоминание")

    assert db.list_reminders(conn, SALIM) == []
    assert db.cancel_reminder(conn, SALIM, db.list_reminders(conn, OWNER)[0]["id"]) is False


# ── доставка ───────────────────────────────────────────────────────────────

class FakeBot:
    """Считает отправленное. Может «падать» — как заблокировавший бота человек."""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


async def test_delivery_sends_only_what_is_ready(conn):
    db.invite_user(conn, OWNER)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                    text="Позвонить Абубакру")
    bot = FakeBot()

    assert await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 14:59")) == 0
    assert await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 15:00")) == 1
    assert "Позвонить Абубакру" in bot.sent[0][1]


async def test_a_reminder_is_never_sent_twice(conn):
    db.invite_user(conn, OWNER)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")), text="Раз")
    bot = FakeBot()

    for _ in range(3):
        await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 16:00"))
    assert len(bot.sent) == 1


async def test_a_reminder_missed_while_the_bot_was_down_still_arrives(conn):
    """Сервер полежал — напоминание должно прийти, но честно сказать, что опоздало."""
    db.invite_user(conn, OWNER)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                    text="Позвонить Абубакру")
    bot = FakeBot()

    await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-16 10:00"))
    assert len(bot.sent) == 1
    assert "было назначено на 15.09.2026 в 15:00" in bot.sent[0][1]


async def test_very_stale_reminders_are_not_dumped_on_the_user(conn):
    """После недельного простоя десяток протухших напоминаний хуже молчания."""
    db.invite_user(conn, OWNER)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-01 15:00")),
                    text="Старьё")
    bot = FakeBot()

    await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 10:00"))
    assert bot.sent == []
    assert db.list_reminders(conn, OWNER) == []   # и не висит вечно в очереди


async def test_one_blocked_user_does_not_stop_the_others(conn):
    """Иначе один человек, заблокировавший бота, лишил бы напоминаний всех."""
    for who in (OWNER, SALIM):
        db.invite_user(conn, who)
        db.add_reminder(conn, who, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                        text=f"для {who}")
    bot = FakeBot(fail_for={OWNER})

    await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 15:00"))

    assert [chat for chat, _ in bot.sent] == [SALIM]
    assert db.list_reminders(conn, OWNER) == []   # не зависло, повторов не будет


async def test_each_person_gets_only_their_own(conn):
    for who in (OWNER, SALIM):
        db.invite_user(conn, who)
    db.add_reminder(conn, OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")), text="моё")
    bot = FakeBot()

    await rem.deliver_due(bot, conn, TZ, now=moment("2026-09-15 15:00"))
    assert [chat for chat, _ in bot.sent] == [OWNER]


# ── команды и кнопки, через настоящий Dispatcher ───────────────────────────

from conftest import OWNER as CHAT_OWNER, text_update  # noqa: E402
from bot import keyboards as kb  # noqa: E402


def press(data: str, user_id: int = CHAT_OWNER, update_id: int = 80):
    from datetime import datetime as dt

    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Алиджон")
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=f"q{update_id}", from_user=user, chat_instance="ci", data=data,
        message=Message(message_id=9, date=dt.now(), chat=chat, text="⏰ напоминание"),
    ))


async def test_command_lists_reminders(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.add_reminder(conn, CHAT_OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                    text="Позвонить Абубакру")

    await dispatcher.feed_update(bot, text_update("/napominaniya"))
    assert any("Позвонить Абубакру" in t for t in session.texts)


async def test_command_when_there_is_nothing(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/napominaniya"))
    assert any("Напоминаний нет" in t for t in session.texts)


async def test_done_button_removes_the_reminder(env):
    dispatcher, bot, session, brain, conn, _ = env
    rid = db.add_reminder(conn, CHAT_OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                          text="Позвонить")

    await dispatcher.feed_update(bot, press(f"{kb.REMINDER_DONE}{rid}"))
    assert db.list_reminders(conn, CHAT_OWNER) == []


async def test_snooze_button_moves_it_a_day_ahead(env):
    dispatcher, bot, session, brain, conn, _ = env
    rid = db.add_reminder(conn, CHAT_OWNER, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                          text="Позвонить")

    await dispatcher.feed_update(bot, press(f"{kb.REMINDER_SNOOZE}{rid}"))
    still_there = db.list_reminders(conn, CHAT_OWNER)
    assert len(still_there) == 1
    assert rem.parse_utc(still_there[0]["fire_at"]) > rem.utc_now()


async def test_someone_elses_reminder_button_does_nothing(env):
    """Кнопку можно переслать — чужое напоминание она трогать не должна."""
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    rid = db.add_reminder(conn, SALIM, fire_at=rem.to_utc_iso(moment("2026-09-15 15:00")),
                          text="чужое")

    await dispatcher.feed_update(bot, press(f"{kb.REMINDER_DONE}{rid}"))
    assert len(db.list_reminders(conn, SALIM)) == 1


async def test_hour_command_changes_when_automatic_reminders_arrive(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya 7"))
    assert db.reminder_hour(conn, CHAT_OWNER) == 7


async def test_hour_command_rejects_nonsense(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya вечером"))
    assert db.reminder_hour(conn, CHAT_OWNER) == db.DEFAULT_REMINDER_HOUR
    assert any("Не знаю такой часовой пояс" in t for t in session.texts)


async def test_time_card_shows_the_current_time(env):
    """Прямой ответ на «а сколько сейчас времени» — сверяется с телефоном."""
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya"))

    card = "\n".join(session.texts)
    assert "Сейчас у тебя:" in card
    assert "Душанбе (UTC+5)" in card


async def test_timezone_button_switches_the_zone(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(f"{kb.TZ_PREFIX}Europe/Moscow"))

    assert db.user_tz_name(conn, CHAT_OWNER, "Asia/Dushanbe") == "Europe/Moscow"


async def test_timezone_can_be_set_by_name(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya Asia/Almaty"))

    assert db.user_tz_name(conn, CHAT_OWNER, "Asia/Dushanbe") == "Asia/Almaty"
    assert any("Алматы" in t for t in session.texts)


async def test_hour_still_works_alongside_zones(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya 7"))
    assert db.reminder_hour(conn, CHAT_OWNER) == 7


async def test_out_of_range_hour_is_refused(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/vremya 99"))
    assert db.reminder_hour(conn, CHAT_OWNER) == db.DEFAULT_REMINDER_HOUR


async def test_reminder_command_does_not_reach_claude(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("/napominaniya"))
    assert brain.seen == []


# ── переход на новую версию ────────────────────────────────────────────────

def test_existing_due_dates_get_reminders_after_the_update(conn):
    """Записи, сделанные до появления напоминаний, не должны остаться без них."""
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, counterparty="Абубакр", amount=500000,
                       currency="KZT", item="сумки", due_date="2026-09-15")

    created = rem.backfill_due_reminders(conn, OWNER, TZ, now=moment("2026-09-01 10:00"))

    assert len(created) == 2   # за день и в день
    assert len(db.list_reminders(conn, OWNER)) == 2


def test_backfill_does_not_duplicate_what_already_exists(conn):
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, amount=100, currency="TJS", due_date="2026-09-15")

    for _ in range(3):
        rem.backfill_due_reminders(conn, OWNER, TZ, now=moment("2026-09-01 10:00"))
    assert len(db.list_reminders(conn, OWNER)) == 2


def test_backfill_ignores_dates_already_passed(conn):
    """Прошедшими сроками занимается проверка просрочек, а не досоздание."""
    db.invite_user(conn, OWNER)
    db.add_transaction(conn, OWNER, amount=100, currency="TJS", due_date="2026-08-01")

    assert rem.backfill_due_reminders(conn, OWNER, TZ, now=moment("2026-09-01 10:00")) == []


# ── «через N секунд» ───────────────────────────────────────────────────────

MSK = ZoneInfo("Europe/Moscow")


def test_thirty_seconds_fires_at_thirty_seconds(ctx):
    """«Напомни через 30 секунд проверить бота»."""
    ctx.now = moment("2026-08-31 17:32")
    out = tools.t_create_reminder(ctx, {"after_seconds": 30, "text": "Проверить бота"})

    assert out["ok"] is True
    assert out["напоминание"]["когда"] == "через 30 секунд"

    fire = ctx.now + timedelta(seconds=30)
    assert db.due_reminders(ctx.conn, rem.to_utc_iso(fire - timedelta(seconds=1))) == []
    assert len(db.due_reminders(ctx.conn, rem.to_utc_iso(fire))) == 1


@pytest.mark.parametrize("seconds,expected", [
    (60, timedelta(minutes=1)),        # «через минуту»
    (300, timedelta(minutes=5)),       # «через 5 минут»
    (1800, timedelta(minutes=30)),     # «через полчаса»
    (7200, timedelta(hours=2)),        # «через два часа»
])
def test_relative_delays(ctx, seconds, expected):
    ctx.now = moment("2026-08-31 17:32")
    tools.t_create_reminder(ctx, {"after_seconds": seconds, "text": "Дело"})
    stored = rem.parse_utc(db.list_reminders(ctx.conn, OWNER)[0]["fire_at"])
    assert stored - ctx.now == expected


@pytest.mark.parametrize("seconds", [0, 1, 4, -30])
def test_too_soon_is_refused(ctx, seconds):
    """Меньше пяти секунд человек не успеет даже дочитать ответ бота."""
    out = tools.t_create_reminder(ctx, {"after_seconds": seconds, "text": "Дело"})
    assert out["ok"] is False
    assert db.list_reminders(ctx.conn, OWNER) == []


def test_more_than_a_year_is_refused(ctx):
    """Почти наверняка ошибка распознавания, а не настоящая просьба."""
    out = tools.t_create_reminder(ctx, {"after_seconds": 400 * 24 * 3600, "text": "Дело"})
    assert out["ok"] is False


def test_reminder_needs_either_a_date_or_a_delay(ctx):
    assert tools.t_create_reminder(ctx, {"text": "Без момента"})["ok"] is False


def test_delay_wins_over_date_if_both_given(ctx):
    """Промпт велит заполнять что-то одно; если модель прислала оба — берём «через»."""
    ctx.now = moment("2026-08-31 17:32")
    tools.t_create_reminder(ctx, {
        "after_seconds": 60, "when": "2026-12-31", "text": "Дело"})
    stored = rem.parse_utc(db.list_reminders(ctx.conn, OWNER)[0]["fire_at"])
    assert stored == ctx.now + timedelta(minutes=1)


# ── секунды в конкретном времени ───────────────────────────────────────────

def test_time_with_seconds(ctx):
    tools.t_create_reminder(ctx, {
        "when": "2026-09-15", "time": "15:30:45", "text": "Точно в срок"})
    assert db.list_reminders(ctx.conn, OWNER)[0]["fire_at"] == rem.to_utc_iso(
        moment("2026-09-15 15:30").replace(second=45))


def test_seconds_are_shown_only_when_they_matter(ctx):
    with_seconds = rem.to_utc_iso(moment("2026-09-15 15:30").replace(second=45))
    round_minute = rem.to_utc_iso(moment("2026-09-15 15:30"))
    assert rem.fmt_local(with_seconds, TZ) == "15.09.2026 в 15:30:45"
    assert rem.fmt_local(round_minute, TZ) == "15.09.2026 в 15:30"


# ── завтра и послезавтра ───────────────────────────────────────────────────

@pytest.mark.parametrize("when,expected_day", [
    ("2026-09-01", 1),   # «завтра» при сегодня 31.08
    ("2026-09-02", 2),   # «послезавтра»
    ("2026-09-07", 7),   # «через неделю»
])
def test_relative_days(ctx, when, expected_day):
    ctx.now = moment("2026-08-31 17:32")
    tools.t_create_reminder(ctx, {"when": when, "time": "15:00", "text": "Дело"})
    stored = rem.parse_utc(db.list_reminders(ctx.conn, OWNER)[0]["fire_at"]).astimezone(TZ)
    assert stored.date() == moment("2026-08-31 00:00").date() + timedelta(days=expected_day)
    assert stored.hour == 15


# ── часовой пояс у каждого свой ────────────────────────────────────────────

def test_same_words_mean_different_moments_in_different_zones(conn, config, tmp_path):
    """«Завтра в 9 утра» в Душанбе и в Москве — это разные мгновения."""
    config.ensure_dirs()
    dushanbe, moscow = 111, 222
    for who, zone in ((dushanbe, "Asia/Dushanbe"), (moscow, "Europe/Moscow")):
        db.invite_user(conn, who)
        db.register_user(conn, who, str(who))
        db.set_user_tz(conn, who, zone)

    for who in (dushanbe, moscow):
        ctx = tools.ToolContext(
            conn=conn, owner_id=who, result=tools.TurnResult(),
            reports_dir=config.reports_dir, default_currency="TJS",
            tz=TZ, now=moment("2026-08-31 17:32"),
        )
        tools.t_create_reminder(ctx, {"when": "2026-09-01", "time": "09:00", "text": "Утро"})

    at_dushanbe = rem.parse_utc(db.list_reminders(conn, dushanbe)[0]["fire_at"])
    at_moscow = rem.parse_utc(db.list_reminders(conn, moscow)[0]["fire_at"])

    assert at_moscow - at_dushanbe == timedelta(hours=2)   # Москва на 2 часа позади
    # Но каждому показывается ровно 09:00 — его собственных.
    assert rem.fmt_local(db.list_reminders(conn, dushanbe)[0]["fire_at"], TZ).endswith("09:00")
    assert rem.fmt_local(db.list_reminders(conn, moscow)[0]["fire_at"], MSK).endswith("09:00")


def test_unknown_zone_in_the_database_does_not_break_anything(conn):
    db.invite_user(conn, OWNER)
    conn.execute("UPDATE users SET tz='Марс/Кратер' WHERE id=?", (OWNER,))
    conn.commit()
    assert db.user_tz(conn, OWNER, TZ) is TZ


def test_a_bad_zone_is_never_saved(conn):
    db.invite_user(conn, OWNER)
    assert db.set_user_tz(conn, OWNER, "Марс/Кратер") is False
    assert db.set_user_tz(conn, OWNER, "Asia/Almaty") is True
    assert db.user_tz_name(conn, OWNER, "Asia/Dushanbe") == "Asia/Almaty"


def test_without_a_zone_the_shared_one_is_used(conn):
    db.invite_user(conn, OWNER)
    assert db.user_tz(conn, OWNER, TZ) is TZ
