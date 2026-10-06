"""Deep Link — ссылка вида t.me/бот?start=код.

Telegram отдаёт боту всё, что стоит после `?start=`, первым аргументом
команды /start. По этому коду и считаем, откуда пришёл человек.

Реферальные коды (`ref12345`) живут в этом же поле, поэтому имя рекламной
ссылки не должно быть на них похоже — иначе одно перебьёт другое.
"""
from __future__ import annotations

import re

from aiogram import Bot

from app.config import settings

# Что разрешает сам Telegram: латиница, цифры, дефис и подчёркивание, до 64.
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REFERRAL_RE = re.compile(r"^ref\d+$")

_username: str | None = None


def payload_of(text: str | None) -> str:
    """Код из команды `/start код`. Пустая строка — если кода нет."""
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    if not parts or not parts[0].split("@", 1)[0] == "/start":
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


def is_referral(code: str) -> bool:
    return bool(REFERRAL_RE.match(code))


def check(code: str) -> str | None:
    """Что не так с названием ссылки. None — всё в порядке."""
    if not code:
        return "Название пустое."
    if len(code) > 64:
        return "Слишком длинное: максимум 64 символа."
    if not CODE_RE.match(code):
        return (
            "Telegram разрешает только латинские буквы, цифры, дефис "
            "и подчёркивание. Пробелов и русских букв быть не должно."
        )
    if is_referral(code):
        return "Так выглядят реферальные ссылки — выберите другое название."
    return None


def build(username: str, code: str) -> str:
    return f"https://t.me/{username}?start={code}"


async def bot_username(bot: Bot) -> str:
    """Имя бота для ссылки. Спрашиваем Telegram один раз за запуск."""
    global _username
    if settings.bot_username:
        return settings.bot_username.lstrip("@")
    if _username is None:
        _username = (await bot.me()).username or ""
    return _username
