"""Сборка диспетчера и проверка, что каждая кнопка ведёт в обработчик."""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, keyboards, runtime
from app.handlers import (
    admin, api_cab, broadcast, deposit, menu, panel, profile, shop, support,
)
from app.middlewares.guard import UserGuardMiddleware
from app.services.fragment import build_provider

ROUTERS = [panel.router, broadcast.router, admin.router, menu.router,
           shop.router, deposit.router, profile.router, support.router,
           api_cab.router]


def declared_callbacks() -> set[str]:
    """Все callback_data, которые бот реально отдаёт в клавиатурах."""
    found = set()
    markups = [
        keyboards.main_menu(), keyboards.stars_entry(), keyboards.premium_menu(),
        keyboards.confirm(), keyboards.deposit_methods(), keyboards.profile(),
        keyboards.support_menu(True), keyboards.support_menu(False),
        keyboards.back(), keyboards.cancel(),
        keyboards.ask_recipient(True), keyboards.ask_recipient(False),
        keyboards.confirm_recipient(),
        keyboards.admin_deposit(1), keyboards.admin_retry(1),
        # админ-панель
        panel.home_kb(), panel.prices_kb(), panel.pay_kb(), panel.toggles_kb(),
        panel.back_kb(), panel.wallet_kb(), broadcast.audience_kb(), broadcast.compose_kb(),
        broadcast.buttons_kb(True), broadcast.buttons_kb(False),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    found.add(button.callback_data)
    return found


def handled_patterns() -> list[str]:
    """Строки, по которым фильтруются callback-хендлеры, — вытаскиваем из кода."""
    patterns = []
    for path in Path("app/handlers").glob("*.py"):
        text = path.read_text()
        patterns += re.findall(r'F\.data\s*==\s*"([^"]+)"', text)
        patterns += re.findall(r'F\.data\.startswith\("([^"]+)"\)', text)
    return patterns


def exact_datas() -> list[str]:
    """Адреса кнопок без параметров: те, что сравниваются целиком."""
    found = set()
    for path in Path("app/handlers").glob("*.py"):
        found |= set(re.findall(r'F\.data\s*==\s*"([^"]+)"', path.read_text()))
    return sorted(found)


def screens() -> list[tuple[str, object]]:
    """Экраны, которые открываются одной кнопкой.

    Обработчик ищем не разбором текста фильтра — у aiogram он объект без
    внятного вида, — а честной проверкой: подсовываем фильтру нажатие с
    нужным адресом и смотрим, согласился ли он.
    """
    class Probe:
        def __init__(self, data):
            self.data = data

    out = []
    for data in exact_datas():
        # Только экраны панели. Шаги покупки и рассылки открываются
        # посреди диалога и без его состояния не имеют смысла — их
        # проверяют свои наборы, каждый со своим сценарием.
        if not data.startswith("pn:"):
            continue
        probe = Probe(data)
        for router in ROUTERS:
            for handler in router.observers["callback_query"].handlers:
                magics = [flt.magic for flt in (handler.filters or [])
                          if getattr(flt, "magic", None) is not None]
                if magics and all(m.resolve(probe) for m in magics):
                    out.append((data, handler.callback))
                    break
            else:
                continue
            break
    return out


class Dumb:
    """Заглушка вместо поставщика: любой вызов — отказ, как при обрыве."""

    def __getattr__(self, name):
        async def refuse(*a, **kw):
            raise RuntimeError("нет связи")
        return refuse


class Sent:
    """Что «отправил» подставной бот: хватает id, чтобы это потом удалить."""
    message_id = 1
    chat = type("C", (), {"id": 111})()


class MuteBot:
    """Телеграм, который всё принимает и ничего не делает."""

    def __getattr__(self, name):
        async def nothing(*a, **kw):
            return Sent()
        return nothing


class Spy:
    """Сообщение и нажатие в одном: лишь бы обработчику было куда писать."""

    def __init__(self, data: str, uid: int = 111):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "username": "admin",
                                        "first_name": "A", "is_bot": False})()
        self.chat = type("C", (), {"id": uid})()
        self.message = self
        self.message_id = 1
        self.text = "x"
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.html_text = "x"
        self.photo = None
        self.document = None
        self.said: list = []

    async def edit_text(self, text, **kw):
        self.said.append(text)
        return self

    async def answer(self, text="", **kw):
        if text:
            self.said.append(text)
        return self

    async def edit_reply_markup(self, **kw):
        return self

    async def copy_to(self, *a, **kw):
        return None


async def open_every_screen(conn) -> int:
    """Открыть каждый экран панели и убедиться, что он не падает.

    Проверка «кнопка ведёт в обработчик» пропускала настоящую поломку:
    обработчик был, но валился на первой строке — забытый импорт, и
    экран просто не открывался. Кнопку видно, ошибку нет.
    """
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=111, user_id=111))
    ready = {"conn": conn, "state": state, "provider": Dumb(), "bot": MuteBot()}

    broken = []
    for data, handler in sorted(set(screens())):
        spy = Spy(data)
        kwargs = {}
        for name, param in inspect.signature(handler).parameters.items():
            if name in ready:
                kwargs[name] = ready[name]
            elif param.default is inspect.Parameter.empty and not kwargs:
                continue
        try:
            await handler(spy, **kwargs)
        except Exception as exc:  # noqa: BLE001 — мы тут именно за этим
            broken.append(f"{data} → {type(exc).__name__}: {exc}")

    if broken:
        print("\n❌ Экраны, которые не открываются:")
        for line in broken:
            print("  ", line)
        return len(broken)

    print(f"✅ Все {len(set(screens()))} экранов открываются")
    return 0


def check_callback_size() -> bool:
    """У Telegram на callback_data 64 байта.

    Кнопку длиннее он рисует как обычную, но нажатие до бота не
    доставляет: со стороны она просто не работает. Кириллица занимает
    два байта на букву, поэтому мерить надо байты, а не длину строки.
    """
    LIMIT = 64
    big = [(data, len(data.encode()))
           for data in declared_callbacks() if len(data.encode()) > LIMIT]
    if big:
        print("\n❌ Кнопки длиннее предела Telegram:")
        for data, size in big:
            print(f"   {size} байт — {data}")
        return True
    print(f"✅ Все {len(declared_callbacks())} кнопок влезают в {LIMIT} байт")
    return False


async def check_same_screen() -> bool:
    """Повторное нажатие не должно выглядеть поломкой."""
    from aiogram.exceptions import TelegramBadRequest

    from app.middlewares.same_screen import SameScreenGuard

    class Press:
        def __init__(self):
            self.answered = False

        async def answer(self, text="", **kw):
            self.answered = True

    guard = SameScreenGuard()
    bad = []

    async def same(event, data):
        raise TelegramBadRequest(
            method=None, message="Bad Request: message is not modified")

    press = Press()
    try:
        await guard(same, press, {})
    except TelegramBadRequest:
        bad.append("ошибка «экран не изменился» всё ещё роняет обработчик")
    if not press.answered:
        bad.append("часики у клиента не сняли")

    # Всё остальное обязано всплывать: иначе настоящие поломки исчезнут
    # из журнала, и чинить их станет нечего.
    async def other(event, data):
        raise TelegramBadRequest(
            method=None, message="Bad Request: message to edit not found")

    try:
        await guard(other, Press(), {})
        bad.append("чужая ошибка проглочена — поломки станут невидимыми")
    except TelegramBadRequest:
        pass

    if bad:
        print("\n❌ Защита от повторного нажатия: " + "; ".join(bad))
        return True
    print("✅ Повторное нажатие гасится, прочие ошибки всплывают")
    return False


def check_delivery_modes() -> bool:
    """Отчёт при запуске не должен пугать владельца на рабочем режиме.

    Раньше проверка сравнивала режим с одним «api», и бот, честно
    продающий через FazerCards, каждый запуск уверял владельца, что
    звёзды не отправляются, и звал его на старый шлюз.
    """
    from app.config import settings
    from app.main import DELIVERY_MODES, readiness

    def warns(mode: str) -> bool:
        settings.fragment_mode = mode
        _, warnings = readiness()
        return any("НЕ отправляются" in item for item in warnings)

    was, bad = settings.fragment_mode, []
    try:
        for mode in sorted(DELIVERY_MODES):
            if warns(mode):
                bad.append(f"рабочий режим {mode!r} объявлен пустышкой")
        for mode in ("mock", "", "фазер"):
            if not warns(mode):
                bad.append(f"про нерабочий режим {mode!r} не предупредили")
        # Режим из настроек могут написать как угодно регистром.
        if warns("FaZeR"):
            bad.append("режим с другим регистром не распознан")
    finally:
        settings.fragment_mode = was

    if bad:
        print("\n❌ Отчёт о готовности врёт:", "; ".join(bad))
        return True
    print(f"✅ Все {len(DELIVERY_MODES)} рабочих режимов выдачи распознаются")
    return False


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + suffix).unlink(missing_ok=True)

    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    dp = Dispatcher(conn=conn, provider=build_provider())
    dp.message.middleware(UserGuardMiddleware())
    dp.callback_query.middleware(UserGuardMiddleware())
    for router in ROUTERS:
        dp.include_router(router)

    handlers = sum(len(o.handlers) for r in ROUTERS for o in r.observers.values())
    print(f"роутеров: {len(ROUTERS)}, хендлеров: {handlers}")
    print("типы апдейтов:", dp.resolve_used_update_types())

    exact = set(handled_patterns())
    orphans = []
    for data in sorted(declared_callbacks()):
        if data in exact:
            continue
        if any(data.startswith(prefix) for prefix in exact):
            continue
        orphans.append(data)

    if orphans:
        print("\n❌ Кнопки без обработчика:", ", ".join(orphans))
        sys.exit(1)

    print(f"\n✅ Все {len(declared_callbacks())} кнопок ведут в обработчики")

    if await open_every_screen(conn):
        await conn.close()
        sys.exit(1)

    if check_delivery_modes():
        await conn.close()
        sys.exit(1)

    if check_callback_size() or await check_same_screen():
        await conn.close()
        sys.exit(1)
    await conn.close()


asyncio.run(main())
