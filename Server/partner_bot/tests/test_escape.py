"""Команда пробивается сквозь незакрытый шаг диалога.

Баг: панель ждала юзернейм клиента, и на /start отвечала «Такого клиента нет».
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.types import Message

from app.middlewares.escape import CommandEscapeMiddleware
from app.states import Cast, Panel

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def msg(text: str | None) -> Message:
    """Настоящий Message: мидлварь проверяет тип события."""
    return Message.model_construct(message_id=1, date=None, chat=None, text=text)


class FakeState:
    def __init__(self, state=None):
        self.state = getattr(state, "state", state)
        self.cleared = False

    async def get_state(self):
        return self.state

    async def clear(self):
        self.state, self.cleared = None, True


async def passes(text, state) -> tuple[FakeState, bool]:
    """Прогнать сообщение через мидлварь. Вернуть состояние и «дошло ли до ручки»."""
    reached = False

    async def handler(event, data):
        nonlocal reached
        reached = True

    fake = FakeState(state)
    await CommandEscapeMiddleware()(handler, msg(text), {"state": fake})
    return fake, reached


async def run() -> None:
    # ------------------------------------------- команда рвёт любой шаг панели
    for state in (Panel.user_search, Panel.adjust, Panel.value,
                  Panel.emoji, Panel.period):
        fake, reached = await passes("/start", state)
        check(f"/start выходит из {state.state}", fake.state is None)
        check(f"обработчик вызван после {state.state}", reached)

    fake, _ = await passes("/panel", Panel.adjust)
    check("/panel тоже выходит из шага", fake.state is None)

    fake, _ = await passes("/start@my_stars_bot", Panel.user_search)
    check("команда с @именем бота тоже считается командой", fake.state is None)

    # ----------------------------------------------- обычный текст не трогаем
    fake, reached = await passes("@klient", Panel.user_search)
    check("юзернейм не сбрасывает шаг", fake.state == Panel.user_search.state)
    check("юзернейм доходит до обработчика", reached)

    fake, _ = await passes("100 бонус", Panel.adjust)
    check("сумма не сбрасывает шаг", fake.state == Panel.adjust.state)

    fake, _ = await passes("50/100", Panel.value)
    check("слэш внутри текста — не команда", fake.state == Panel.value.state)

    # ------------------------------------------------------ рассылка особая
    fake, _ = await passes("/order 12 — скидка!", Cast.content)
    check("пост рассылки со слэшем не рвётся", fake.state == Cast.content.state)

    fake, _ = await passes("/start", Cast.content)
    check("/start всё же выводит из рассылки", fake.state is None)

    fake, _ = await passes("/cancel", Cast.buttons)
    check("/cancel выводит из шага кнопок", fake.state is None)

    # ------------------------------------------------- вне диалога ничего нет
    fake, reached = await passes("/start", None)
    check("без шага сброса не происходит", not fake.cleared)
    check("без шага сообщение идёт дальше", reached)

    # ------------------------- фото без подписи не роняет мидлварь
    reached = False

    async def handler(event, data):
        nonlocal reached
        reached = True

    fake = FakeState(Panel.value)
    await CommandEscapeMiddleware()(handler, msg(None), {"state": fake})
    check("сообщение без текста проходит спокойно", reached and fake.state == Panel.value.state)


async def live() -> None:
    """Настоящий диспетчер: мидлварь обязана срабатывать раньше фильтров."""
    from aiogram import Bot, Dispatcher, Router
    from aiogram.filters import Command, StateFilter
    from datetime import datetime, timezone

    from aiogram.types import Chat, Update, User

    seen = []

    waiting = Router(name="waiting")

    @waiting.message(StateFilter(Panel.user_search))
    async def on_text(message, state):
        seen.append("шаг")

    commands = Router(name="commands")

    @commands.message(Command("start"))
    async def on_start(message, state):
        await state.clear()
        seen.append("start")

    dp = Dispatcher()
    dp.message.outer_middleware(CommandEscapeMiddleware())
    dp.include_router(waiting)     # как в боте: панель включена первой
    dp.include_router(commands)

    bot = Bot(token="123456:TEST")
    user = User(id=111, is_bot=False, first_name="Админ")
    chat = Chat(id=111, type="private")

    def update(text, uid=1):
        message = Message.model_construct(
            message_id=uid, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=chat, from_user=user, text=text)
        return Update.model_construct(update_id=uid, message=message)

    key = dp.fsm.resolve_context(bot, chat_id=111, user_id=111)

    await key.set_state(Panel.user_search)
    await dp.feed_update(bot, update("@klient"))
    check("шаг ловит обычный текст", seen == ["шаг"], str(seen))

    await key.set_state(Panel.user_search)
    await dp.feed_update(bot, update("/start", 2))
    check("/start доходит до своего обработчика", seen == ["шаг", "start"], str(seen))
    check("после /start шаг закрыт", await key.get_state() is None)

    await bot.session.close()


async def main() -> None:
    await run()
    await live()


asyncio.run(main())
print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
if FAIL:
    print("ПРОВАЛЫ:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
