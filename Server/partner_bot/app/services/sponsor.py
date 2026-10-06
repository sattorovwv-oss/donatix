"""Обязательная подписка на каналы спонсора.

Владелец задаёт каналы, и до подписки бот клиента не обслуживает. Две
вещи здесь важнее удобства.

Первая — бот не должен запереть сам себя. Если канал указан с опечаткой
или бота из него выгнали, проверка подписки не удаётся ни у кого. Считать
это «не подписан» нельзя: тогда один неверный символ в настройке закрыл
бы магазин для всех клиентов разом, а владелец узнал бы об этом по
тишине. Поэтому неудачная проверка пропускает клиента и пишет в журнал.

Вторая — на каждое нажатие в Telegram не сходишь. У десяти тысяч
клиентов это десять тысяч запросов на ровном месте. Поэтому удачная
проверка помнится несколько минут; отказ не помнится вовсе, иначе
кнопка «я подписался» работала бы с задержкой.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app import runtime

log = logging.getLogger(__name__)

#: Сколько помним, что клиент подписан.
TTL = 10 * 60
#: Больше стольких записей не держим: чистим самые старые.
KEEP = 50_000

_ok: dict[int, float] = {}
_links: dict[str, tuple[float, str]] = {}
_lock = asyncio.Lock()

#: Состояния, при которых человек в канале.
INSIDE = ("creator", "administrator", "member")


def on() -> bool:
    return runtime.get_bool("sponsor_on") and bool(channels())


def channels() -> list[str]:
    """Каналы из настройки. Разделитель любой привычный."""
    raw = (runtime.get("sponsor_channel") or "").replace(",", " ")
    out = []
    for piece in raw.split():
        name = piece.strip().strip("/")
        if name.startswith("https://t.me/"):
            name = name[len("https://t.me/"):]
        if name.startswith("t.me/"):
            name = name[len("t.me/"):]
        if not name or name == "-":
            continue
        if not name.startswith("@") and not name.lstrip("-").isdigit():
            name = "@" + name
        if name not in out:
            out.append(name)
    return out


def link_of(channel: str) -> str:
    """Ссылка на канал. Для числового ID — та, что удалось узнать."""
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    hit = _links.get(channel)
    return hit[1] if hit else ""


def forget(user_id: int | None = None) -> None:
    """Забыть, что клиент подписан. Без аргумента — всех."""
    if user_id is None:
        _ok.clear()
    else:
        _ok.pop(user_id, None)


def _remember(user_id: int) -> None:
    now = time.time()
    _ok[user_id] = now
    if len(_ok) > KEEP:
        # Чистим разом, а не по одной: иначе на каждом заказе будет
        # перебор всего словаря.
        stale = [uid for uid, when in _ok.items() if now - when > TTL]
        for uid in stale or list(_ok)[: len(_ok) // 2]:
            _ok.pop(uid, None)


async def _learn_link(bot: Bot, channel: str) -> None:
    """Узнать ссылку на канал по числовому ID — и запомнить."""
    hit = _links.get(channel)
    if hit and time.time() - hit[0] < 3600:
        return
    try:
        chat = await bot.get_chat(channel)
    except TelegramAPIError as exc:
        log.warning("Канал %s не отвечает: %s", channel, exc)
        return
    link = (f"https://t.me/{chat.username}" if chat.username
            else (chat.invite_link or ""))
    _links[channel] = (time.time(), link)


async def missing(bot: Bot, user_id: int) -> list[str]:
    """Каналы, на которые клиент не подписан. Пусто — можно работать."""
    if not on():
        return []

    hit = _ok.get(user_id)
    if hit and time.time() - hit < TTL:
        return []

    out = []
    for channel in channels():
        try:
            member = await bot.get_chat_member(channel, user_id)
        except TelegramAPIError as exc:
            # Канал с опечаткой или бот не админ. Закрывать магазин для
            # всех из-за настройки нельзя — пропускаем и жалуемся в журнал.
            log.warning("Подписку на %s проверить не вышло: %s", channel, exc)
            continue
        if member.status not in INSIDE:
            if not channel.startswith("@"):
                await _learn_link(bot, channel)
            out.append(channel)

    if not out:
        _remember(user_id)
    return out
