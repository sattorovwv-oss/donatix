"""Обязательная подписка на канал спонсора."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.exceptions import TelegramAPIError

from app import db, keyboards, runtime
from app.middlewares.sponsor import SponsorGate
from app.services import sponsor

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Member:
    def __init__(self, status: str):
        self.status = status


class FakeBot:
    """Считает обращения: по ним видно, работает ли память."""

    def __init__(self, status="member", boom=False):
        self.status, self.boom, self.asked = status, boom, 0

    async def get_chat_member(self, chat, user_id):
        self.asked += 1
        if self.boom:
            raise TelegramAPIError(method=None, message="bot is not a member")
        return Member(self.status)

    async def get_chat(self, chat):
        raise TelegramAPIError(method=None, message="нет доступа")


class Screen:
    def __init__(self):
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class Press:
    """Нажатие кнопки."""

    def __init__(self, data=""):
        self.data = data
        self.message = Screen()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)


class Letter:
    """Сообщение от клиента."""

    def __init__(self):
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class Who:
    def __init__(self, uid):
        self.id = uid


async def passes(gate, event, bot, uid) -> bool:
    """True — защита пропустила дальше."""
    reached = []

    async def inner(event, data):
        reached.append(True)

    await gate(inner, event, {"event_from_user": Who(uid), "bot": bot})
    return bool(reached)


async def run(conn) -> None:
    gate = SponsorGate()
    CLIENT, ADMIN = 5001, 111

    # ------------------------------------------- разбор настройки
    await runtime.set_value(conn, "sponsor_channel",
                            "@pervyi, https://t.me/vtoroy  t.me/tretiy")
    check("каналы разбираются из любой записи",
          sponsor.channels() == ["@pervyi", "@vtoroy", "@tretiy"],
          str(sponsor.channels()))

    await runtime.set_value(conn, "sponsor_channel", "@kanal")
    check("ссылка на канал строится", sponsor.link_of("@kanal") == "https://t.me/kanal")

    # ------------------------------------------- выключено — не мешаем
    await runtime.set_value(conn, "sponsor_on", "0")
    sponsor.forget()
    bot = FakeBot()
    check("выключенная проверка пропускает",
          await passes(gate, Letter(), bot, CLIENT))
    check("и не ходит в Telegram", bot.asked == 0, str(bot.asked))

    # ------------------------------------------- подписан — проходит
    await runtime.set_value(conn, "sponsor_on", "1")
    sponsor.forget()
    bot = FakeBot(status="member")
    check("подписанный проходит", await passes(gate, Letter(), bot, CLIENT))
    check("подписку спросили один раз", bot.asked == 1, str(bot.asked))

    await passes(gate, Letter(), bot, CLIENT)
    check("второй раз берём из памяти, а не из Telegram",
          bot.asked == 1, f"обращений: {bot.asked}")

    # ------------------------------------------- не подписан — стоп
    sponsor.forget()
    bot = FakeBot(status="left")
    letter = Letter()
    check("неподписанного дальше не пускаем",
          not await passes(gate, letter, bot, CLIENT))
    check("и показываем, куда подписаться",
          letter.sent and "обуна шавед" in letter.sent[0],
          str(letter.sent)[:80])

    press = Press("m:stars")
    check("нажатия тоже не проходят",
          not await passes(gate, press, bot, CLIENT))
    check("экран подписки показан и на нажатие",
          press.message.sent and "обуна шавед" in press.message.sent[0])

    # кнопка «я подписался» обязана проходить, иначе нажимать её незачем
    check("«я подписался» проходит защиту",
          await passes(gate, Press("sub:check"), bot, CLIENT))

    # ------------------------------------------- владельца не запираем
    check("админ проходит без подписки",
          await passes(gate, Letter(), bot, ADMIN))

    # ------------------- канал с опечаткой не должен закрыть магазин
    # Это главное: одна неверная буква в настройке не может оставить
    # всех клиентов без бота, а владельца — без выручки и без объяснения.
    sponsor.forget()
    broken = FakeBot(boom=True)
    check("при неудачной проверке клиента пропускаем",
          await passes(gate, Letter(), broken, CLIENT))

    # ------------------------------------------- память и клавиатура
    sponsor.forget()
    bot = FakeBot(status="member")
    await passes(gate, Letter(), bot, CLIENT)
    sponsor.forget(CLIENT)
    await passes(gate, Letter(), bot, CLIENT)
    check("после сброса памяти спрашиваем заново", bot.asked == 2, str(bot.asked))

    markup = keyboards.sponsor_gate(["@kanal"])
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    check("на экране есть кнопка подтверждения", "sub:check" in datas, str(datas))
    check("и ссылка на канал", "https://t.me/kanal" in urls, str(urls))

    # Канал без узнаваемой ссылки кнопкой не рисуем: кнопка, ведущая в
    # никуда, читается как поломка бота.
    empty = keyboards.sponsor_gate(["-1001234567890"])
    check("канал без ссылки кнопкой не рисуется",
          not [b for row in empty.inline_keyboard for b in row if b.url])

    await runtime.set_value(conn, "sponsor_on", "0")
    await runtime.set_value(conn, "sponsor_channel", "")
    sponsor.forget()


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
