"""Готовые наборы звёзд: кнопки в магазине и их правка из панели."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import PrivateAttr

from app import db, keyboards, runtime
from app.handlers import panel, shop
from app.money import fmt, stars_cost
from app.services.fragment import DeliveryProvider, DeliveryResult, Recipient

BUYER_ID = 777
ADMIN_ID = 111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    """Настоящий User: обработчики проверяют типы через isinstance."""

    def __new__(cls, uid=BUYER_ID, username="buyer"):
        return User(id=uid, is_bot=False, first_name="Клиент", username=username)


class SpyMessage(Message):
    """Сообщение Telegram, которое запоминает, что бот в него написал."""

    _log: list = PrivateAttr(default_factory=list)

    async def answer(self, text, reply_markup=None, **kw):
        self._log.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self._log.append((text, reply_markup))
        return self

    @property
    def last(self) -> str:
        return self._log[-1][0] if self._log else ""

    @property
    def markup(self):
        return self._log[-1][1] if self._log else None


class SpyCallback(CallbackQuery):
    _alerts: list = PrivateAttr(default_factory=list)

    async def answer(self, text="", **kw):
        if text:
            self._alerts.append(text)

    @property
    def alerts(self) -> list:
        return self._alerts

    @property
    def last(self) -> str:
        return self.message.last

    @property
    def markup(self):
        return self.message.markup


def msg(text=None, user=None) -> SpyMessage:
    user = user or FakeUser()
    return SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=user.id, type="private"), from_user=user, text=text,
    )


def call_of(data: str, user=None) -> SpyCallback:
    user = user or FakeUser()
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=msg(user=user),
    )


class OkProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        return DeliveryResult(order_id="frg-1", raw={})

    async def resolve_recipient(self, username):
        return Recipient(username=username, name="Клиент Тест", verified=True)


def buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def state_for(uid: int, storage: MemoryStorage) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


async def run(conn) -> None:
    storage = MemoryStorage()
    buyer = FakeUser()
    state = state_for(BUYER_ID, storage)

    # ------------------------------------------------- набор по умолчанию
    check("наборы заданы из коробки",
          runtime.star_packs() == [50, 100, 250, 500, 1000, 2500],
          str(runtime.star_packs()))

    kb = buttons(keyboards.stars_entry())
    check("кнопок ровно по числу наборов + две служебные", len(kb) == 8, str(len(kb)))
    check("на кнопке видно и звёзды, и сумму", kb[0][0] == "⭐️ 50 — 10.00 с.", kb[0][0])
    check("цена набора равна цене за штуку × количество",
          kb[0][0].endswith(fmt(stars_cost(50))))
    check("у кнопки свой callback", kb[0][1] == "stars:pack:50")
    check("остался ввод своего количества",
          any(data == "stars:buy" for _, data in kb))
    check("есть возврат в меню", any(data == "m:main" for _, data in kb))
    check("наборы по два в ряд",
          len(keyboards.stars_entry().inline_keyboard[0].inline_keyboard
              if hasattr(keyboards.stars_entry().inline_keyboard[0], "inline_keyboard")
              else keyboards.stars_entry().inline_keyboard[0]) == 2)

    # ------------------------------------- цена кнопок следует за наценкой
    old_price = runtime.star_price_e4()
    await runtime.set_value(conn, "star_price_e4", str(old_price * 2))
    doubled = buttons(keyboards.stars_entry())[0][0]
    check("подорожала звезда — подорожали кнопки", doubled == "⭐️ 50 — 20.00 с.", doubled)
    await runtime.set_value(conn, "star_price_e4", str(old_price))

    # --------------------------------------------- покупка нажатием набора
    await db.upsert_user(conn, BUYER_ID, "buyer", "Клиент")
    await db.credit(conn, BUYER_ID, 100_00, as_deposit=True)

    call = call_of("stars:pack:100", user=buyer)
    await shop.cb_stars_pack(call, state, conn)
    data = await state.get_data()
    check("количество взято из кнопки", data.get("quantity") == 100, str(data))
    check("цена посчитана", data.get("price") == 20_00, str(data))
    check("товар помечен как звёзды", data.get("product_type") == "stars")
    check("сразу спрашивает получателя", "Ба кӣ мефиристем" in call.last, call.last[:80])
    check("вопроса о количестве нет", "Сколько звёзд" not in call.last)
    check("ждём получателя", await state.get_state() == "Buy:recipient")

    # ------------------------------------------- не хватает денег на набор
    await state.clear()
    call = call_of("stars:pack:2500", user=buyer)
    await shop.cb_stars_pack(call, state, conn)
    check("дорогой набор упирается в баланс", "намерасад" in call.last.lower(),
          call.last[:100])
    check("после отказа шаг закрыт", await state.get_state() is None)

    # --------------------------------------------- набор, которого больше нет
    await runtime.set_value(conn, "star_packs", "50,100")
    call = call_of("stars:pack:2500", user=buyer)
    await shop.cb_stars_pack(call, state, conn)
    check("исчезнувший набор отклоняется",
          any("дигар нест" in a for a in call.alerts), str(call.alerts))
    check("и показывается свежее меню", "Telegram Stars" in call.last)

    # ------------------------------------------ ручной ввод не сломался
    await state.set_state(shop.Buy.quantity)
    await state.update_data(product_type="stars")
    message = msg("150", user=buyer)
    await shop.on_quantity(message, state, conn)
    data = await state.get_data()
    check("своё количество по-прежнему работает", data.get("quantity") == 150, str(data))
    check("и цена считается так же", data.get("price") == stars_cost(150))

    message = msg("7", user=buyer)
    await state.set_state(shop.Buy.quantity)
    await shop.on_quantity(message, state, conn)
    check("меньше минимума не проходит", "аз <code>50</code>" in message.last,
          message.last[:80])
    await state.clear()

    # ------------------------------------------------- правка из панели
    admin_state = state_for(ADMIN_ID, storage)
    admin = FakeUser(ADMIN_ID, "admin")

    call = call_of("pn:set:star_packs", user=admin)
    await panel.cb_set_field(call, admin_state)
    check("экран правки открывается", "Наборы звёзд" in call.last, call.last[:80])
    check("видно текущий список", "50, 100" in call.last, call.last[:200])
    check("ждём значение", await admin_state.get_state() == "Panel:value")

    message = msg("сто, двести", user=admin)
    await panel.on_field_value(message, admin_state, conn)
    check("текст вместо чисел отклонён", "❌" in message.last)

    message = msg("50, 100, 500, 1000", user=admin)
    await panel.on_field_value(message, admin_state, conn)
    check("список сохранён", runtime.star_packs() == [50, 100, 500, 1000],
          str(runtime.star_packs()))
    check("в ответе виден новый список", "50, 100, 500, 1000" in message.last)
    check("кнопки перестроились",
          [d for _, d in buttons(keyboards.stars_entry()) if d.startswith("stars:pack:")]
          == ["stars:pack:50", "stars:pack:100", "stars:pack:500", "stars:pack:1000"])

    await panel.cb_set_field(call_of("pn:set:star_packs", user=admin), admin_state)
    message = msg("1000 500 100  100", user=admin)
    await panel.on_field_value(message, admin_state, conn)
    check("пробелы вместо запятых тоже понимаются",
          runtime.star_packs() == [100, 500, 1000], str(runtime.star_packs()))
    check("повторы схлопываются", runtime.get("star_packs") == "100,500,1000",
      runtime.get("star_packs"))

    await panel.cb_set_field(call_of("pn:set:star_packs", user=admin), admin_state)
    message = msg("10, 100, 999999", user=admin)
    await panel.on_field_value(message, admin_state, conn)
    check("вне лимитов пропускается", runtime.star_packs() == [100],
          str(runtime.star_packs()))
    check("и об этом сказано прямо", "пропущены" in message.last, message.last)

    await panel.cb_set_field(call_of("pn:set:star_packs", user=admin), admin_state)
    message = msg("10, 20", user=admin)
    await panel.on_field_value(message, admin_state, conn)
    check("совсем негодный список не сохраняется", runtime.star_packs() == [100])
    check("и объясняет лимиты", "вне лимитов" in message.last, message.last)

    check("кнопка наборов есть в разделе цен",
          any("Наборы звёзд" in text for text, _ in buttons(panel.prices_kb())))

    await runtime.set_value(conn, "star_packs", "50,100,250,500,1000,2500")


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
