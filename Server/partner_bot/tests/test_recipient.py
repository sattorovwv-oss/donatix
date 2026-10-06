"""Проверка получателя: кнопка «Себе», подтверждение имени аккаунта."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, keyboards, runtime
from app.handlers import shop as shop_h
from app.services.fragment import (
    DeliveryProvider, MockProvider, Recipient,
)

BUYER = 4242
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=BUYER, username="alijon", first_name="Алиджон"):
        self.id, self.username, self.first_name = uid, username, first_name


class FakeMessage:
    def __init__(self, text=None, user=None):
        self.text = text
        self.from_user = user or FakeUser()
        self.replies: list[str] = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.replies.append(text)
        return self

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""


class FakeCallback:
    def __init__(self, data, user=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.message = FakeMessage(user=self.from_user)
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)

    @property
    def last(self):
        return self.message.last


class NamedProvider(DeliveryProvider):
    """Fragment, который возвращает настоящее имя аккаунта."""

    async def resolve_recipient(self, username):
        known = {"alijon": "Алиджон Махмаджонов", "friend": "Дилшод"}
        if username not in known:
            return None
        return Recipient(username=username, name=known[username], verified=True)


class NamelessProvider(DeliveryProvider):
    """Fragment нашёл аккаунт, но имени не отдал."""

    async def resolve_recipient(self, username):
        return Recipient(username=username, name="", verified=True)


def state_for(storage, uid=BUYER):
    return FSMContext(storage=storage,
                      key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


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


async def run(conn) -> None:
    storage = MemoryStorage()
    state = state_for(storage)
    provider = NamedProvider()

    await db.upsert_user(conn, BUYER, "alijon", "Алиджон")
    await db.credit(conn, BUYER, 100000)

    # ---------------------------------------------- кнопка «Себе» есть/нет
    kb = keyboards.ask_recipient(has_username=True)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("кнопка «Себе» показывается, если юзернейм есть",
          any("Ба худам" in text for text in labels), str(labels))

    kb = keyboards.ask_recipient(has_username=False)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("без юзернейма кнопки «Себе» нет",
          not any("Ба худам" in text for text in labels), str(labels))

    # --------------------------------------------------- покупка себе
    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)

    call = FakeCallback("order:self")
    await shop_h.cb_buy_for_self(call, state, provider)
    check("«Себе» подставляет свой юзернейм без ввода",
          "@alijon" in call.last, call.last.replace("\n", " ")[:80])
    check("«Себе» показывает имя аккаунта",
          "Алиджон Махмаджонов" in call.last)
    check("свой аккаунт помечен как ваш", "ҳисоби шумост" in call.last)
    check("чужого предупреждения на своём аккаунте нет",
          "чужой" not in call.last)

    saved = await state.get_data()
    check("юзернейм сохранён в заказе", saved.get("recipient") == "alijon")

    # ------------------------------------- покупка себе без юзернейма
    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)
    call = FakeCallback("order:self", user=FakeUser(uid=99, username=None))
    await shop_h.cb_buy_for_self(call, state, provider)
    check("без юзернейма объясняется, как его завести",
          "юзернейм надоред" in call.last and "Номи корбар" in call.last)

    # ------------------------------------------------ чужой получатель
    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)
    msg = FakeMessage("@friend")
    await shop_h.on_recipient(msg, state, conn, provider)
    check("чужой аккаунт показывает своё имя", "Дилшод" in msg.last)
    check("чужой аккаунт помечен предупреждением", "бегона" in msg.last.lower())
    check("сначала идёт проверка, а не сразу оплата",
          await state.get_state() == "Buy:check_recipient")

    # ----------------------------------------------- несуществующий
    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)
    msg = FakeMessage("@ghostaccount")
    await shop_h.on_recipient(msg, state, conn, provider)
    check("несуществующий аккаунт отклоняется", "ёфт нашуд" in msg.last)
    check("к оплате не переходим", await state.get_state() == "Buy:recipient")

    # ------------------ шлюз не умеет проверять имя — говорим честно
    class UnverifiedProvider(DeliveryProvider):
        supports_name_lookup = False

        async def resolve_recipient(self, username):
            return Recipient(username=username, name="", verified=False)

    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)
    msg = FakeMessage("@unchecked")
    await shop_h.on_recipient(msg, state, conn, UnverifiedProvider())
    check("непроверенный аккаунт помечается честно",
          "санҷида намешавад" in msg.last, msg.last.replace("\n", " ")[:100])
    check("предлагается открыть t.me для сверки", "t.me/unchecked" in msg.last)
    check("к оплате всё равно можно перейти",
          await state.get_state() == "Buy:check_recipient")

    # -------------------------------------- Fragment не отдал имя
    await state.set_state(shop_h.Buy.recipient)
    await state.update_data(product_type="stars", quantity=100, price=2000)
    msg = FakeMessage("@someone")
    await shop_h.on_recipient(msg, state, conn, NamelessProvider())
    check("без имени показывается хотя бы юзернейм", "@someone" in msg.last)

    # -------------------------------------------- разбор юзернеймов
    cases = {
        "@alijon26": "alijon26",
        "alijon26": "alijon26",
        "https://t.me/alijon26": "alijon26",
        "t.me/alijon26/": "alijon26",
        "  @alijon26  ": "alijon26",
        "ab": None,
        "@1bad": None,
        "плохой юзер": None,
        "@": None,
    }
    bad = {raw: shop_h.parse_username(raw) for raw, exp in cases.items()
           if shop_h.parse_username(raw) != exp}
    check("юзернеймы разбираются во всех формах", not bad, str(bad))

    # ------------------------------------- mock тоже отдаёт имя и ошибку
    mock = MockProvider()
    found = await mock.resolve_recipient("someone")
    missing = await mock.resolve_recipient("notfound")
    check("mock возвращает имя", found is not None and "MOCK" in found.display)
    check("mock умеет не находить аккаунт", missing is None)

    health = await mock.healthcheck()
    check("проверка Fragment в mock честно говорит о режиме",
          health["mode"] == "mock")


asyncio.run(main())
