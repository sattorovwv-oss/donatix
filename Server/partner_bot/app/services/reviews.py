"""Отзывы: спросить у клиента, показать владельцу, опубликовать в канале.

Отзыв не попадает в канал сразу: сначала он приходит владельцу с кнопками
«Опубликовать» и «Удалить». Так в канал не уедет ни ругань, ни реклама
чужого бота.

Один завершённый заказ = один отзыв. Держится это на UNIQUE(order_id) в
базе, а не на проверке в коде: две кнопки, нажатые подряд, успели бы
проскочить обе.
"""
from __future__ import annotations

import logging

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.utils.keyboard import InlineKeyboardBuilder

import asyncio

from app import db, runtime, texts
from app.config import settings
from app.keyboards import DANGER, SUCCESS, btn

log = logging.getLogger(__name__)


def enabled() -> bool:
    return runtime.get_bool("reviews_on")


def channel() -> str:
    """Куда публиковать: @канал или числовой id."""
    return (runtime.get("reviews_channel") or "").strip()


def channel_target() -> str | int | None:
    target = channel()
    if not target:
        return None
    if target.lstrip("-").isdigit():
        return int(target)
    return target if target.startswith("@") else f"@{target.lstrip('@')}"


def author_of(user: db.User | None) -> str:
    """Как подписать отзыв в канале. Юзернейм лучше — по нему видно живого
    человека, а не безымянную строчку."""
    if user is None:
        return "клиент"
    if user.username:
        return f"@{user.username}"
    return user.first_name or "клиент"


def username_of(user: db.User | None) -> str:
    """Никнейм для карточки проверки. Прямо говорим, когда его нет: у части
    аккаунтов юзернейма нет вовсе, и это не ошибка бота."""
    if user is not None and user.username:
        return f"@{user.username}"
    return "<i>не установлен</i>"


def rate_kb(order_id: int) -> "InlineKeyboardBuilder":
    kb = InlineKeyboardBuilder()
    kb.row(*[
        btn("⭐️" * n if n <= 3 else f"⭐️{n}", f"rv:rate:{order_id}:{n}")
        for n in range(1, 6)
    ])
    kb.row(btn("Не сейчас", "rv:skip"))
    return kb.as_markup()


def moderate_kb(review_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("✅ Опубликовать", f"rv:ok:{review_id}", style=SUCCESS),
        btn("🗑 Удалить", f"rv:no:{review_id}", style=DANGER),
    )
    return kb.as_markup()


async def offer(bot: Bot, conn: aiosqlite.Connection, order: db.Order) -> bool:
    """Предложить клиенту оценить выполненный заказ."""
    if not enabled():
        return False
    if await db.review_of_order(conn, order.id) is not None:
        return False
    try:
        await bot.send_message(
            order.user_id,
            texts.REVIEW_ASK.format(order_id=order.id),
            reply_markup=rate_kb(order.id),
        )
    except TelegramAPIError as exc:
        log.info("Не смог предложить отзыв по заказу %s: %s", order.id, exc)
        return False
    return True


SEND_DELAY = 0.05        # ~20 сообщений в секунду — предел Telegram


async def ask_past_buyers(
    bot: Bot, conn: aiosqlite.Connection, limit: int = 500,
) -> dict[str, int]:
    """Попросить отзыв у тех, кто покупал раньше.

    Отметку «спрашивали» ставим только на реально отправленное сообщение:
    кто не получил из-за блокировки бота, останется в списке на потом.
    """
    targets = await db.review_targets(conn, limit)
    report = {"total": len(targets), "sent": 0, "blocked": 0, "failed": 0}

    for order in targets:
        try:
            await bot.send_message(
                order.user_id,
                texts.REVIEW_ASK_OLD.format(order_id=order.id, title=order.title),
                reply_markup=rate_kb(order.id),
            )
        except TelegramForbiddenError:
            report["blocked"] += 1
            await db.mark_review_asked(conn, order.id, order.user_id)
            continue
        except TelegramAPIError as exc:
            report["failed"] += 1
            log.debug("Просьба об отзыве не дошла до %s: %s", order.user_id, exc)
            continue

        report["sent"] += 1
        await db.mark_review_asked(conn, order.id, order.user_id)
        await asyncio.sleep(SEND_DELAY)

    return report


async def to_moderation(bot: Bot, conn: aiosqlite.Connection, review: db.Review) -> None:
    """Отправить отзыв владельцу на проверку."""
    order = await db.get_order(conn, review.order_id)
    user = await db.get_user(conn, review.user_id)
    text = texts.ADMIN_REVIEW.format(
        stars=review.stars, rating=review.rating,
        order_id=review.order_id,
        title=order.title if order else "—",
        username=username_of(user),
        name=(user.first_name if user and user.first_name else "—"),
        author=author_of(user), user_id=review.user_id,
        text=review.text or "<i>без текста</i>",
    )
    from app.services import access

    for admin_id in access.admins():
        try:
            await bot.send_message(admin_id, text, reply_markup=moderate_kb(review.id))
        except TelegramAPIError as exc:
            log.warning("Отзыв %s не дошёл админу %s: %s", review.id, admin_id, exc)


async def publish(bot: Bot, conn: aiosqlite.Connection, review: db.Review) -> str | int:
    """Опубликовать отзыв в канале. Строка — причина, по которой не вышло."""
    target = channel_target()
    if target is None:
        return "не задан канал отзывов"

    order = await db.get_order(conn, review.order_id)
    user = await db.get_user(conn, review.user_id)
    template = texts.REVIEW_POST if review.text else texts.REVIEW_POST_SHORT
    body = template.format(
        stars=review.stars,
        text=review.text or "",
        title=order.title if order else "—",
        author=author_of(user),
    )
    try:
        message = await bot.send_message(target, body)
    except TelegramAPIError as exc:
        log.warning("Отзыв %s не ушёл в канал: %s", review.id, exc)
        return str(exc)
    return message.message_id
