"""Проверка админ-панели: сохранение настроек, наценка, рассылка."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, runtime
from app.handlers import broadcast as bc
from app.handlers import panel
from app.money import fmt, fmt4

ADMIN_ID = 111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=ADMIN_ID, username="admin", first_name="Админ"):
        self.id, self.username, self.first_name = uid, username, first_name


class FakeMessage:
    def __init__(self, text=None, user=None):
        self.text = text
        self.from_user = user or FakeUser()
        self.chat = type("C", (), {"id": self.from_user.id})()
        self.message_id = 1
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))
        return self

    async def edit_text(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))
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


async def _last_markup(call):
    """Разметка последнего показанного экрана."""
    return call.message.markups[-1]


def state_for(storage):
    return FSMContext(storage=storage,
                      key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


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

    # ------------------------------------------------ настройки сохраняются
    check("по умолчанию цена берётся из .env",
          runtime.star_price_e4() == 2000, fmt4(runtime.star_price_e4()))

    call = FakeCallback("pn:set:star_price_e4")
    await panel.cb_set_field(call, state)
    check("экран ввода цены открывается", "Цена продажи звезды" in call.last)

    msg = FakeMessage("не число")
    await panel.on_field_value(msg, state, conn)
    check("нечисловая цена отклоняется", "числом" in msg.last)

    msg = FakeMessage("0.2500")
    await panel.on_field_value(msg, state, conn)
    check("цена сохранилась с четырьмя знаками",
          runtime.star_price_e4() == 2500, fmt4(runtime.star_price_e4()))

    # Четыре знака нужны, чтобы 10% и 15% наценки не сливались в одну цену
    call = FakeCallback("pn:set:star_price_e4")
    await panel.cb_set_field(call, state)
    await panel.on_field_value(FakeMessage("0.1629"), state, conn)
    check("дробная цена сохраняется точно",
          runtime.star_price_e4() == 1629, fmt4(runtime.star_price_e4()))
    await runtime.set_value(conn, "star_price_e4", "2500")

    # Главная проверка: значение должно пережить перезапуск бота
    runtime._cache.clear()
    await runtime.load(conn)
    check("цена пережила перезапуск",
          runtime.star_price_e4() == 2500, fmt4(runtime.star_price_e4()))

    # ---------------------------------------------------------- наценка
    for field, value in (("star_cost_e4", "0.18"), ("margin_percent", "30")):
        call = FakeCallback(f"pn:set:{field}")
        await panel.cb_set_field(call, state)
        await panel.on_field_value(FakeMessage(value), state, conn)

    check("себестоимость сохранилась",
          runtime.star_cost_e4() == 1800, fmt4(runtime.star_cost_e4()))
    check("наценка сохранилась", runtime.margin_percent() == 30)
    check("цена по наценке считается верно",
          runtime.price_from_margin_e4() == 2340, fmt4(runtime.price_from_margin_e4()))

    call = FakeCallback("pn:recalc")
    await panel.cb_recalc(call, conn)
    check("«пересчитать» применяет цену",
          runtime.star_price_e4() == 2340, fmt4(runtime.star_price_e4()))
    check("прибыль с звезды считается",
          runtime.profit_per_star_e4() == 540, fmt4(runtime.profit_per_star_e4()))

    text = panel.prices_text()
    check("экран цен показывает прибыль и заработок с 1000 звёзд",
          "Прибыль с 1 звезды" in text and fmt(5400) in text, text[:200])

    # Продажа ниже себестоимости должна бросаться в глаза
    await runtime.set_value(conn, "star_price_e4", "1000")
    check("убыточная цена помечается предупреждением",
          "убыток" in panel.prices_text().lower())
    await runtime.set_value(conn, "star_price_e4", "2340")

    # ------------------------------------------------ процент и границы
    call = FakeCallback("pn:set:referral_percent")
    await panel.cb_set_field(call, state)
    msg = FakeMessage("150")
    await panel.on_field_value(msg, state, conn)
    check("процент больше 100 отклоняется", "больше 100" in msg.last)

    call = FakeCallback("pn:set:min_stars")
    await panel.cb_set_field(call, state)
    msg = FakeMessage("0")
    await panel.on_field_value(msg, state, conn)
    check("нулевой минимум отклоняется", "больше нуля" in msg.last)

    # --------------------------------------------------------- реквизиты
    call = FakeCallback("pn:set:pay_card_number")
    await panel.cb_set_field(call, state)
    await panel.on_field_value(FakeMessage("8888 7777 6666 5555"), state, conn)
    check("номер карты сохранился",
          runtime.get("pay_card_number") == "8888 7777 6666 5555")
    check("экран реквизитов показывает карту",
          "8888 7777 6666 5555" in panel.pay_text())

    call = FakeCallback("pn:set:pay_extra")
    await panel.cb_set_field(call, state)
    await panel.on_field_value(FakeMessage("-"), state, conn)
    check("минус очищает поле", runtime.get("pay_extra") == "")

    # ------------------------------------------------------ тариф Premium
    call = FakeCallback("pn:premium:6")
    await panel.cb_premium_edit(call, state)
    await panel.on_field_value(FakeMessage("199.50"), state, conn)
    plan = runtime.find_premium(6)
    check("цена Premium сохранилась", plan["price"] == 19950, fmt(plan["price"]))
    runtime._cache.clear()
    await runtime.load(conn)
    check("цена Premium пережила перезапуск",
          runtime.find_premium(6)["price"] == 19950)

    # --------------------------------------------------- включение разделов
    check("звёзды включены по умолчанию", runtime.get_bool("stars_enabled"))
    # ----------------------------------- автоцены: кнопка была не нарисована
    # Экран цен писал, включены автоцены или нет, служба обновления
    # работала, обработчик был — а кнопки не было ни одной. Владелец не
    # мог включить их вообще никак.
    datas = [b.callback_data
             for row in panel.prices_kb().inline_keyboard for b in row]
    check("на экране цен есть кнопка автоцен", "pn:autoprice" in datas,
          str(datas))

    await panel.cb_toggle(FakeCallback("pn:toggle:stars_enabled"), conn)
    check("раздел выключается", not runtime.get_bool("stars_enabled"))
    from app import keyboards
    buttons = [b.text for row in keyboards.main_menu().inline_keyboard for b in row]
    check("выключенный раздел пропал из меню клиента",
          not any("звезды" in b for b in buttons), str(buttons))
    await panel.cb_toggle(FakeCallback("pn:toggle:stars_enabled"), conn)

    # ------------------------------------------------- кнопки для рассылки
    good = "Наш канал - https://t.me/mychan\nОтзывы | https://example.com/x"
    parsed = [bc.BUTTON_RE.match(line.strip()) for line in good.splitlines()]
    check("кнопки разбираются с дефисом и чертой", all(parsed))
    check("название и ссылка разделены верно",
          parsed[0].group(1) == "Наш канал" and parsed[0].group(2) == "https://t.me/mychan")

    for bad in ("просто текст", "Кнопка - ftp://x", "Кнопка без ссылки -"):
        check(f"мусор отклоняется: {bad[:22]}", bc.BUTTON_RE.match(bad) is None)

    markup = bc.build_markup([["Канал", "https://t.me/c"], ["Бот", "https://t.me/b"]])
    check("клавиатура рассылки собирается",
          len(markup.inline_keyboard) == 2
          and markup.inline_keyboard[0][0].url == "https://t.me/c")
    check("без кнопок клавиатуры нет", bc.build_markup([]) is None)

    # ------------------------------------------------------- аудитории
    await db.upsert_user(conn, 900, "a", "A")
    await db.upsert_user(conn, 901, "b", "B")
    await db.upsert_user(conn, 902, "c", "C")
    await db.credit(conn, 901, 5000)
    await db.create_order(conn, user_id=902, product_type="stars", quantity=100,
                          recipient="x", price=100)
    await db.set_banned(conn, 900, True)

    counts = {key: await bc.count_audience(conn, key) for key in bc.AUDIENCES}
    check("забаненные исключены из рассылки", counts["all"] == 2, str(counts))
    check("«у кого есть баланс» считает верно", counts["funded"] == 1, str(counts))
    check("«только покупателям» считает верно", counts["buyers"] == 1, str(counts))
    check("«без покупок» считает верно", counts["silent"] == 1, str(counts))

    ids = await bc.audience_ids(conn, "funded")
    check("список получателей корректен", ids == [901], str(ids))

    # ------------------------------------------ себестоимость из API
    from decimal import Decimal

    class FakeEstimate:
        quantity, amount, currency = 1000, "11.50", "ton"
        usd_total = Decimal("32.775")
        usd_per_unit = Decimal("0.032775")
        usdt_per_ton = "2.85"

    class CostProvider:
        async def cost_estimate(self, product_type, amount):
            return FakeEstimate()

    # Без курса доллара бот должен просить его, а не считать наугад
    await runtime.set_value(conn, "usd_rate_diram", "0")
    call = FakeCallback("pn:cost")
    await panel.cb_cost(call, CostProvider())
    check("без курса доллара бот просит его задать",
          "курс доллара" in call.last.lower(), call.last[:70])

    await runtime.set_value(conn, "usd_rate_diram", "1090")   # 10.90 сомони
    await runtime.set_value(conn, "margin_percent", "10")
    call = FakeCallback("pn:cost")
    await panel.cb_cost(call, CostProvider())
    check("себестоимость посчитана из цены сервиса",
          "0.3572" in call.last, call.last.replace("\n", " ")[:170])
    check("показана цена в долларах", "$32.77" in call.last or "32.78" in call.last)
    # 0.36 × 1.10 = 0.396 -> округляется до 0.40
    check("предложена цена с наценкой 10%",
          "0.3929" in call.last, call.last.replace("\n", " ")[-170:])

    # Кнопка сохранения несёт посчитанное значение
    buttons = [b.callback_data for row in
               (await _last_markup(call)).inline_keyboard for b in row]
    save = next((b for b in buttons if b and b.startswith("pn:cost_save:")), None)
    check("кнопка сохранения содержит себестоимость",
          save == "pn:cost_save:3572", str(save))

    await panel.cb_cost_save(FakeCallback(save), conn)
    check("себестоимость сохранилась",
          runtime.star_cost_e4() == 3572, str(runtime.star_cost_e4()))
    check("цена продажи пересчитана по наценке 10%",
          runtime.star_price_e4() == 3929, str(runtime.star_price_e4()))

    # Провайдер без цен не должен ронять панель
    class NoCostProvider:
        pass

    call = FakeCallback("pn:cost")
    await panel.cb_cost(call, NoCostProvider())
    check("провайдер без цен объясняется, а не падает",
          any("вручную" in a for a in call.alerts), str(call.alerts))

    # Ошибка сервиса тоже не роняет
    class BrokenCost:
        async def cost_estimate(self, *a):
            raise RuntimeError("сеть недоступна")

    call = FakeCallback("pn:cost")
    await panel.cb_cost(call, BrokenCost())
    check("ошибка сервиса показывается понятно",
          "Цена не пришла" in call.last, call.last[:60])

    # ------------------------------------------------------ главный экран
    home = await panel.home_text(conn)
    check("главный экран собирается и показывает цену", "Админ-панель" in home
          and fmt4(runtime.star_price_e4()) in home, home[-120:])


asyncio.run(main())
