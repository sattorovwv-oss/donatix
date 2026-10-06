"""Промокоды на скидку: мастер создания, применение к заказу, активации."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, keyboards, runtime
from app.handlers import panel, shop
from app.money import discount_of, fmt
from app.services import delivery
from app.services.fragment import DeliveryError, DeliveryProvider, DeliveryResult, Recipient

ADMIN_ID = 111
BUYER_ID = 777
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=ADMIN_ID, username="admin"):
        self.id, self.username, self.first_name = uid, username, "Кто-то"


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def me(self):
        return FakeUser(1, "test_bot")


class FakeMessage:
    def __init__(self, text=None, user=None, bot=None):
        self.text = text
        self.from_user = user or FakeUser()
        self.bot = bot or FakeBot()
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.replies[-1] if self.replies else ""

    @property
    def markup(self):
        return self.markups[-1] if self.markups else None


class FakeCallback:
    def __init__(self, data, user=None, bot=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.bot = bot or FakeBot()
        self.message = FakeMessage(user=self.from_user, bot=self.bot)
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)

    @property
    def last(self) -> str:
        return self.message.last

    @property
    def markup(self):
        return self.message.markup


class OkProvider(DeliveryProvider):
    def __init__(self):
        self.delivered = []

    async def deliver_stars(self, username, amount):
        self.delivered.append((username, amount))
        return DeliveryResult(order_id="frg-1", raw={})

    async def deliver_premium(self, username, months):
        self.delivered.append((username, months))
        return DeliveryResult(order_id="frg-2", raw={})

    async def resolve_recipient(self, username):
        return Recipient(username=username, name="Кто-то Тест", verified=True)


class FailProvider(OkProvider):
    async def deliver_stars(self, username, amount):
        raise DeliveryError("нет звёзд на балансе")


def texts_of(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def state_for(uid: int, storage: MemoryStorage) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


# ─────────────────────────────────────────────────────────── арифметика


def arithmetic() -> None:
    check("10% от 1000.00 — это 100.00", discount_of(100_000, 10) == 10_000)
    check("итог 1000 − 10% = 900", 100_000 - discount_of(100_000, 10) == 90_000)
    check("15% считается", discount_of(100_00, 15) == 15_00)
    check("нулевой процент скидки не даёт", discount_of(100_00, 0) == 0)
    check("скидка 100% не уводит цену в минус", discount_of(100_00, 100) == 100_00)
    check("копейки округляются к ближайшему дираму",
          discount_of(3333, 10) == 333, str(discount_of(3333, 10)))


# ─────────────────────────────────────────────────── мастер в панели


async def wizard(conn) -> None:
    storage = MemoryStorage()
    state = state_for(ADMIN_ID, storage)

    call = FakeCallback("pn:promos")
    await panel.cb_promos(call, state, conn)
    check("раздел открывается", "Промокоды" in call.last)
    check("есть кнопка создания", "➕ Создать промокод" in texts_of(call.markup))

    call = FakeCallback("pn:promo_new")
    await panel.cb_promo_new(call, state)
    check("шаг 1 просит промокод", "Введите промокод" in call.last, call.last[:120])
    check("шаг 1 показывает пример", "ALI10" in call.last)
    check("ждём код", await state.get_state() == "PromoNew:code")

    message = FakeMessage("ALI 10")
    await panel.on_promo_code(message, state, conn)
    check("код с пробелом отклонён", "❌" in message.last)
    check("после отказа всё ещё шаг 1", await state.get_state() == "PromoNew:code")

    message = FakeMessage("ali10")
    await panel.on_promo_code(message, state, conn)
    check("шаг 2 спрашивает проценты", "Сколько процентов скидка?" in message.last)
    check("код приведён к верхнему регистру", "ALI10" in message.last)
    check("ждём процент", await state.get_state() == "PromoNew:percent")

    message = FakeMessage("0")
    await panel.on_promo_percent(message, state)
    check("ноль процентов отклонён", "❌" in message.last)
    message = FakeMessage("150")
    await panel.on_promo_percent(message, state)
    check("больше 100% отклонено", "❌" in message.last)

    message = FakeMessage("10%")
    await panel.on_promo_percent(message, state)
    check("процент со знаком % понят", "Сколько раз можно активировать" in message.last)
    check("ждём лимит", await state.get_state() == "PromoNew:limit")

    message = FakeMessage("сто")
    await panel.on_promo_limit(message, state)
    check("нечисловой лимит отклонён", "❌" in message.last)

    message = FakeMessage("100")
    await panel.on_promo_limit(message, state)
    check("показано подтверждение", "Проверьте промокод" in message.last)
    for line in ("Промокод: <code>ALI10</code>", "Скидка: <b>10%</b>",
                 "Лимит активаций: <b>100</b>"):
        check(f"в сводке есть «{line[:20]}…»", line in message.last, message.last)
    check("есть кнопка сохранения", "💾 Сохранить" in texts_of(message.markup))
    check("есть кнопка отмены", "❌ Отмена" in texts_of(message.markup))
    check("ждём подтверждения", await state.get_state() == "PromoNew:confirm")
    check("до сохранения кода в базе нет", await db.get_promo(conn, "ALI10") is None)

    call = FakeCallback("pn:promo_save")
    await panel.cb_promo_save(call, state, conn)
    promo = await db.get_promo(conn, "ALI10")
    check("промокод сохранён", promo is not None)
    check("сохранён как скидочный", promo["kind"] == "discount")
    check("процент сохранён", promo["percent"] == 10)
    check("лимит сохранён", promo["max_uses"] == 100)
    check("активаций пока ноль", promo["used_count"] == 0)
    check("шаг закрыт", await state.get_state() is None)
    check("в ответе видно, что код активен", "активен" in call.last.lower())

    # список
    call = FakeCallback("pn:promos")
    await panel.cb_promos(call, state, conn)
    for line in ("−10%", "Использован: <b>0</b> из <b>100</b>",
                 "Осталось: <b>100</b>", "Статус: <b>Активен</b>"):
        check(f"в списке есть «{line}»", line in call.last, call.last)

    # повтор кода
    call = FakeCallback("pn:promo_new")
    await panel.cb_promo_new(call, state)
    message = FakeMessage("ALI10")
    await panel.on_promo_code(message, state, conn)
    check("дубль кода отклонён", "уже есть" in message.last)
    await state.clear()


# ──────────────────────────────────────────────── покупка со скидкой


async def purchase(conn) -> None:
    storage = MemoryStorage()
    buyer = FakeUser(BUYER_ID, "buyer")
    state = state_for(BUYER_ID, storage)
    provider = OkProvider()
    bot = FakeBot()

    await db.upsert_user(conn, BUYER_ID, "buyer", "Покупатель")
    await db.credit(conn, BUYER_ID, 200_00, as_deposit=True)

    # доводим заказ до подтверждения
    await state.set_state(shop.Buy.confirm)
    await state.update_data(product_type="stars", quantity=100, price=20_00,
                            recipient="buyer", recipient_name="Покупатель Тест")

    call = FakeCallback("order:promo", user=buyer, bot=bot)
    await shop.cb_order_promo(call, state)
    check("бот просит промокод", "Промокод" in call.last)
    check("ждём код", await state.get_state() == "Buy:promo")
    check("можно вернуться к заказу",
          any("Назад к заказу" in t for t in texts_of(call.markup)))

    message = FakeMessage("NETAKOGO", user=buyer, bot=bot)
    await shop.on_order_promo(message, state, conn)
    check("несуществующий код отклонён", "не существует" in message.last)
    check("после отказа всё ещё ждём код", await state.get_state() == "Buy:promo")

    # код на баланс к заказу не применяется
    await db.create_promo(conn, "BONUS50", amount=50_00, max_uses=10)
    message = FakeMessage("BONUS50", user=buyer, bot=bot)
    await shop.on_order_promo(message, state, conn)
    check("код на пополнение к заказу не липнет",
          "пополнение баланса" in message.last, message.last[:120])

    message = FakeMessage("ali10", user=buyer, bot=bot)
    await shop.on_order_promo(message, state, conn)
    check("код принят", "истифода шуд" in message.replies[0])
    check("показана экономия", "2.00" in message.replies[0], message.replies[0])
    check("в сводке зачёркнута старая цена", "<s>20.00 с.</s>" in message.last, message.last)
    check("в сводке новая сумма", "Барои пардохт: <b>18.00 с.</b>" in message.last)
    check("в сводке остаток пересчитан", "Мемонад: <b>182.00 с.</b>" in message.last)
    check("вернулись к подтверждению", await state.get_state() == "Buy:confirm")
    check("появилась кнопка снятия промокода",
          "✖️ Промокодро гирифтан" in texts_of(message.markup))

    # снять и вернуть
    call = FakeCallback("order:promo_off", user=buyer, bot=bot)
    await shop.cb_order_promo_off(call, state, conn)
    check("без промокода полная цена", "Барои пардохт: <b>20.00 с.</b>" in call.last)
    check("кнопка снова предлагает промокод",
          any("Промокод" in t for t in texts_of(call.markup)))

    message = FakeMessage("ALI10", user=buyer, bot=bot)
    await state.set_state(shop.Buy.promo)
    await shop.on_order_promo(message, state, conn)
    check("код применяется повторно", "18.00" in message.last)

    check("активация ещё не списана",
          (await db.get_promo(conn, "ALI10"))["used_count"] == 0)

    # оплата
    call = FakeCallback("order:go", user=buyer, bot=bot)
    await shop.cb_pay(call, state, conn, provider, bot)

    user = await db.get_user(conn, BUYER_ID)
    check("списана сумма со скидкой", user.balance == 182_00, fmt(user.balance))
    orders = await db.list_orders(conn, user_id=BUYER_ID)
    order = orders[0]
    check("в заказе цена со скидкой", order.price == 18_00)
    check("в заказе записан промокод", order.promo == "ALI10")
    check("в заказе записан размер скидки", order.discount == 2_00)
    check("заказ выдан", order.status == db.ORDER_DELIVERED)

    promo = await db.get_promo(conn, "ALI10")
    check("активация списана после выдачи", promo["used_count"] == 1, str(promo["used_count"]))
    check("осталось 99", promo["max_uses"] - promo["used_count"] == 99)

    # второй раз тем же клиентом — нельзя
    await state.set_state(shop.Buy.promo)
    await state.update_data(product_type="stars", quantity=100, price=20_00,
                            recipient="buyer", recipient_name="Покупатель Тест")
    message = FakeMessage("ALI10", user=buyer, bot=bot)
    await shop.on_order_promo(message, state, conn)
    check("повторно тем же клиентом код не берётся",
          "уже использовали" in message.last, message.last[:120])
    await state.clear()


# ────────────────────────────── неудачный заказ активацию не съедает


async def refund_keeps_activation(conn) -> None:
    other = 888
    await db.upsert_user(conn, other, "drugoy", "Другой")
    await db.credit(conn, other, 100_00, as_deposit=True)
    bot = FakeBot()

    before = (await db.get_promo(conn, "ALI10"))["used_count"]
    order = await delivery.purchase(
        bot, conn, FailProvider(),
        user_id=other, product_type="stars", quantity=100,
        recipient="drugoy", price=18_00, promo="ALI10", discount=2_00,
    )
    check("заказ вернулся отменённым", order.status == db.ORDER_REFUNDED, order.status)
    check("деньги вернулись клиенту", (await db.get_user(conn, other)).balance == 100_00)
    check("активация не потрачена на сорванном заказе",
          (await db.get_promo(conn, "ALI10"))["used_count"] == before)
    check("клиент может применить код снова",
          not isinstance(await db.check_discount(conn, "ALI10", other), str))


# ─────────────────────────────────────── лимит активаций и резерв


async def limits(conn) -> None:
    await db.create_promo(conn, "ODIN", amount=0, max_uses=1,
                          kind="discount", percent=50)
    await db.upsert_user(conn, 901, "a", "А")
    await db.upsert_user(conn, 902, "b", "Б")

    check("код доступен первому",
          not isinstance(await db.check_discount(conn, "ODIN", 901), str))
    check("код доступен и второму, пока никто не купил",
          not isinstance(await db.check_discount(conn, "ODIN", 902), str))

    # первый оформил заказ — активация зарезервирована
    order = await db.create_order(
        conn, user_id=901, product_type="stars", quantity=50,
        recipient="a", price=10_00, promo="ODIN", discount=10_00,
    )
    check("пока заказ в работе, второму код уже не достаётся",
          await db.check_discount(conn, "ODIN", 902) == "exhausted")

    await db.transition_order(conn, order.id, expected=db.ORDER_DELIVERING,
                              new=db.ORDER_DELIVERED)
    promo = await db.get_promo(conn, "ODIN")
    check("после выдачи активация списана", promo["used_count"] == 1)
    check("код закончился", await db.check_discount(conn, "ODIN", 902) == "exhausted")

    # повторный переход того же заказа не должен списать вторую активацию
    await db.transition_order(conn, order.id, expected=db.ORDER_DELIVERING,
                              new=db.ORDER_DELIVERED)
    check("повторный переход активацию не удваивает",
          (await db.get_promo(conn, "ODIN"))["used_count"] == 1)

    rows = await db.list_promos(conn)
    line = next(panel.promo_line(r) for r in rows if r["code"] == "ODIN")
    check("в списке код помечен как закончившийся", "Закончился" in line, line)
    check("у закончившегося кода осталось 0", "Осталось: <b>0</b>" in line)

    # старые бонусные коды не сломались
    result = await db.redeem_promo(conn, "BONUS50", 902)
    check("бонусный код по-прежнему кладёт деньги на баланс", result == 50_00, str(result))
    check("баланс вырос", (await db.get_user(conn, 902)).balance == 50_00)
    check("бонусный код к заказу не применить",
          await db.check_discount(conn, "BONUS50", 901) == "not_for_order")
    check("скидочный код на баланс не активировать",
          await db.redeem_promo(conn, "ALI10", 902) == "not_for_balance")

    # удаление из списка
    check("промокод удаляется", await db.delete_promo(conn, "ODIN") is True)
    check("удалённого кода нет", await db.get_promo(conn, "ODIN") is None)


async def long_code_button(conn) -> None:
    """Длинный кириллический код: кнопка удаления должна работать.

    Раньше кнопка несла код целиком, а у Telegram на callback_data
    64 байта и кириллица занимает два на букву. Кнопка рисовалась, но
    нажатие до бота не доходило — для владельца она просто не работала.
    """
    from app.handlers import panel

    code = "ПРОМОКОДНАНОВЫЙГОДДВАТЫСЯЧИ"        # 27 русских букв
    await db.create_promo(conn, code, amount=0, max_uses=5,
                          kind="discount", percent=10)

    rows = await db.list_promos(conn, limit=panel.PROMO_LIMIT)
    datas = [b.callback_data
             for row in panel.promos_kb(rows).inline_keyboard for b in row]
    ours = [d for d in datas if d.startswith("pn:promo_del:")]
    check("кнопки удаления укладываются в предел Telegram",
          all(len(d.encode()) <= 64 for d in ours),
          str([(d, len(d.encode())) for d in ours if len(d.encode()) > 64]))

    call = FakeCallback(panel.promo_del_data(code))
    await panel.cb_promo_delete(call, conn)
    check("длинный код удаляется нажатием",
          await db.get_promo(conn, code) is None)
    check("владельцу показали, что именно удалено",
          any(code in alert for alert in call.alerts), str(call.alerts))

    # Кнопка от уже удалённого кода не должна молча ничего не делать.
    stale = FakeCallback(panel.promo_del_data("ЭТОГОКОДАНЕТ"))
    await panel.cb_promo_delete(stale, conn)
    check("устаревшая кнопка честно объясняется",
          any("удалили" in alert for alert in stale.alerts), str(stale.alerts))


async def main() -> None:
    arithmetic()
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await wizard(conn)
        await purchase(conn)
        await refund_keeps_activation(conn)
        await limits(conn)
        await long_code_button(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
