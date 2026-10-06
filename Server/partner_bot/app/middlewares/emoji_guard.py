"""Страховка на случай, когда Telegram перестал принимать премиум-эмодзи.

Право на них зависит от подписки владельца бота. Если Premium кончится,
Telegram начнёт отвергать каждое сообщение с <tg-emoji> — бот замолчит
целиком, и владелец узнает об этом от покупателей.

Мидлварь ловит такой отказ, убирает премиум-эмодзи из сообщения, повторяет
отправку и выключает их до следующей проверки в панели.
"""
from __future__ import annotations

import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger(__name__)

TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)

# По этим словам узнаём отказ именно из-за премиум-эмодзи.
MARKERS = ("custom_emoji", "custom emoji", "emoji-id", "emoji_id")

FIELDS = ("text", "caption")


def strip_custom(text: str) -> str:
    """Убрать теги премиум-эмодзи, оставив запасные значки."""
    return TG_EMOJI_RE.sub(r"\1", text)


class CustomEmojiGuard(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if not any(marker in message for marker in MARKERS):
                raise

            changed = False
            for field in FIELDS:
                value = getattr(method, field, None)
                if isinstance(value, str) and "<tg-emoji" in value:
                    object.__setattr__(method, field, strip_custom(value))
                    changed = True

            # Премиум-значок бывает и на кнопках — его тоже надо снять,
            # иначе повтор упрётся в тот же отказ.
            markup = getattr(method, "reply_markup", None)
            rows = getattr(markup, "inline_keyboard", None)
            for row in rows or []:
                for button in row:
                    if getattr(button, "icon_custom_emoji_id", None):
                        object.__setattr__(button, "icon_custom_emoji_id", None)
                        changed = True

            if not changed:
                raise

            log.error(
                "Telegram отверг премиум-эмодзи (%s). Выключаю их и повторяю "
                "отправку обычными значками.", exc,
            )
            await self._disable()
            return await make_request(bot, method)

    @staticmethod
    async def _disable() -> None:
        from app import db, runtime

        if not runtime.get_bool("custom_emoji_on"):
            return
        try:
            conn = await db.connect()
            try:
                await runtime.set_value(conn, "custom_emoji_on", "0")
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — база не должна ломать отправку
            log.warning("Не смог выключить премиум-эмодзи в базе: %s", exc)
            runtime._cache["custom_emoji_on"] = "0"
