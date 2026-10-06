"""Повторное нажатие кнопки не должно выглядеть поломкой.

Telegram отвечает ошибкой, когда экран перерисовывают тем же самым
содержимым. Само по себе это не беда — показывать нечего, потому что
ничего и не изменилось. Беда в том, что обработчик на ней падает и не
успевает ответить на нажатие: у клиента остаются крутиться часики, и он
видит ровно то же, что при сломанной кнопке.

Нажимают повторно постоянно: экран не сменился — человек жмёт ещё раз.

Гасим только эту ошибку и только её: всё остальное должно всплывать
наверх и попадать в журнал, иначе настоящие поломки станут невидимыми.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)

#: Текст ошибки Telegram, когда новое содержимое совпало со старым.
SAME = "message is not modified"


class SameScreenGuard(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as exc:
            if SAME not in str(exc):
                raise
            log.debug("Экран не изменился, нажатие гасим тихо")
            # Снять часики. Спрашиваем про метод, а не про тип: проверка
            # через isinstance делает эту ветку непроверяемой тестом —
            # именно так у нас однажды и спряталась мёртвая кнопка.
            reply = getattr(event, "answer", None)
            if callable(reply):
                # Если ответить не вышло — не страшно: нажатие уже
                # ничего не меняет, а ронять из-за этого нельзя.
                with suppress(TelegramAPIError):
                    await reply()
            return None
