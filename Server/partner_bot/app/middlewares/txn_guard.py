"""Предохранитель: транзакция не должна пережить обработчик.

Первая запись в базу открывает транзакцию и берёт замок. Если обработчик
после неё вернулся или упал раньше commit, замок висит на общем
соединении до следующего commit — а он может случиться через минуты.
Всё это время API, юзербот и фоновые задачи ждут свои пятнадцать секунд
и получают «database is locked».

Такие места чинятся по одному, но заводиться будут снова: любая новая
функция с записью и ранним выходом — та же дыра. Поэтому проверка стоит
снаружи всех обработчиков: на выходе смотрим, не осталась ли транзакция,
снимаем её и пишем в журнал, откуда взялась.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app import db

log = logging.getLogger(__name__)


class TxnGuard(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        conn = data.get("conn")
        try:
            return await handler(event, data)
        finally:
            if conn is not None and await db.release(conn):
                log.warning(
                    "Обработчик оставил открытую транзакцию — снята. "
                    "Событие: %s", _what(event),
                )


def _what(event: TelegramObject) -> str:
    """Чем нажали: кнопка или текст, чтобы найти виновника в коде."""
    press = getattr(event, "data", None)
    if isinstance(press, str):
        return f"кнопка {press!r}"
    text = getattr(event, "text", None)
    return f"текст {text[:40]!r}" if isinstance(text, str) else type(event).__name__
