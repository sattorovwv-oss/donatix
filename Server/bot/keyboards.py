"""Инлайн-кнопки: карточка операции и панель админа."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Операции
EDIT_PREFIX = "edit:"
DELETE_PREFIX = "del:"

# Напоминания
REMINDER_DONE = "rem:done:"
REMINDER_SNOOZE = "rem:snooze:"
REMINDER_CANCEL = "rem:cancel:"

TZ_PREFIX = "tz:"
# Русские названия для тех поясов, что мы предлагаем кнопками.
TZ_NAMES_RU = {
    "Asia/Dushanbe": "Душанбе",
    "Asia/Almaty": "Алматы",
    "Asia/Tashkent": "Ташкент",
    "Europe/Moscow": "Москва",
    "Asia/Bishkek": "Бишкек",
    "Asia/Baku": "Баку",
}
# Те пояса, которые реально нужны этому боту. Остальные — командой /vremya Asia/Baku.
TZ_CHOICES = (
    ("🇹🇯 Душанбе", "Asia/Dushanbe"),
    ("🇰🇿 Алматы", "Asia/Almaty"),
    ("🇺🇿 Ташкент", "Asia/Tashkent"),
    ("🇷🇺 Москва", "Europe/Moscow"),
)

# Панель админа
ADMIN_LIST = "adm:list"
ADMIN_ADD = "adm:add"
ADMIN_USER = "adm:user:"        # + id
ADMIN_BLOCK = "adm:block:"      # + id
ADMIN_UNBLOCK = "adm:unblock:"  # + id
ADMIN_WIPE_ASK = "adm:wipe?:"   # + id — спросить подтверждение
ADMIN_WIPE_YES = "adm:wipe!:"   # + id — удалить насовсем
ADMIN_GRANT = "adm:grant:"      # + id — дать доступ незнакомцу из уведомления

STATUS_MARK = {
    "active": "✅",
    "invited": "⏳",
    "awaiting_name": "⏳",
    "blocked": "🚫",
}


def transaction_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Исправить", callback_data=f"{EDIT_PREFIX}{tx_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{DELETE_PREFIX}{tx_id}"),
        ]]
    )


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сделано", callback_data=f"{REMINDER_DONE}{reminder_id}"),
        InlineKeyboardButton(text="⏰ Отложить на день",
                             callback_data=f"{REMINDER_SNOOZE}{reminder_id}"),
    ]])


def reminders_list_keyboard(reminders: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Список напоминаний — у каждого своя кнопка отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 {r['text'][:40]}",
                              callback_data=f"{REMINDER_CANCEL}{r['id']}")]
        for r in reminders
    ])


def timezone_keyboard() -> InlineKeyboardMarkup:
    rows, pair = [], []
    for label, name in TZ_CHOICES:
        pair.append(InlineKeyboardButton(text=label, callback_data=f"{TZ_PREFIX}{name}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def grant_access_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопка под уведомлением о незнакомце — чтобы не набирать id руками."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="➕ Дать доступ", callback_data=f"{ADMIN_GRANT}{user_id}"),
        ]]
    )


def _user_label(user: dict[str, Any]) -> str:
    mark = STATUS_MARK.get(user["status"], "•")
    name = user.get("name") or (f"@{user['tg_username']}" if user.get("tg_username") else None)
    title = name or f"id {user['id']}"
    if user["role"] == "admin":
        title += " · админ"
    return f"{mark} {title}"


def users_keyboard(users: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_user_label(u), callback_data=f"{ADMIN_USER}{u['id']}")]
        for u in users
    ]
    rows.append([
        InlineKeyboardButton(text="➕ Добавить по ID", callback_data=ADMIN_ADD),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=ADMIN_LIST),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_card_keyboard(user: dict[str, Any], *, is_self: bool) -> InlineKeyboardMarkup:
    """Кнопки карточки. Себя заблокировать или удалить нельзя — иначе можно
    остаться без единого админа и потерять управление ботом."""
    rows: list[list[InlineKeyboardButton]] = []
    user_id = user["id"]

    if not is_self:
        if user["status"] == "blocked":
            rows.append([InlineKeyboardButton(
                text="✅ Разрешить", callback_data=f"{ADMIN_UNBLOCK}{user_id}")])
        else:
            rows.append([InlineKeyboardButton(
                text="🚫 Заблокировать", callback_data=f"{ADMIN_BLOCK}{user_id}")])
        rows.append([InlineKeyboardButton(
            text="🗑 Удалить вместе с данными", callback_data=f"{ADMIN_WIPE_ASK}{user_id}")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=ADMIN_LIST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_wipe_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить навсегда",
                              callback_data=f"{ADMIN_WIPE_YES}{user_id}")],
        [InlineKeyboardButton(text="⬅️ Нет, вернуться",
                              callback_data=f"{ADMIN_USER}{user_id}")],
    ])
