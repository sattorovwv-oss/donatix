"""Кнопка на старом сообщении не должна выглядеть поломкой.

Телеграм хранит сообщения для правки двое суток. Дальше он всё ещё
доставляет нажатие, но вместо самого сообщения присылает заглушку: у неё
нет метода правки, и любой обработчик, который перерисовывает экран,
падает на ней с AttributeError — ещё до того, как ответит на нажатие. У
клиента остаются крутиться часики.

Снаружи это выглядит как «некоторые кнопки не работают»: свежие экраны
слушаются, вчерашние молчат. Ни ошибки, ни объяснения.

Починить в обработчиках нельзя: правка экрана раскидана по семи десяткам
мест, и каждое новое добавит ту же дыру. Поэтому проверка стоит здесь,
перед всеми.

Заглушка умеет отвечать новым сообщением — этим и пользуемся: вместо
мёртвого нажатия человек получает свежее меню, с которым можно работать.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)


def editable(screen: object) -> bool:
    """Можно ли перерисовать этот экран.

    Спрашиваем про умение, а не про тип: проверка через isinstance
    делает эту ветку непроверяемой тестом, а ровно за такой проверкой у
    нас уже пряталась мёртвая кнопка.
    """
    return callable(getattr(screen, "edit_text", None))


class StaleScreenGuard(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        screen = getattr(event, "message", None)
        if editable(screen):
            return await handler(event, data)

        log.info("Нажатие на устаревший экран: %s", getattr(event, "data", "—"))
        await self._replace(event, screen, data)
        return None

    @staticmethod
    async def _replace(event: TelegramObject, screen: object, data: dict) -> None:
        """Снять часики и выдать свежее меню вместо мёртвого экрана."""
        from app import texts

        answer = getattr(event, "answer", None)
        if callable(answer):
            with suppress(TelegramAPIError):
                await answer(texts.SCREEN_OLD, show_alert=True)

        conn = data.get("conn")
        send = getattr(screen, "answer", None)
        if conn is None or not callable(send):
            return

        # Заглушка отвечает новым сообщением — человек получает рабочий
        # экран, а не совет «откройте меню сами».
        from app.handlers.menu import main_markup

        with suppress(TelegramAPIError):
            await send(texts.MENU.format(balance=await _balance(conn, event)),
                       reply_markup=await main_markup(conn))


async def _balance(conn, event) -> str:
    from app import db
    from app.money import fmt

    user = await db.get_user(conn, event.from_user.id)
    return fmt(user.balance if user else 0)
