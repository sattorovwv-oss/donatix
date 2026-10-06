"""Команда важнее незакрытого диалога.

Панель часто ждёт от админа текст: сумму, дату, юзернейм клиента. Пока этот
шаг не закрыт, его обработчик забирает себе любое сообщение — включая /start.
Это внешняя мидлварь: она отрабатывает до фильтров, поэтому успевает сбросить
состояние, и команда доходит до своего обработчика.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)")

# Из рассылки выходим только по этим командам: остальной текст, начинающийся
# со слэша, может быть частью самого поста.
NAV_COMMANDS = {"start", "menu", "panel", "admin", "cancel"}
CAST_STATES = {"Cast:content", "Cast:buttons"}


def escapes(current_state: str, command: str) -> bool:
    """Вырывает ли команда из этого шага."""
    if current_state in CAST_STATES:
        return command in NAV_COMMANDS
    return True


class CommandEscapeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if isinstance(event, Message) and state is not None:
            match = COMMAND_RE.match(event.text or "")
            if match:
                current = await state.get_state()
                if current and escapes(current, match.group(1).lower()):
                    await state.clear()
                    # raw_state диспетчер кладёт в data ещё до нас, а StateFilter
                    # смотрит именно на него — иначе шаг всё равно перехватит.
                    data["raw_state"] = None
        return await handler(event, data)
