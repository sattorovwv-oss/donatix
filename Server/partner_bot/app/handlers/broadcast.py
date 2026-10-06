"""Рассылка: любой тип сообщения + кнопки со ссылками.

Сообщение не пересобирается, а копируется через copyMessage — поэтому
работает всё, что вы можете отправить боту: текст, фото, видео, GIF,
документ, аудио, кружок, стикер. Кнопки добавляются поверх копии.
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.services import access
from app.handlers.panel import AUDIENCES, back_kb, safe_edit, show_home
from app.keyboards import DANGER, PRIMARY, SUCCESS, btn
from app.states import Cast

log = logging.getLogger(__name__)
router = Router(name="broadcast")

router.message.filter(F.from_user.func(lambda u: access.is_admin(u.id)))
router.callback_query.filter(F.from_user.func(lambda u: access.is_admin(u.id)))

# «Название - https://...» или «Название | https://...»
BUTTON_RE = re.compile(r"^(.{1,64}?)\s*[|\-–—]\s*(https?://\S+|t\.me/\S+)$")

SEND_DELAY = 0.05        # ~20 сообщений в секунду — предел Telegram
PROGRESS_EVERY = 25


# ------------------------------------------------------------- разметка


def audience_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (label, _) in AUDIENCES.items():
        kb.row(InlineKeyboardButton(text=label, callback_data=f"bc:aud:{key}"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


def compose_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("❌ Отмена", "pn:home", style=DANGER))
    return kb.as_markup()


def buttons_kb(has_buttons: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Добавить кнопки", callback_data="bc:buttons"))
    if has_buttons:
        kb.row(btn("🗑 Убрать кнопки", "bc:clear", style=DANGER))
    kb.row(btn("👁 Предпросмотр", "bc:preview", style=PRIMARY))
    kb.row(btn("🚀 Отправить", "bc:send", style=SUCCESS))
    kb.row(btn("❌ Отмена", "pn:home", style=DANGER))
    return kb.as_markup()


def build_markup(buttons: list[list[str]]) -> InlineKeyboardMarkup | None:
    """Собрать клавиатуру рассылки: по одной кнопке в ряд."""
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for title, url in buttons:
        kb.row(InlineKeyboardButton(text=title, url=url))
    return kb.as_markup()


async def count_audience(conn: aiosqlite.Connection, key: str) -> int:
    _, where = AUDIENCES[key]
    async with conn.execute(f"SELECT COUNT(*) AS c FROM users WHERE {where}") as cur:
        return (await cur.fetchone())["c"]


async def audience_ids(conn: aiosqlite.Connection, key: str) -> list[int]:
    _, where = AUDIENCES[key]
    async with conn.execute(f"SELECT id FROM users WHERE {where}") as cur:
        return [row["id"] for row in await cur.fetchall()]


# ------------------------------------------------------------- сценарий


@router.callback_query(F.data == "pn:cast")
async def cb_start(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    lines = []
    for key, (label, _) in AUDIENCES.items():
        lines.append(f"• {label} — <b>{await count_audience(conn, key)}</b>")
    await safe_edit(
        call,
        "📣 <b>Рассылка</b>\n\nКому отправляем?\n\n" + "\n".join(lines),
        audience_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("bc:aud:"))
async def cb_audience(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    key = call.data.rsplit(":", 1)[1]
    if key not in AUDIENCES:
        await call.answer("Неизвестная группа.", show_alert=True)
        return

    total = await count_audience(conn, key)
    if total == 0:
        await call.answer("В этой группе никого нет.", show_alert=True)
        return

    await state.set_state(Cast.content)
    await state.update_data(audience=key, buttons=[])
    await safe_edit(
        call,
        f"📣 <b>Рассылка · {AUDIENCES[key][0]}</b>\n"
        f"Получателей: <b>{total}</b>\n\n"
        "Пришлите сообщение, которое надо разослать.\n\n"
        "Подойдёт что угодно: текст, фото, видео, GIF, документ, голосовое, "
        "кружок, стикер. Форматирование и подпись сохранятся.",
        compose_kb(),
    )
    await call.answer()


@router.message(Cast.content)
async def on_content(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    data = await state.get_data()
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Cast.confirm)

    total = await count_audience(conn, data["audience"])
    await message.answer(
        f"📣 <b>Сообщение принято</b>\n\n"
        f"Кому: <b>{AUDIENCES[data['audience']][0]}</b> ({total} чел.)\n"
        f"Кнопок: <b>нет</b>\n\n"
        "Можно добавить кнопки со ссылками или сразу отправить.",
        reply_markup=buttons_kb(has_buttons=False),
    )


@router.callback_query(Cast.confirm, F.data == "bc:buttons")
async def cb_ask_buttons(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Cast.buttons)
    await safe_edit(
        call,
        "🔗 <b>Кнопки под сообщением</b>\n\n"
        "Пришлите список — по одной кнопке в строке:\n\n"
        "<code>Наш канал - https://t.me/mychannel</code>\n"
        "<code>Купить звёзды - https://t.me/mybot</code>\n"
        "<code>Отзывы - https://t.me/reviews</code>\n\n"
        "Разделитель — дефис или вертикальная черта. "
        "Ссылка должна начинаться с <code>https://</code> или <code>t.me/</code>.",
        back_kb("bc:back", "‹ Назад"),
    )
    await call.answer()


@router.message(Cast.buttons, F.text)
async def on_buttons(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    parsed: list[list[str]] = []
    bad: list[str] = []

    for line in (message.text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = BUTTON_RE.match(line)
        if not match:
            bad.append(line)
            continue
        title, url = match.group(1).strip(), match.group(2).strip()
        if url.startswith("t.me/"):
            url = "https://" + url
        parsed.append([title, url])

    if bad:
        await message.answer(
            "❌ Не разобрал эти строки:\n\n"
            + "\n".join(f"• <code>{line[:60]}</code>" for line in bad[:5])
            + "\n\nНужен формат: <code>Название - https://ссылка</code>"
        )
        return
    if not parsed:
        await message.answer("❌ Не нашёл ни одной кнопки. Пришлите хотя бы одну строку.")
        return
    if len(parsed) > 10:
        await message.answer("❌ Максимум 10 кнопок.")
        return

    await state.update_data(buttons=parsed)
    await state.set_state(Cast.confirm)

    data = await state.get_data()
    total = await count_audience(conn, data["audience"])
    listing = "\n".join(f"├ {title} → {url}" for title, url in parsed)
    await message.answer(
        f"✅ <b>Кнопок добавлено: {len(parsed)}</b>\n\n{listing}\n\n"
        f"Кому: <b>{AUDIENCES[data['audience']][0]}</b> ({total} чел.)",
        reply_markup=buttons_kb(has_buttons=True),
    )


@router.callback_query(Cast.buttons, F.data == "bc:back")
async def cb_buttons_back(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.set_state(Cast.confirm)
    data = await state.get_data()
    total = await count_audience(conn, data["audience"])
    await safe_edit(
        call,
        f"📣 <b>Рассылка · {AUDIENCES[data['audience']][0]}</b>\n"
        f"Получателей: <b>{total}</b>\n"
        f"Кнопок: <b>{len(data.get('buttons') or [])}</b>",
        buttons_kb(bool(data.get("buttons"))),
    )
    await call.answer()


@router.callback_query(Cast.confirm, F.data == "bc:clear")
async def cb_clear_buttons(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(buttons=[])
    await call.answer("Кнопки убраны")
    await safe_edit(call, "📣 Кнопки убраны. Можно отправлять.", buttons_kb(False))


@router.callback_query(Cast.confirm, F.data == "bc:preview")
async def cb_preview(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    try:
        await bot.copy_message(
            chat_id=call.from_user.id,
            from_chat_id=data["from_chat_id"],
            message_id=data["message_id"],
            reply_markup=build_markup(data.get("buttons") or []),
        )
    except TelegramAPIError as exc:
        await call.answer(f"Не смог показать: {exc}"[:190], show_alert=True)
        return
    await call.message.answer(
        "👆 <b>Так это увидят клиенты.</b>\n\nОтправляем?",
        reply_markup=buttons_kb(bool(data.get("buttons"))),
    )
    await call.answer()


@router.callback_query(Cast.confirm, F.data == "bc:send")
async def cb_send(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    data = await state.get_data()
    await state.clear()
    await call.answer()

    recipients = await audience_ids(conn, data["audience"])
    markup = build_markup(data.get("buttons") or [])
    status = await call.message.answer(
        f"🚀 Рассылка пошла: <b>0 / {len(recipients)}</b>"
    )

    sent = blocked = failed = 0
    for index, user_id in enumerate(recipients, start=1):
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=data["from_chat_id"],
                message_id=data["message_id"],
                reply_markup=markup,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            # Telegram просит притормозить — ждём и пробуем этого же ещё раз.
            log.warning("Рассылка: пауза %s сек по требованию Telegram", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=data["from_chat_id"],
                    message_id=data["message_id"],
                    reply_markup=markup,
                )
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1          # заблокировал бота или удалил аккаунт
        except TelegramAPIError as exc:
            failed += 1
            log.debug("Рассылка: %s не получил — %s", user_id, exc)

        await asyncio.sleep(SEND_DELAY)

        if index % PROGRESS_EVERY == 0:
            try:
                await status.edit_text(
                    f"🚀 Рассылка идёт: <b>{index} / {len(recipients)}</b>\n"
                    f"✅ {sent}  ·  🚫 {blocked}  ·  ❌ {failed}"
                )
            except TelegramAPIError:
                pass

    await status.edit_text(
        f"📣 <b>Рассылка завершена</b>\n\n"
        f"├ Отправлено: <b>{sent}</b>\n"
        f"├ Заблокировали бота: <b>{blocked}</b>\n"
        f"└ Не доставлено: <b>{failed}</b>"
    )
    await show_home(call.message, conn)
    log.info("Рассылка: %s отправлено, %s заблокировано, %s ошибок", sent, blocked, failed)
