"""Не подписан на канал спонсора — бот не обслуживает.

Стоит снаружи остальных: до подписки клиент не должен попадать ни в один
экран. Три исключения, без которых это была бы ловушка:

— владельцу и админам проверка не мешает: иначе можно потерять доступ к
  панели, задав канал с опечаткой, и починить настройку станет нечем;
— кнопка «я подписался» проходит всегда, иначе нажимать её бессмысленно;
— когда проверка не удалась (канал неверный, бота выгнали), клиент
  проходит: см. app/services/sponsor.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject

from app import keyboards, texts
from app.services import access, sponsor

log = logging.getLogger(__name__)

#: Нажатия, которые пропускаем без проверки.
FREE = ("sub:check",)


class SponsorGate(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot = data.get("bot")
        if user is None or bot is None or not sponsor.on():
            return await handler(event, data)
        if access.is_admin(user.id):
            return await handler(event, data)

        press = getattr(event, "data", "")
        if isinstance(press, str) and press in FREE:
            return await handler(event, data)

        left = await sponsor.missing(bot, user.id)
        if not left:
            return await handler(event, data)

        await self._ask(event, left)
        return None

    @staticmethod
    async def _ask(event: TelegramObject, left: list[str]) -> None:
        """Показать, куда подписаться. Экран один и тот же на сообщение и
        на нажатие — разница лишь в том, куда его положить."""
        markup = keyboards.sponsor_gate(left)
        answer = getattr(event, "answer", None)
        screen = getattr(event, "message", None)

        with suppress(TelegramAPIError):
            if screen is not None:                     # это было нажатие
                if callable(answer):
                    await answer()
                await screen.answer(texts.SPONSOR_ASK, reply_markup=markup)
            elif callable(answer):
                await answer(texts.SPONSOR_ASK, reply_markup=markup)
