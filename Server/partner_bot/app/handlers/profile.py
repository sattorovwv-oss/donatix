"""Профиль, история покупок, промокоды, реферальная система."""
from __future__ import annotations

from datetime import datetime

import aiosqlite
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app import runtime
from app.emoji import em
from app.config import settings
from app.money import fmt
from app.states import Promo

router = Router(name="profile")


def _human_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


@router.callback_query(F.data == "m:profile")
async def cb_profile(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    user = await db.get_user(conn, call.from_user.id)
    if user is None:
        await call.answer("Профил ёфт нашуд, /start-ро пахш кунед", show_alert=True)
        return

    stats = await db.user_order_stats(conn, user.id)
    username = f"@{user.username}" if user.username else "не установлен"
    await call.message.edit_text(
        texts.PROFILE.format(
            user_id=user.id,
            username=username,
            balance=fmt(user.balance),
            total_deposit=fmt(user.total_deposit),
            total=stats["total"],
            done=stats["done"],
            active=stats["active"],
            premium=stats["premium"],
            premium_spent=fmt(stats["premium_spent"]),
            stars=stats["stars"],
            stars_spent=fmt(stats["stars_spent"]),
            created=_human_date(user.created_at),
        ),
        reply_markup=keyboards.profile(),
    )
    await call.answer()


@router.callback_query(F.data == "p:history")
async def cb_history(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    orders = await db.list_orders(conn, user_id=call.from_user.id, limit=10)
    back = keyboards.back("m:profile", f"{em('back')} Бозгашт")

    if not orders:
        await call.message.edit_text(texts.HISTORY_EMPTY, reply_markup=back)
        await call.answer()
        return

    all_orders = await db.list_orders(conn, user_id=call.from_user.id, limit=500)
    done = sum(1 for o in all_orders if o.status == db.ORDER_DELIVERED)
    refunded = sum(1 for o in all_orders if o.is_refunded)
    spent = sum(o.price for o in all_orders if o.status == db.ORDER_DELIVERED)

    summary = texts.HISTORY_SUMMARY.format(
        done=done, refunded=refunded, spent=fmt(spent)
    )

    lines = []
    for order in orders:
        # Возврат показываем отдельной строкой: человеку важно видеть,
        # что деньги за неудачный заказ вернулись, а не пропали.
        tail = (
            f"\n└ {em('money')} <b>{fmt(order.price)}</b> ба баланс баргашт"
            if order.is_refunded else
            f"\n└ Гирифта шуд: <b>{fmt(order.price)}</b>"
        )
        lines.append(
            f"<b>№{order.id}</b> · {order.status_title}\n"
            f"├ {order.title} → <code>@{order.recipient}</code>\n"
            f"├ {_human_date(order.created_at)}"
            + tail
        )

    await call.message.edit_text(
        texts.HISTORY.format(summary=summary, items="\n\n".join(lines)),
        reply_markup=back,
    )
    await call.answer()


@router.callback_query(F.data == "p:ref")
async def cb_referral(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    user = await db.get_user(conn, call.from_user.id)
    if user is None:
        await call.answer("/start-ро пахш кунед", show_alert=True)
        return

    bot_username = settings.bot_username or (await call.bot.me()).username
    await call.message.edit_text(
        texts.REFERRAL.format(
            percent=runtime.referral_percent(),
            ref_count=user.ref_count,
            ref_earned=fmt(user.ref_earned),
            link=f"https://t.me/{bot_username}?start=ref{user.id}",
        ),
        reply_markup=keyboards.back("m:profile", "‹ Назад"),
    )
    await call.answer()


@router.callback_query(F.data == "p:promo")
async def cb_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Promo.code)
    await call.message.edit_text(
        texts.PROMO_ASK, reply_markup=keyboards.back("m:profile", "‹ Назад")
    )
    await call.answer()


@router.message(Promo.code, F.text)
async def on_promo(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    result = await db.redeem_promo(conn, message.text or "", message.from_user.id)
    if isinstance(result, str):
        await message.answer(texts.PROMO_ERRORS.get(result, "❌ Промокод не принят."))
        return

    await state.clear()
    user = await db.get_user(conn, message.from_user.id)
    await message.answer(
        texts.PROMO_OK.format(amount=fmt(result), balance=fmt(user.balance if user else 0)),
        reply_markup=keyboards.back(),
    )
