"""Кто пускается в бота.

Проверка собрана в middleware, а не расставлена по обработчикам: забыть вызов
в новом обработчике — значит открыть дыру, а middleware пропустить нельзя.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from . import db

log = logging.getLogger(__name__)

NO_ACCESS = (
    "У тебя нет доступа к этому боту.\n\n"
    "Твой Telegram id: {user_id}\n"
    "Передай его владельцу — он откроет доступ."
)
BLOCKED = "Доступ закрыт. Если это ошибка — напиши владельцу бота."

# Чтобы чужой сканер не завалил владельца уведомлениями об одном и том же id.
NOTIFY_EVERY = timedelta(days=1)


def bootstrap_admins(conn: sqlite3.Connection, user_ids: frozenset[int]) -> None:
    """Завести владельцев из .env как админов.

    Вызывается на старте. При переходе со старой однопользовательской версии
    у владельца уже есть записи — этот вызов сохраняет ему доступ к ним.
    """
    for user_id in user_ids:
        db.ensure_admin(conn, user_id)
    if user_ids:
        log.info("Админы: %s", sorted(user_ids))


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "admin" and user.get("status") != "blocked")


def is_active(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("status") == "active")


def _sender(event: TelegramObject) -> User | None:
    return getattr(event, "from_user", None)


def _is_start_command(event: TelegramObject) -> bool:
    text = getattr(event, "text", None) or ""
    return text.split()[0].split("@")[0] == "/start" if text.strip() else False


def _is_plain_text(event: TelegramObject) -> bool:
    """Обычное текстовое сообщение — то, чем человек может назвать своё имя."""
    if not isinstance(event, Message):
        return False
    text = (event.text or "").strip()
    return bool(text) and not text.startswith("/")


class AccessMiddleware(BaseMiddleware):
    """Пускает только заведённых людей и кладёт их запись в данные обработчика."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # id незнакомца → когда владельцу о нём сообщали в последний раз
        self._notified: dict[int, datetime] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender = _sender(event)
        if sender is None or sender.is_bot:
            return None

        user = db.get_user(self.conn, sender.id)

        if user is None:
            await self._refuse_stranger(event, sender)
            return None

        if user["status"] == "blocked":
            await self._say(event, BLOCKED)
            return None

        # Пока человек не представился, к Claude его пускать нельзя: первое же
        # голосовое ушло бы в модель мимо регистрации и стоило бы денег.
        if user["status"] == "invited" and not _is_start_command(event):
            await self._say(event, "Нажми /start, чтобы начать.")
            return None

        if user["status"] == "awaiting_name" and not _is_plain_text(event) \
                and not _is_start_command(event):
            await self._say(event, "Сначала напиши, как тебя зовут.")
            return None

        if user["status"] == "active":
            db.touch_last_seen(self.conn, sender.id)
            # У владельца, пришедшего со старой версии, имени ещё нет —
            # берём его из Телеграма молча, чтобы не дёргать вопросами.
            if not user.get("name"):
                name = getattr(sender, "first_name", None) or (
                    f"@{sender.username}" if sender.username else None)
                if name:
                    db.rename_user(self.conn, sender.id, name)
                    user = db.get_user(self.conn, sender.id)

        data["user"] = user
        data["sender"] = sender
        return await handler(event, data)

    # ── вспомогательное ────────────────────────────────────────────────────

    @staticmethod
    async def _say(event: TelegramObject, text: str) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text[:200], show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
        except Exception:
            log.exception("Не удалось ответить на закрытый доступ")

    async def _refuse_stranger(self, event: TelegramObject, sender: User) -> None:
        await self._say(event, NO_ACCESS.format(user_id=sender.id))
        await self._notify_admins(event, sender)

    async def _notify_admins(self, event: TelegramObject, sender: User) -> None:
        """Сообщить владельцу про незнакомца — с кнопкой, чтобы не набирать id руками."""
        now = datetime.now(timezone.utc)
        last = self._notified.get(sender.id)
        if last and now - last < NOTIFY_EVERY:
            return
        self._notified[sender.id] = now

        from .keyboards import grant_access_keyboard

        name = " ".join(x for x in (sender.first_name, sender.last_name) if x) or "без имени"
        handle = f" (@{sender.username})" if sender.username else ""
        text = (
            f"К боту постучался незнакомый человек:\n\n"
            f"{name}{handle}\n"
            f"id: {sender.id}\n\n"
            f"Дать ему доступ?"
        )
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        for admin in db.list_users(self.conn):
            if admin["role"] != "admin" or admin["status"] == "blocked":
                continue
            try:
                await bot.send_message(
                    admin["id"], text, reply_markup=grant_access_keyboard(sender.id)
                )
            except Exception:
                log.warning("Не смог уведомить админа %s", admin["id"])
