"""Сквозной прогон пользовательского сценария через настоящие обработчики.

Telegram подменён заглушками, всё остальное — боевой код: FSM, база,
списание баланса, выдача.
"""
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
from app.handlers import deposit as dep_h
from app.handlers import menu as menu_h
from app.handlers import profile as prof_h
from app.handlers import shop as shop_h
from app.handlers import support as sup_h
from app.money import fmt
from app.services.fragment import DeliveryProvider, DeliveryResult, Recipient

USER_ID = 777
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, user_id=USER_ID, username="buyer", first_name="Покупатель"):
        self.id, self.username, self.first_name = user_id, username, first_name


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def me(self):
        return FakeUser(1, "test_bot", "Bot")


class FakePhoto:
    file_id = "photo-123"


class FakeMessage:
    """Сообщение от пользователя. Ответы бота копятся в .replies."""

    def __init__(self, text=None, user=None, photo=False, bot=None):
        self.text = text
        self.from_user = user or FakeUser()
        self.photo = [FakePhoto()] if photo else None
        self.document = None
        self.bot = bot or FakeBot()
        self.replies: list[str] = []
        self.copies: list[int] = []
        self.markups: list = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self

    @property
    def markup(self):
        return self.markups[-1] if self.markups else None

    async def edit_reply_markup(self, **kwargs):
        return self

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return self

    async def copy_to(self, chat_id, **kwargs):
        self.copies.append(chat_id)

    @property
    def last(self) -> str:
        return self.replies[-1] if self.replies else ""


class FakeCallback:
    def __init__(self, data, user=None, bot=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.bot = bot or FakeBot()
        self.message = FakeMessage(user=self.from_user, bot=self.bot)
        self.alerts: list[str] = []

    async def answer(self, text="", **kwargs):
        if text:
            self.alerts.append(text)

    @property
    def last(self) -> str:
        return self.message.last


class OkProvider(DeliveryProvider):
    def __init__(self):
        self.delivered: list[tuple[str, int]] = []

    async def deliver_stars(self, username, amount):
        self.delivered.append((username, amount))
        return DeliveryResult(order_id="frg-1", raw={})

    async def deliver_premium(self, username, months):
        self.delivered.append((username, months))
        return DeliveryResult(order_id="frg-2", raw={})

    async def resolve_recipient(self, username):
        if username == "notfound":
            return None
        return Recipient(username=username, name=f"{username.capitalize()} Test", verified=True)


def make_state(storage: MemoryStorage, bot) -> FSMContext:
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + suffix).unlink(missing_ok=True)

    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run_scenario(conn)
        await cancel_under_photo(conn)
        await one_deposit_at_a_time(conn)
    finally:
        await conn.close()

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


async def one_deposit_at_a_time(conn) -> None:
    """Одна заявка на человека, и один чек на заявку.

    На этом держится весь автоматический приём: платёж узнаётся по сумме
    с копейками, и две открытые заявки дают две суммы, к которым
    пришедшие деньги подходят обе. Выбрать наугад нельзя — платёж уходит
    владельцу разбирать руками.
    """
    from app.handlers import deposit as dep_h

    who = 60_001
    await db.upsert_user(conn, who, "двойник", "Двойник")
    storage = MemoryStorage()
    bot = FakeBot()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=who, user_id=who))
    user = FakeUser(who)

    # Первая заявка заводится обычным порядком.
    call = FakeCallback("dep:card", user=user, bot=bot)
    await dep_h.cb_card(call, state, conn)
    msg = FakeMessage("100", user=user, bot=bot)
    await dep_h.on_amount(msg, state, conn)
    first = (await state.get_data())["deposit_id"]
    check("первая заявка заводится", bool(first), str(first))

    # Вторая — нет: вместо неё показывают первую.
    again = FakeCallback("dep:card", user=user, bot=bot)
    await dep_h.cb_card(again, state, conn)
    check("вторую заявку завести нельзя",
          "аллакай дархост доред" in again.last, again.last[:80])
    check("и показана именно незакрытая",
          fmt((await db.get_deposit(conn, first)).amount) in again.last,
          again.last[:160])

    # Из России — тем же ответом: способ другой, а правило одно.
    ru = FakeCallback("dep:ru", user=user, bot=bot)
    await dep_h.cb_ru(ru, state, conn)
    check("из России вторую тоже не завести",
          "аллакай дархост доред" in ru.last, ru.last[:80])

    # Чек. Первый принимается, второй по той же заявке — нет.
    await state.set_state("Deposit:receipt")
    await state.update_data(amount=10001, deposit_id=first)
    receipt = FakeMessage(user=user, photo=True, bot=bot)
    await dep_h.on_receipt(receipt, state, conn, bot)
    check("первый чек принят",
          bool((await db.get_deposit(conn, first)).receipt_file_id))

    await state.set_state("Deposit:receipt")
    await state.update_data(amount=10001, deposit_id=first)
    twice = FakeMessage(user=user, photo=True, bot=bot)
    await dep_h.on_receipt(twice, state, conn, bot)
    check("тот же чек второй раз не принимается",
          "аллакай фиристода будед" in twice.last or "аллакай гирифта шуд" in twice.last,
          twice.last[:80])

    # Отмена освобождает человека: можно завести новую.
    drop = FakeCallback(f"dep:drop:{first}", user=user, bot=bot)
    await dep_h.cb_drop(drop, state, conn)
    check("заявка отменяется", "бекор шуд" in drop.last, drop.last[:60])
    check("в базе она закрыта",
          (await db.get_deposit(conn, first)).status != db.DEP_PENDING)

    fresh = FakeCallback("dep:card", user=user, bot=bot)
    await dep_h.cb_card(fresh, state, conn)
    check("после отмены новая заявка заводится",
          "Маблағро бо сомонӣ нависед" in fresh.last, fresh.last[:60])

    # Чужую заявку по номеру не закрыть.
    other = 60_002
    await db.upsert_user(conn, other, "чужой", "Чужой")
    mine = await db.create_deposit(conn, user_id=other, amount=12345,
                                   method="карта", receipt_file_id="")
    check("чужую заявку отменить нельзя",
          not await db.cancel_deposit(conn, mine.id, who)
          and (await db.get_deposit(conn, mine.id)).status == db.DEP_PENDING)


async def cancel_under_photo(conn) -> None:
    """«Отмена» под картинкой должна работать.

    Меню показывается правкой текста сообщения, а у фотографии текста
    нет — Telegram такую правку отвергает. Для человека это выглядит
    мёртвой кнопкой: нажал «Отмена» под примером чека, и ничего.
    """
    from app.handlers.menu import render_menu

    class PhotoMessage(FakeMessage):
        """Сообщение-фотография: правку текста отвергает, как Telegram."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.text = None
            self.deleted = False

        async def edit_text(self, *args, **kwargs):
            raise RuntimeError("у фотографии нет текста — правка невозможна")

        async def delete(self):
            self.deleted = True

    call = FakeCallback("m:main")
    call.message = PhotoMessage(user=call.from_user, bot=call.bot)

    await render_menu(call, conn)
    check("меню открывается и с экрана-картинки",
          bool(call.message.replies), str(call.message.replies[:1]))
    check("картинка при этом убирается", call.message.deleted)

    # И обычный текстовый экран по-прежнему правится на месте, а не
    # засыпает чат новыми сообщениями.
    plain = FakeCallback("m:main")
    plain.message.text = "старое меню"
    await render_menu(plain, conn)
    check("текстовый экран по-прежнему правится на месте",
          bool(plain.message.replies), str(plain.message.replies[:1]))


async def run_scenario(conn) -> None:
    bot = FakeBot()
    storage = MemoryStorage()
    state = make_state(storage, bot)
    provider = OkProvider()

    await db.upsert_user(conn, USER_ID, "buyer", "Покупатель")

    # ---------------------------------------------------------------- /start
    msg = FakeMessage("/start", bot=bot)
    await menu_h.cmd_start(msg, state, conn)
    check("/start показывает меню с балансом", "Хуш омадед" in msg.last
          and "0.00" in msg.last)

    # --------------------------------------------------------- пополнение
    call = FakeCallback("dep:card", bot=bot)
    await dep_h.cb_card(call, state, conn)
    check("пополнение спрашивает сумму", "Маблағро бо сомонӣ нависед" in call.last)

    msg = FakeMessage("abc", bot=bot)
    await dep_h.on_amount(msg, state, conn)
    check("нечисловая сумма отклоняется", "Маблағро бо рақам нависед" in msg.last)

    msg = FakeMessage("5", bot=bot)
    await dep_h.on_amount(msg, state, conn)
    check("сумма ниже минимума отклоняется", "Камтарин маблағ" in msg.last)

    msg = FakeMessage("100", bot=bot)
    await dep_h.on_amount(msg, state, conn)
    # Без юзербота (чек проверяет владелец) сумма ровная — никаких копеек.
    paying = (await state.get_data())["amount"]
    check("сумма без копеек", paying == 10000, str(paying))
    check("выдаются реквизиты Душанбе",
          "Душанбе" in msg.last and fmt(paying) in msg.last, msg.last[:200])
    check("состояние ждёт чек", await state.get_state() == "Deposit:receipt")

    check("про чек на первом экране не просим",
          "чек" not in msg.last.lower(), msg.last[:200])
    check("экран реквизитов короткий", len(msg.last) < 420, str(len(msg.last)))
    buttons = [b.text for row in msg.markup.inline_keyboard for b in row]
    check("есть кнопка «я оплатил»",
          any("пардохт кардам" in b for b in buttons), str(buttons))
    check("номер карты можно скопировать",
          any("нусхаи" in b.lower() for b in buttons), str(buttons))

    call = FakeCallback("dep:paid", bot=bot)
    await dep_h.cb_paid(call, state, conn)
    # Юзербота нет — оплату проверяет владелец. Обещать «пополнится само»
    # нельзя: клиент не пришлёт чек, и заявка повиснет.
    check("после «я оплатил» просят чек",
          "скриншоти чекро" in call.last, call.last[:240])
    check("и не обещают зачислить само",
          "худаш" not in call.last, call.last[:240])
    check("названа сумма к зачислению", fmt(paying) in call.last, call.last)
    check("состояние осталось прежним",
          await state.get_state() == "Deposit:receipt")

    call = FakeCallback("dep:back", bot=bot)
    await dep_h.cb_back_to_requisites(call, state, conn)
    check("к реквизитам можно вернуться", "гузаронед" in call.last,
          call.last[:80])

    call = FakeCallback("dep:paid", bot=bot)
    await dep_h.cb_paid(call, state, conn)

    msg = FakeMessage("вот перевёл", bot=bot)
    await dep_h.on_receipt_wrong(msg)
    check("текст вместо чека не принимается", "скриншот" in msg.last)

    msg = FakeMessage(photo=True, bot=bot)
    await dep_h.on_receipt(msg, state, conn, bot)
    pending = await db.list_deposits(conn, status=db.DEP_PENDING)
    check("заявка на пополнение создана", len(pending) == 1
          and pending[0].amount == paying,
          fmt(pending[0].amount) if pending else "—")

    # админ подтверждает
    from app.handlers.admin import _resolve_deposit
    report = await _resolve_deposit(conn, bot, pending[0].id, 111, approved=True)
    user = await db.get_user(conn, USER_ID)
    check("после подтверждения баланс пополнен", user.balance == paying,
          fmt(user.balance))
    check("повторное подтверждение не проходит",
          "уже обработана" in await _resolve_deposit(conn, bot, pending[0].id, 111, approved=True))

    # ------------------------------------------------------- покупка звёзд
    call = FakeCallback("stars:buy", bot=bot)
    await shop_h.cb_stars_buy(call, state, conn)
    check("показан курс и доступное количество",
          "Шумораро бо рақам нависед" in call.last and "500</b> ⭐" in call.last)

    msg = FakeMessage("10", bot=bot)
    await shop_h.on_quantity(msg, state, conn)
    check("количество ниже минимума отклоняется", "Рақами бутун" in msg.last)

    msg = FakeMessage("99999", bot=bot)
    await shop_h.on_quantity(msg, state, conn)
    check("количество выше максимума отклоняется", "Рақами бутун" in msg.last)

    msg = FakeMessage("100", bot=bot)
    await shop_h.on_quantity(msg, state, conn)
    check("корректное количество ведёт к вводу получателя",
          "username" in msg.last.lower())

    msg = FakeMessage("не юзернейм!", bot=bot)
    await shop_h.on_recipient(msg, state, conn, provider)
    check("кривой юзернейм отклоняется", "ба юзернейм монанд нест" in msg.last)

    msg = FakeMessage("@notfound", bot=bot)
    await shop_h.on_recipient(msg, state, conn, provider)
    check("несуществующий получатель отклоняется", "ёфт нашуд" in msg.last)

    msg = FakeMessage("https://t.me/target_user", bot=bot)
    await shop_h.on_recipient(msg, state, conn, provider)
    check("ссылка t.me распознаётся как юзернейм",
          "Гирандаро санҷед" in msg.last and "@target_user" in msg.last)
    check("показывается ИМЯ аккаунта, а не только юзернейм",
          "Target_user Test" in msg.last, msg.last.replace("\n", " ")[:110])
    check("чужой аккаунт помечен предупреждением", "бегона" in msg.last)
    check("состояние ждёт подтверждения получателя",
          await state.get_state() == "Buy:check_recipient")

    call = FakeCallback("order:recipient_ok", bot=bot)
    await shop_h.cb_recipient_ok(call, state, conn)
    check("после подтверждения показывается сводка заказа",
          "Тасдиқи фармоиш" in call.last)
    check("в сводке верная сумма и остаток",
          "20.00" in call.last and fmt(paying - 2000) in call.last,
          call.last.replace("\n", " ")[:100])

    call = FakeCallback("order:go", bot=bot)
    await shop_h.cb_pay(call, state, conn, provider, bot)
    user = await db.get_user(conn, USER_ID)
    orders = await db.list_orders(conn, user_id=USER_ID)
    check("звёзды отправлены получателю", provider.delivered == [("target_user", 100)],
          str(provider.delivered))
    check("баланс списан", user.balance == paying - 2000, fmt(user.balance))
    check("заказ выполнен", orders and orders[0].status == db.ORDER_DELIVERED)
    check("покупателю пришло подтверждение",
          any("иҷро шуд" in text for _, text in bot.messages))

    # ------------------------------------------------- покупка без денег
    call = FakeCallback("stars:buy", bot=bot)
    await shop_h.cb_stars_buy(call, state, conn)
    msg = FakeMessage("10000", bot=bot)
    await shop_h.on_quantity(msg, state, conn)
    check("покупка сверх баланса блокируется до списания",
          "Маблағ намерасад" in msg.last)

    # ------------------------------------------------------------ профиль
    call = FakeCallback("m:profile", bot=bot)
    await prof_h.cb_profile(call, state, conn)
    check("профиль показывает баланс и статистику",
          fmt(paying - 2000) in call.last and "Ситора харида шуд" in call.last
          and "100" in call.last)

    call = FakeCallback("p:history", bot=bot)
    await prof_h.cb_history(call, conn)
    check("история покупок не пустая", "@target_user" in call.last)

    call = FakeCallback("p:ref", bot=bot)
    await prof_h.cb_referral(call, conn)
    check("реферальная ссылка сформирована", f"ref{USER_ID}" in call.last)

    # ---------------------------------------------------------- калькулятор
    msg = FakeMessage("500", bot=bot)
    await menu_h.on_calc(msg)
    check("калькулятор считает стоимость звёзд", "100.00" in msg.last, msg.last)

    msg = FakeMessage("50с", bot=bot)
    await menu_h.on_calc(msg)
    check("калькулятор считает звёзды за сумму", "250" in msg.last, msg.last)

    # ------------------------------------------------------------ поддержка
    call = FakeCallback("m:support", bot=bot)
    await sup_h.cb_support(call, state, conn)
    check("раздел поддержки открывается", "Дастгирӣ" in call.last)

    call = FakeCallback("t:new", bot=bot)
    await sup_h.cb_new_ticket(call, state, conn)
    msg = FakeMessage("Не пришли звёзды по заказу 1", bot=bot)
    await sup_h.on_subject(msg, state, conn, bot)
    tickets = await db.list_tickets(conn, status=db.TICKET_OPEN)
    check("тикет создан", len(tickets) == 1 and "Муроҷиати №" in msg.last)

    call = FakeCallback("t:new", bot=bot)
    await sup_h.cb_new_ticket(call, state, conn)
    check("второй тикет при открытом первом не создаётся",
          any("аллакай муроҷиати кушода доред" in alert for alert in call.alerts))


asyncio.run(main())
