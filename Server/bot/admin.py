"""Панель управления пользователями: /admin.

Всё внутри Телеграма — ни домена, ни пароля, ни открытого порта не нужно.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from . import db, keyboards as kb
from .access import is_admin

log = logging.getLogger(__name__)
router = Router(name="admin")

NOT_ADMIN = "Эта команда только для владельца бота."

# Кто из админов сейчас вводит id нового человека.
_awaiting_id: set[int] = set()


def is_awaiting_id(admin_id: int) -> bool:
    return admin_id in _awaiting_id


# ── тексты ─────────────────────────────────────────────────────────────────

def _ago(iso: str | None) -> str:
    if not iso:
        return "ни разу"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - moment).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


STATUS_WORD = {
    "active": "работает",
    "invited": "приглашён, ещё не заходил",
    "awaiting_name": "вводит имя",
    "blocked": "заблокирован",
}


def user_line(conn: sqlite3.Connection, user: dict[str, Any]) -> str:
    stats = db.user_stats(conn, user["id"])
    mark = kb.STATUS_MARK.get(user["status"], "•")
    name = user.get("name") or "без имени"
    parts = [f"{mark} {name}", f"id {user['id']}"]
    if user["role"] == "admin":
        parts.append("админ")
    if user["status"] == "active":
        parts.append(f"{stats['transactions']} записей")
        parts.append(f"был {_ago(user.get('last_seen_at'))}")
    else:
        parts.append(STATUS_WORD.get(user["status"], user["status"]))
    return " · ".join(parts)


def users_text(conn: sqlite3.Connection) -> str:
    users = db.list_users(conn)
    if not users:
        return "Пока никого нет."
    lines = [f"Пользователи ({len(users)})", ""]
    lines += [user_line(conn, u) for u in users]
    return "\n".join(lines)


def user_card_text(conn: sqlite3.Connection, user: dict[str, Any]) -> str:
    stats = db.user_stats(conn, user["id"])
    lines = [
        f"{kb.STATUS_MARK.get(user['status'], '•')} {user.get('name') or 'без имени'}",
        "",
        f"id: {user['id']}",
    ]
    if user.get("tg_username"):
        lines.append(f"ник: @{user['tg_username']}")
    lines += [
        f"роль: {'владелец' if user['role'] == 'admin' else 'пользователь'}",
        f"состояние: {STATUS_WORD.get(user['status'], user['status'])}",
        f"операций: {stats['transactions']}",
        f"накладных: {stats['documents']}",
        f"добавлен: {user['invited_at'][:10]}",
        f"последний раз: {_ago(user.get('last_seen_at'))}",
    ]
    return "\n".join(lines)


# ── показ ──────────────────────────────────────────────────────────────────

async def _show_list(target: Message | CallbackQuery, conn: sqlite3.Connection) -> None:
    text = users_text(conn)
    markup = kb.users_keyboard(db.list_users(conn))
    if isinstance(target, CallbackQuery):
        await _edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _edit(query: CallbackQuery, text: str, markup: Any) -> None:
    """Обновить сообщение панели на месте, а не плодить новые."""
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        # Телеграм отвергает правку, если текст не изменился — это не ошибка.
        await query.answer()


# ── команда ────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, conn: sqlite3.Connection, user: dict[str, Any]) -> None:
    if not is_admin(user):
        await message.answer(NOT_ADMIN)
        return
    _awaiting_id.discard(user["id"])
    await _show_list(message, conn)


# ── кнопки ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:"))
async def on_admin_button(
    query: CallbackQuery, conn: sqlite3.Connection, user: dict[str, Any]
) -> None:
    # Кнопки — отдельная поверхность: закрывать её надо своей проверкой,
    # иначе чужой может нажать кнопку из пересланного сообщения.
    if not is_admin(user):
        await query.answer(NOT_ADMIN, show_alert=True)
        return

    data = query.data

    if data == kb.ADMIN_LIST:
        _awaiting_id.discard(user["id"])
        await _show_list(query, conn)
        await query.answer()
        return

    if data == kb.ADMIN_ADD:
        _awaiting_id.add(user["id"])
        await query.answer()
        await query.message.answer(
            "Пришли Telegram id человека — просто числом.\n\n"
            "Если не знаешь его id: пусть он напишет боту, "
            "и я пришлю тебе кнопку «Дать доступ»."
        )
        return

    if data.startswith(kb.ADMIN_GRANT):
        target_id = int(data.removeprefix(kb.ADMIN_GRANT))
        added = db.invite_user(conn, target_id)
        await query.answer("Доступ открыт" if added else "Он уже был в списке")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.answer(
            f"Доступ для id {target_id} открыт. Скажи ему нажать Старт."
            if added else f"id {target_id} уже был в списке."
        )
        return

    target_id = int(data.rsplit(":", 1)[1])
    target = db.get_user(conn, target_id)
    if target is None:
        await query.answer("Такого человека уже нет", show_alert=True)
        await _show_list(query, conn)
        return

    is_self = target_id == user["id"]

    if data.startswith(kb.ADMIN_USER):
        await _edit(query, user_card_text(conn, target),
                    kb.user_card_keyboard(target, is_self=is_self))
        await query.answer()
        return

    if data.startswith(kb.ADMIN_BLOCK):
        if is_self:
            await query.answer("Себя заблокировать нельзя", show_alert=True)
            return
        if target["role"] == "admin" and db.count_admins(conn) <= 1:
            await query.answer("Это последний админ — без него бот станет неуправляемым",
                               show_alert=True)
            return
        db.set_status(conn, target_id, "blocked")
        await query.answer("Заблокирован")
        await _edit(query, user_card_text(conn, db.get_user(conn, target_id)),
                    kb.user_card_keyboard(db.get_user(conn, target_id), is_self=False))
        return

    if data.startswith(kb.ADMIN_UNBLOCK):
        # Кто уже представлялся — возвращается работать; кто нет — снова к регистрации.
        db.set_status(conn, target_id, "active" if target.get("name") else "invited")
        await query.answer("Доступ вернул")
        await _edit(query, user_card_text(conn, db.get_user(conn, target_id)),
                    kb.user_card_keyboard(db.get_user(conn, target_id), is_self=False))
        return

    if data.startswith(kb.ADMIN_WIPE_ASK):
        if is_self:
            await query.answer("Себя удалить нельзя", show_alert=True)
            return
        stats = db.user_stats(conn, target_id)
        await _edit(
            query,
            f"Удалить «{target.get('name') or target_id}» вместе со всеми данными?\n\n"
            f"Пропадут: {stats['transactions']} операций и {stats['documents']} накладных.\n"
            f"Вернуть это будет нельзя.",
            kb.confirm_wipe_keyboard(target_id),
        )
        await query.answer()
        return

    if data.startswith(kb.ADMIN_WIPE_YES):
        if is_self:
            await query.answer("Себя удалить нельзя", show_alert=True)
            return
        stats = db.delete_user(conn, target_id)
        log.info("Удалён пользователь %s: %s", target_id, stats)
        await query.answer("Удалён")
        await _show_list(query, conn)
        return

    await query.answer()


# ── приём id после кнопки «Добавить» ───────────────────────────────────────

async def handle_new_id(
    message: Message, conn: sqlite3.Connection, user: dict[str, Any], bot_username: str | None
) -> bool:
    """Обработать введённый админом id. Возвращает True, если сообщение было про это."""
    if user["id"] not in _awaiting_id:
        return False

    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("Нужно число — Telegram id. Или нажми /admin, чтобы отменить.")
        return True

    _awaiting_id.discard(user["id"])
    target_id = int(text)

    if target_id <= 0:
        await message.answer("Telegram id — положительное число. Попробуй ещё раз через /admin.")
        return True

    if db.invite_user(conn, target_id):
        where = f"@{bot_username}" if bot_username else "этого бота"
        await message.answer(
            f"Готово, доступ открыт для id {target_id}.\n\n"
            f"Перешли ему: «Открой {where} и нажми Старт»."
        )
    else:
        existing = db.get_user(conn, target_id)
        await message.answer(
            f"id {target_id} уже в списке — {STATUS_WORD.get(existing['status'], existing['status'])}."
        )
    await _show_list(message, conn)
    return True
