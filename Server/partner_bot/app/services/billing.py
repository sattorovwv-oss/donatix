"""Счёт владельцу на оплату заказа MyStars.

Каждый заказ MyStars оплачивается отдельным переводом. Бот не подписывает
транзакции — он присылает владельцу готовую ссылку Tonkeeper с уже
подставленными адресом, суммой и memo. Один тап, и никакая сид-фраза
не хранится ни у сервиса, ни на сервере.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings

log = logging.getLogger(__name__)

INVOICE = (
    "💸 <b>Оплатите заказ MyStars</b>\n\n"
    "├ Сумма: <b>{amount} {currency}</b>;\n"
    "├ Memo: <code>{memo}</code>;\n"
    "└ Заказ: <code>{order_id}</code>.\n\n"
    "Адрес:\n<code>{address}</code>\n\n"
    "⚠️ Переводите <b>ровно эту сумму</b> и обязательно с memo — "
    "иначе MyStars вернёт перевод и заказ не выполнится.\n\n"
    "{deadline}"
    "Как оплатите — товар уедет получателю сам, покупателю придёт "
    "уведомление."
)


def invoice_keyboard(links) -> "InlineKeyboardBuilder":
    kb = InlineKeyboardBuilder()
    if links.tonkeeper:
        kb.row(InlineKeyboardButton(text="💎 Оплатить в Tonkeeper",
                                    url=links.tonkeeper, style="success"))
    return kb.as_markup() if links.tonkeeper else None


def make_sender(bot: Bot):
    """Вернуть корутину, которую ManualPayer зовёт для показа счёта."""

    async def send(links) -> None:
        deadline = (
            f"⏳ Оплатить до: <b>{links.expires_at}</b>\n\n"
            if links.expires_at else ""
        )
        text = INVOICE.format(
            amount=links.amount, currency=links.currency.upper(),
            memo=links.memo, order_id=links.order_id,
            address=links.address, deadline=deadline,
        )
        markup = invoice_keyboard(links)

        from app.services import access

        targets = list(access.admins())
        if settings.orders_chat_id:
            targets.append(settings.orders_chat_id)

        delivered = 0
        for chat_id in targets:
            try:
                await bot.send_message(chat_id, text, reply_markup=markup)
                delivered += 1
            except TelegramAPIError as exc:
                log.warning("Счёт по заказу %s не ушёл в чат %s: %s",
                            links.order_id, chat_id, exc)
        if not delivered:
            # Некому оплатить — заказ так и повиснет, лучше сказать сразу.
            from app.services.fragment import DeliveryError

            raise DeliveryError(
                "Счёт на оплату некому отправить: проверьте ADMIN_IDS "
                "и что вы писали боту /start."
            )

    return send
