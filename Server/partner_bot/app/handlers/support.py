"""Техподдержка: тикеты и переписка с админом."""
from __future__ import annotations

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app import runtime
from app.config import settings
from app.money import fmt
from app.services.delivery import notify_admins
from app.states import Support

router = Router(name="support")

def read_notice() -> str:
    """Объявление в разделе поддержки. Меняется из админ-панели."""
    return runtime.get("support_notice") or texts.SUPPORT_NOTICE_DEFAULT


@router.callback_query(F.data == "m:support")
async def cb_support(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    open_count = await db.count_open_tickets(conn, call.from_user.id)
    await call.message.edit_text(
        texts.SUPPORT.format(open_tickets=open_count, notice=read_notice()),
        reply_markup=keyboards.support_menu(has_open=open_count > 0),
    )
    await call.answer()


@router.callback_query(F.data == "t:new")
async def cb_new_ticket(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    if await db.count_open_tickets(conn, call.from_user.id) > 0:
        await call.answer(texts.TICKET_LIMIT, show_alert=True)
        return
    await state.set_state(Support.subject)
    await call.message.edit_text(
        texts.TICKET_ASK_SUBJECT, reply_markup=keyboards.back("m:support", "‹ Назад")
    )
    await call.answer()


@router.message(Support.subject, F.text)
async def on_subject(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    text = (message.text or "").strip()
    if len(text) < 5:
        await message.answer("❗️ Мушкилотро муфассалтар нависед — камаш 5 аломат.")
        return

    ticket = await db.create_ticket(conn, message.from_user.id, text[:500])
    await db.add_ticket_message(
        conn, ticket.id, message.from_user.id, is_admin=False, text=text
    )
    await state.clear()
    await message.answer(
        texts.TICKET_CREATED.format(ticket_id=ticket.id), reply_markup=keyboards.back()
    )

    user = await db.get_user(conn, message.from_user.id)
    buyer = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or "без имени"
    )
    await notify_admins(bot, texts.ADMIN_NEW_TICKET.format(
        ticket_id=ticket.id, buyer=buyer, user_id=message.from_user.id,
        balance=fmt(user.balance if user else 0), subject=text[:1000],
    ))


@router.callback_query(F.data == "t:reply")
async def cb_reply(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    tickets = await db.list_tickets(
        conn, user_id=call.from_user.id, status=db.TICKET_OPEN, limit=1
    )
    if not tickets:
        await call.answer("Муроҷиати кушода нест.", show_alert=True)
        return
    await state.set_state(Support.reply)
    await state.update_data(ticket_id=tickets[0].id)
    await call.message.edit_text(
        texts.TICKET_ASK_REPLY.format(ticket_id=tickets[0].id),
        reply_markup=keyboards.back("m:support", "‹ Назад"),
    )
    await call.answer()


@router.message(Support.reply, F.text)
async def on_reply(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    ticket = await db.get_ticket(conn, ticket_id) if ticket_id else None
    if ticket is None or ticket.user_id != message.from_user.id:
        await state.clear()
        await message.answer("Муроҷиат ёфт нашуд.")
        return

    text = (message.text or "").strip()
    await db.add_ticket_message(conn, ticket.id, message.from_user.id, is_admin=False, text=text)
    await state.clear()
    await message.answer(
        texts.TICKET_USER_REPLY_SENT.format(ticket_id=ticket.id),
        reply_markup=keyboards.back(),
    )

    buyer = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or "без имени"
    )
    await notify_admins(bot, texts.ADMIN_TICKET_REPLY.format(
        ticket_id=ticket.id, buyer=buyer, user_id=message.from_user.id, text=text[:1000],
    ))
