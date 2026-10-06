"""Регистрация пользователя (в т.ч. по реферальной ссылке) и бан-лист."""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app import db, links, texts
from app.config import settings
from app.services import access

REF_RE = re.compile(r"^ref(\d+)$")


class UserGuardMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        conn: aiosqlite.Connection | None = data.get("conn")
        if user is None or conn is None:
            return await handler(event, data)

        # `/start ref42` — пришёл по приглашению, `/start instagram` — по рекламе.
        payload = links.payload_of(event.text) if isinstance(event, Message) else ""
        match = REF_RE.match(payload)
        referrer_id = int(match.group(1)) if match else None

        is_new = await db.upsert_user(
            conn, user.id, user.username, user.first_name, referrer_id
        )
        if payload and not match:
            await db.record_link_hit(conn, payload, user.id, is_new)

        # Админа не банит собственный бан-лист — иначе можно потерять доступ.
        if not access.is_admin(user.id) and await _banned(conn, user.id):
            if isinstance(event, Message):
                await event.answer(texts.BANNED)
            elif isinstance(event, CallbackQuery):
                await event.answer(texts.BANNED, show_alert=True)
            return None

        return await handler(event, data)


async def _banned(conn: aiosqlite.Connection, user_id: int) -> bool:
    record = await db.get_user(conn, user_id)
    return bool(record and record.is_banned)
