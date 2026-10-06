"""Клиент ставит оценку и пишет отзыв; владелец решает, публиковать ли."""
from __future__ import annotations

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import db, keyboards, texts
from app.config import settings
from app.services import access
from app.keyboards import btn
from app.services import reviews
from app.states import Review

router = Router(name="reviews")

MAX_LENGTH = 1000


@router.callback_query(F.data == "rv:skip")
async def cb_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "Хорошо, в другой раз 🙂", reply_markup=keyboards.back()
    )
    await call.answer()


@router.callback_query(F.data.startswith("rv:rate:"))
async def cb_rate(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    _, _, raw_order, raw_rating = call.data.split(":")
    order_id, rating = int(raw_order), int(raw_rating)

    order = await db.get_order(conn, order_id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Этот заказ не ваш.", show_alert=True)
        return
    if order.status != db.ORDER_DELIVERED:
        await call.answer("Отзыв можно оставить только по выполненному заказу.",
                          show_alert=True)
        return

    review = await db.create_review(
        conn, order_id=order_id, user_id=call.from_user.id, rating=rating,
    )
    if review is None:
        await state.clear()
        await call.message.edit_text(texts.REVIEW_ALREADY, reply_markup=keyboards.back())
        await call.answer()
        return

    await state.set_state(Review.text)
    await state.update_data(review_id=review.id)

    kb = InlineKeyboardBuilder()
    kb.row(btn("Отправить без текста", f"rv:send:{review.id}"))
    await call.message.edit_text(
        texts.REVIEW_ASK_TEXT.format(stars=review.stars), reply_markup=kb.as_markup()
    )
    await call.answer()


@router.message(Review.text, F.text)
async def on_review_text(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    data = await state.get_data()
    review = await db.get_review(conn, data.get("review_id", 0))
    if review is None or review.user_id != message.from_user.id:
        await state.clear()
        await message.answer("Не понял, к какому заказу отзыв. Откройте меню: /menu")
        return

    text = (message.text or "").strip()
    if len(text) > MAX_LENGTH:
        await message.answer(
            f"❌ Слишком длинно: {len(text)} символов, а можно до {MAX_LENGTH}."
        )
        return

    await db.set_review_text(conn, review.id, text)
    await state.clear()
    await _finish(bot, conn, message, review.id)


@router.callback_query(F.data.startswith("rv:send:"))
async def cb_send_without_text(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    review = await db.get_review(conn, int(call.data.rsplit(":", 1)[1]))
    if review is None or review.user_id != call.from_user.id:
        await call.answer("Отзыв не найден.", show_alert=True)
        return
    await state.clear()
    await _finish(bot, conn, call.message, review.id)
    await call.answer()


async def _finish(bot: Bot, conn: aiosqlite.Connection, target: Message, review_id: int) -> None:
    review = await db.get_review(conn, review_id)
    if review is not None:
        await reviews.to_moderation(bot, conn, review)
    await target.answer(texts.REVIEW_SENT, reply_markup=keyboards.back())


# ------------------------------------------------------------- модерация


@router.callback_query(F.data.startswith("rv:ok:"))
async def cb_publish(call: CallbackQuery, conn: aiosqlite.Connection, bot: Bot) -> None:
    if not access.is_admin(call.from_user.id):
        await call.answer("Это кнопка владельца.", show_alert=True)
        return

    review = await db.get_review(conn, int(call.data.rsplit(":", 1)[1]))
    if review is None:
        await call.answer("Отзыв не найден.", show_alert=True)
        return
    if review.status != db.REVIEW_PENDING:
        await call.answer("Этот отзыв уже разобран.", show_alert=True)
        return

    result = await reviews.publish(bot, conn, review)
    if isinstance(result, str):
        await call.answer("Не отправилось", show_alert=True)
        await call.message.answer(
            "❌ <b>Отзыв не ушёл в канал</b>\n\n"
            f"<blockquote expandable>{result}</blockquote>\n\n"
            "<blockquote>Проверьте: канал указан в панели, бот добавлен "
            "в него администратором с правом писать.</blockquote>"
        )
        return

    # Статус меняем после отправки: если Telegram отказал, отзыв остаётся
    # на модерации и кнопку можно нажать снова.
    await db.moderate_review(conn, review.id, db.REVIEW_PUBLISHED, channel_msg=result)
    await call.answer("Опубликован")
    await _mark(call, "✅ <b>Опубликован</b>")


@router.callback_query(F.data.startswith("rv:no:"))
async def cb_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    if not access.is_admin(call.from_user.id):
        await call.answer("Это кнопка владельца.", show_alert=True)
        return

    review = await db.get_review(conn, int(call.data.rsplit(":", 1)[1]))
    if review is None:
        await call.answer("Отзыв не найден.", show_alert=True)
        return
    if not await db.moderate_review(conn, review.id, db.REVIEW_DELETED):
        await call.answer("Этот отзыв уже разобран.", show_alert=True)
        return

    await call.answer("Удалён")
    await _mark(call, "🗑 <b>Удалён</b>")


async def _mark(call: CallbackQuery, verdict: str) -> None:
    """Дописать решение к сообщению модерации и убрать кнопки."""
    body = call.message.html_text if call.message.text else ""
    await call.message.edit_text(f"{body}\n\n{verdict}", reply_markup=None)
