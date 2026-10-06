"""Первое сообщение владельцу: «бот активирован».

Бот ставится по трём вещам — токен, ID админа и ключ поставщика. Всё
остальное (реквизиты, цены, поддержка) задаётся уже в /panel. Чтобы
владелец не гадал, что делать дальше, при первом запуске бот сам пишет
ему, что работает и чего ещё не хватает.

Шлём один раз: служба перезапускается сама после падений, и напоминание
на каждый перезапуск превратилось бы в спам.
"""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError

from app import runtime

log = logging.getLogger(__name__)

FLAG = "activated_notice"


def todo() -> list[str]:
    """Чего не хватает для полной работы. Пусто — всё готово."""
    from app.services.fragment import DELIVERY_MODES, mode_now

    items = []
    if not runtime.get("pay_card_number"):
        items.append("💳 Реквизиты карты — /panel → 💳 Реквизиты\n"
                     "   (пока их нет, пополнение картой у клиентов выключено)")
    if mode_now() not in DELIVERY_MODES:
        items.append("🔌 Ключ поставщика — без него звёзды и игры не выдаются")
    from app import texts
    if not texts.support_username():
        items.append("📞 Ваш контакт для покупателей — /panel → 📞 Мой контакт")
    return items


def text(username: str) -> str:
    lines = [f"✅ <b>Бот @{username} активирован и работает.</b>", ""]
    left = todo()
    if left:
        lines.append("Осталось задать:")
        lines.extend(f"• {item}" for item in left)
    else:
        lines.append("Всё настроено — можно продавать.")
    lines += [
        "",
        "Цены уже стоят по умолчанию, поменять: /panel → 💵 Цены и наценка.",
        "Проверить поставщика: /panel → 🔌 Проверить связь.",
    ]
    return "\n".join(lines)


async def greet_once(bot, conn, username: str, admins) -> int:
    """Написать админам один раз за жизнь базы. Возвращает, скольким дошло."""
    if runtime.get(FLAG):
        return 0
    body = text(username)
    sent = 0
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, body)
            sent += 1
        except TelegramAPIError as exc:
            # Чаще всего админ ещё не нажал /start у бота. Флаг тогда не
            # ставим: напишем при следующем запуске.
            log.warning("Не смог написать админу %s: %s", admin_id, exc)
    if sent:
        await runtime.set_value(conn, FLAG, "1")
    return sent
