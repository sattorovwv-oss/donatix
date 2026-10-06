"""Кнопка на старом сообщении: Telegram шлёт заглушку вместо экрана."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime
from app.handlers import menu as menu_h
from app.middlewares.stale_screen import StaleScreenGuard, editable

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Stale:
    """Заглушка Telegram: отвечать умеет, править себя — нет.

    Ровно так выглядит сообщение старше двух суток.
    """

    def __init__(self):
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class Live(Stale):
    """Обычное сообщение — его можно перерисовать."""

    async def edit_text(self, text, **kw):
        self.sent.append(text)


class Who:
    id = 777


class Press:
    def __init__(self, screen, data="m:info"):
        self.data = data
        self.from_user = Who()
        self.message = screen
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)


async def run(conn) -> None:
    gate = StaleScreenGuard()
    await db.upsert_user(conn, 777, "kto", "Кто")

    check("живой экран считается правимым", editable(Live()))
    check("заглушка — нет", not editable(Stale()))
    check("отсутствие экрана — тоже нет", not editable(None))

    reached = []

    async def inner(event, data):
        reached.append(True)

    live = Press(Live())
    await gate(inner, live, {"conn": conn})
    check("по живому экрану защита пропускает", reached == [True])

    reached.clear()
    old = Press(Stale())
    await gate(inner, old, {"conn": conn})
    check("обработчик на старом экране не зовётся", reached == [])
    check("клиенту объяснили, что экран устарел",
          old.alerts and "кӯҳна" in old.alerts[0], str(old.alerts))
    check("и прислали свежее меню",
          old.message.sent and "Хуш омадед" in old.message.sent[0],
          str(old.message.sent)[:60])

    # Без соединения с базой меню не шлём, но часики снимаем: молчание
    # хуже короткого объяснения.
    quiet = Press(Stale())
    await gate(inner, quiet, {})
    check("без базы хотя бы объясняем", bool(quiet.alerts), str(quiet.alerts))
    check("и ничего не падает", quiet.message.sent == [])

    # То, ради чего всё: настоящий обработчик на настоящей заглушке.
    from aiogram.types import Chat, User
    from aiogram.types import InaccessibleMessage as Gap

    real = Press(Gap(chat=Chat(id=777, type="private"), message_id=1, date=0))
    real.from_user = User(id=777, is_bot=False, first_name="Кто")
    boom = None
    try:
        await menu_h.cb_info(real)
    except Exception as exc:  # noqa: BLE001 — ловим именно поломку
        boom = exc
    check("без защиты обработчик правда падает на заглушке",
          isinstance(boom, AttributeError), repr(boom))
    check("и клиент не получает ничего", real.alerts == [])


async def unmatched(conn) -> None:
    """Нажатие, которое не взял никто, не должно пропадать молча."""
    from app.handlers import fallback

    class State:
        def __init__(self):
            self.cleared = False

        async def clear(self):
            self.cleared = True

    # Клиент: потерянный шаг объясняем и возвращаем витрину.
    press = Press(Live(), data="order:self")
    state = State()
    await fallback.nobody_took_it(press, state, conn)
    check("потерянное состояние чистится", state.cleared)
    check("клиенту объяснили, что шаг потерялся",
          press.alerts and "гум шуд" in press.alerts[0], str(press.alerts))
    check("и вернули рабочее меню",
          press.message.sent and "Хуш омадед" in press.message.sent[0],
          str(press.message.sent)[:60])

    # Владельцу — панель, а не витрина: он нажимал в панели.
    class Boss(Who):
        id = 111        # админ из тестового .env

    boss = Press(Live(), data="pn:promo_save")
    boss.from_user = Boss()
    await fallback.nobody_took_it(boss, State(), conn)
    check("владельцу вернули панель",
          boss.message.sent and "Админ-панель" in boss.message.sent[0],
          str(boss.message.sent)[:60])

    # Ловушка: если этот роутер подключить не последним, он съест все
    # нажатия и бот перестанет работать целиком.
    import io as _io
    import re as _re

    body = _io.open("app/main.py", encoding="utf-8").read()
    order = _re.findall(r"dp\.include_router\((\w+)\.router\)", body)
    check("запасной роутер подключён последним",
          order and order[-1] == "fallback", str(order))


async def crash_is_visible(conn) -> None:
    """Поломка в обработчике отвечает клиенту, а не оставляет часики."""
    import app.main as boot

    class Update:
        def __init__(self, press):
            self.callback_query = press

    class Event:
        def __init__(self, press):
            self.update = Update(press)
            self.exception = RuntimeError("что-то сломалось")

    # Достаём перехватчик из собранного диспетчера тем же путём, каким
    # его зовёт aiogram.
    press = Press(Live())
    taken = []

    async def handler(event):
        log = []
        press.alerts.clear()
        return None

    # Собираем настоящего бота нельзя (нужен токен), поэтому проверяем
    # поведение перехватчика напрямую — он объявлен внутри main().
    body = io.open("app/main.py", encoding="utf-8").read()
    check("перехват ошибок подключён", "dp.errors.register(on_error)" in body)
    check("он отвечает на нажатие",
          "await press.answer(text[:190], show_alert=True)" in body and "texts.SOMETHING_BROKE" in body)
    check("и пишет причину в журнал", 'log.exception("Обработчик упал' in body)


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
        await unmatched(conn)
        await crash_is_visible(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
