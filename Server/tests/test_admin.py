"""Панель /admin: права, кнопки и защита от выстрела себе в ногу."""

from datetime import datetime

import pytest
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import admin as admin_module
from bot import db, keyboards as kb

from conftest import OWNER, STRANGER, text_update

SALIM, KARIM = 222, 333


def press(data: str, user_id: int = OWNER, update_id: int = 50) -> Update:
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Кто-то")
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=f"q{update_id}", from_user=user, chat_instance="ci", data=data,
        message=Message(message_id=5, date=datetime.now(), chat=chat, text="панель"),
    ))


def all_text(session) -> str:
    parts = []
    for m in session.sent:
        parts.append(getattr(m, "text", "") or "")
    return "\n".join(parts)


# ── права ──────────────────────────────────────────────────────────────────

async def test_owner_sees_the_panel(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    await dispatcher.feed_update(bot, text_update("/admin"))

    text = all_text(session)
    assert "Пользователи" in text
    assert str(SALIM) in text


async def test_ordinary_person_is_refused_the_panel(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    await dispatcher.feed_update(bot, text_update("/admin", user_id=SALIM))

    assert "Пользователи" not in all_text(session)
    assert "только для владельца" in all_text(session)


async def test_ordinary_person_cannot_press_panel_buttons(env):
    """Кнопки — отдельная поверхность: пересланное сообщение не должно работать."""
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    db.invite_user(conn, KARIM)

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_YES}{KARIM}", user_id=SALIM))
    assert db.get_user(conn, KARIM) is not None   # ничего не удалилось


async def test_stranger_cannot_press_panel_buttons(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, KARIM)
    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_YES}{KARIM}", user_id=STRANGER))
    assert db.get_user(conn, KARIM) is not None


# ── добавление по ID ───────────────────────────────────────────────────────

async def test_adding_a_person_by_id(env):
    dispatcher, bot, session, brain, conn, _ = env

    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update(str(SALIM), update_id=60))

    user = db.get_user(conn, SALIM)
    assert user is not None and user["status"] == "invited"
    assert "moneybot" in all_text(session)   # готовая строка для пересылки


async def test_a_number_is_not_treated_as_a_transaction_while_adding(env):
    """Пока владелец вводит id, его сообщение не должно уйти в Claude как операция."""
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update(str(SALIM), update_id=60))
    assert brain.seen == []


async def test_normal_messages_still_reach_claude_after_adding(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update(str(SALIM), update_id=60))
    await dispatcher.feed_update(bot, text_update("отправил 100 сомони", update_id=61))
    assert brain.seen[-1][0] == "отправил 100 сомони"


async def test_non_number_is_rejected_and_nobody_is_added(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update("абракадабра", update_id=60))

    assert "Нужно число" in all_text(session)
    assert len(db.list_users(conn)) == 1   # только владелец


async def test_adding_someone_who_is_already_there(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")

    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update(str(SALIM), update_id=60))

    assert "уже в списке" in all_text(session)
    assert db.get_user(conn, SALIM)["status"] == "active"   # не сброшен


async def test_grant_button_from_the_stranger_notice(env):
    """Главный путь пользователя: незнакомец постучался — владелец нажал кнопку."""
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, text_update("привет", user_id=STRANGER))
    assert db.get_user(conn, STRANGER) is None

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_GRANT}{STRANGER}"))
    assert db.get_user(conn, STRANGER)["status"] == "invited"

    # Теперь он может зарегистрироваться.
    await dispatcher.feed_update(bot, text_update("/start", user_id=STRANGER, update_id=70))
    assert db.get_user(conn, STRANGER)["status"] == "awaiting_name"


# ── блокировка и удаление ──────────────────────────────────────────────────

async def test_block_and_unblock(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_BLOCK}{SALIM}"))
    assert db.get_user(conn, SALIM)["status"] == "blocked"

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_UNBLOCK}{SALIM}", update_id=51))
    assert db.get_user(conn, SALIM)["status"] == "active"


async def test_unblocking_someone_who_never_registered_sends_them_back_to_start(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.set_status(conn, SALIM, "blocked")

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_UNBLOCK}{SALIM}"))
    assert db.get_user(conn, SALIM)["status"] == "invited"


async def test_owner_cannot_block_himself(env):
    """Иначе можно остаться без управления собственным ботом."""
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_BLOCK}{OWNER}"))
    assert db.get_user(conn, OWNER)["status"] == "active"


async def test_owner_cannot_delete_himself(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_YES}{OWNER}"))
    assert db.get_user(conn, OWNER) is not None


async def test_the_last_admin_cannot_be_blocked(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.ensure_admin(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")

    # Салим — второй админ, его заблокировать можно.
    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_BLOCK}{SALIM}"))
    assert db.get_user(conn, SALIM)["status"] == "blocked"
    # Владелец остался единственным — себя он заблокировать не может (проверено выше).
    assert db.count_admins(conn) == 1


async def test_delete_asks_before_wiping(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.add_transaction(conn, SALIM, amount=1, currency="TJS")

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_ASK}{SALIM}"))

    assert db.get_user(conn, SALIM) is not None   # пока ничего не удалено
    edits = [m for m in session.sent if isinstance(m, EditMessageText)]
    assert any("нельзя" in (m.text or "") for m in edits)


async def test_confirmed_delete_wipes_the_person_and_their_records(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.invite_user(conn, SALIM)
    db.add_transaction(conn, SALIM, item="сумки", amount=1, currency="TJS")

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_YES}{SALIM}"))

    assert db.get_user(conn, SALIM) is None
    assert db.search_transactions(conn, SALIM) == []


async def test_deleting_one_person_leaves_the_owners_records_alone(env):
    dispatcher, bot, session, brain, conn, _ = env
    db.add_transaction(conn, OWNER, item="сумки", amount=500000, currency="KZT")
    db.invite_user(conn, SALIM)
    db.add_transaction(conn, SALIM, item="сумки", amount=1, currency="TJS")

    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_WIPE_YES}{SALIM}"))
    assert len(db.search_transactions(conn, OWNER, text="сумки")) == 1


async def test_acting_on_a_deleted_person_does_not_crash(env):
    dispatcher, bot, session, brain, conn, _ = env
    await dispatcher.feed_update(bot, press(f"{kb.ADMIN_BLOCK}{999999}"))
    assert db.get_user(conn, 999999) is None


# ── тексты панели ──────────────────────────────────────────────────────────

def test_panel_shows_counts_but_not_other_peoples_records(conn):
    """Владелец видит, сколько у кого записей, но не сами чужие операции."""
    db.ensure_admin(conn, OWNER)
    db.register_user(conn, OWNER, "Алиджон")
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    db.add_transaction(conn, SALIM, counterparty="СЕКРЕТ", item="СЕКРЕТ",
                       amount=999, currency="TJS")

    text = admin_module.users_text(conn)
    assert "Салим" in text and "1 записей" in text
    assert "СЕКРЕТ" not in text and "999" not in text


def test_user_card_hides_the_records_too(conn):
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    db.add_transaction(conn, SALIM, counterparty="СЕКРЕТ", amount=999, currency="TJS")

    card = admin_module.user_card_text(conn, db.get_user(conn, SALIM))
    assert "операций: 1" in card
    assert "СЕКРЕТ" not in card


def test_empty_panel_says_so(conn):
    assert "никого" in admin_module.users_text(conn)


@pytest.mark.parametrize("iso,expected", [
    (None, "ни разу"),
    (datetime.now().isoformat(), "сегодня"),
])
def test_last_seen_is_human_readable(iso, expected):
    assert admin_module._ago(iso) == expected


# ── путь пользователя целиком ──────────────────────────────────────────────

async def test_full_story_owner_adds_a_person_who_then_keeps_his_own_books(env):
    """Ровно то, что просил владелец: даю доступ по ID → человек жмёт Старт →
    вводит имя → ведёт свой учёт, которого я не вижу в своём."""
    dispatcher, bot, session, brain, conn, _ = env

    # 1. Владелец добавляет человека по id прямо с телефона.
    await dispatcher.feed_update(bot, press(kb.ADMIN_ADD))
    await dispatcher.feed_update(bot, text_update(str(SALIM), update_id=60))
    assert db.get_user(conn, SALIM)["status"] == "invited"

    # 2. Человек жмёт Старт и представляется.
    await dispatcher.feed_update(bot, text_update("/start", user_id=SALIM, update_id=61))
    await dispatcher.feed_update(bot, text_update("Салим", user_id=SALIM, update_id=62))
    assert db.get_user(conn, SALIM)["status"] == "active"

    # 3. Оба ведут свой учёт.
    db.add_transaction(conn, OWNER, item="сумки", amount=500000, currency="KZT",
                       counterparty="Абубакр")
    db.add_transaction(conn, SALIM, item="сумки", amount=700, currency="TJS",
                       counterparty="Абубакр")

    # 4. Каждый видит только своё.
    assert [r["amount"] for r in db.search_transactions(conn, OWNER, text="сумки")] == [500000]
    assert [r["amount"] for r in db.search_transactions(conn, SALIM, text="сумки")] == [700]

    # 5. В панели у владельца видно обоих и сколько у кого записей.
    panel = admin_module.users_text(conn)
    assert "Салим" in panel and "1 записей" in panel


async def test_reports_of_two_people_do_not_mix(env, config):
    """Отчёт одного не должен содержать ни строчки из чужого."""
    from bot import reports

    dispatcher, bot, session, brain, conn, _ = env
    config.ensure_dirs()
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")

    db.add_transaction(conn, OWNER, item="мойтовар", amount=500000, currency="KZT",
                       counterparty="Абубакр", happened_on="2026-08-10")
    db.add_transaction(conn, SALIM, item="чужойтовар", amount=700, currency="TJS",
                       counterparty="Карим", happened_on="2026-08-10")

    path, count = reports.build_report(
        conn, OWNER, date_from="2026-08-01", date_to="2026-08-31",
        out_dir=config.reports_dir, font_path=config.font_path,
        font_bold_path=config.font_bold_path,
    )
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)

    assert count == 1
    assert "мойтовар" in text
    assert "чужойтовар" not in text
    assert "Карим" not in text
