"""Пополнение Steam: проверка логина, покупка, панель, разбор ответов API."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import PrivateAttr

from app import db, keyboards, runtime, texts
from app.handlers import panel, shop
from app.money import fmt, steam_cost
from app.services.fragment import DeliveryProvider, DeliveryResult, SteamAccount

BUYER = 777
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def me(self):
        return User(id=1, is_bot=True, first_name="Bot", username="test_bot")


class SpyMessage(Message):
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
    def last(self) -> str:
        return self.message.last

    @property
    def markup(self):
        return self.message.markup

    @property
    def alerts(self) -> list:
        return self._alerts


def msg(text=None, uid=BUYER) -> SpyMessage:
    user = User(id=uid, is_bot=False, first_name="Клиент", username="buyer")
    return SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=user, text=text,
    )


def call_of(data: str, uid=BUYER) -> SpyCallback:
    user = User(id=uid, is_bot=False, first_name="Клиент", username="buyer")
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=msg(uid=uid),
    )


def buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class SteamProvider(DeliveryProvider):
    """Сервис выдачи, у которого Steam работает."""

    def __init__(self, known=("mypal", "igrok"), answer=True):
        self.topped: list[tuple[str, int]] = []
        self.known, self.answer = set(known), answer

    async def check_steam_login(self, login):
        if not self.answer:
            return None            # сервис не смог проверить
        return SteamAccount(login=login, exists=login in self.known,
                            name=f"{login.capitalize()} Player")

    async def deliver_steam(self, login, amount):
        self.topped.append((login, amount))
        return DeliveryResult(order_id="fzr-steam-1", raw={})

    async def steam_rate(self):
        return Decimal("0.0105"), "RUB"


# ────────────────────────────────── разбор ответов сервиса (без сети)


async def api_parsing() -> None:
    from app.services import fazer

    class Stub(fazer.FazerProvider):
        def __init__(self, reply):
            self.reply = reply
            self._base = "https://x"
            self._session = None

        async def _request(self, method, path, payload=None, *, safe=False,
                           **kw):
            self.last = (method, path, payload)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    # курс в разных обёртках
    for reply, where in (
        ({"rate": 0.0105, "currency": "RUB"}, "в корне"),
        ({"data": {"price": "0.0105", "wallet_currency": "rub"}}, "в data"),
        ({"rates": [{"price_usd": 0.0105, "code": "RUB"}]}, "списком"),
    ):
        rate, currency = await Stub(reply).steam_rate()
        check(f"курс Steam разобран {where}", rate == Decimal("0.0105"), str(rate))
        check(f"валюта разобрана {where}", currency == "RUB", currency)

    try:
        await Stub({"ok": True}).steam_rate()
        check("ответ без курса — ошибка", False)
    except fazer.DeliveryError as exc:
        check("ответ без курса — ошибка", "не вернул курс" in str(exc), str(exc))

    # проверка логина
    for reply, exists, where in (
        ({"exists": True, "name": "Player"}, True, "по exists"),
        ({"account": {"valid": True, "persona_name": "Игрок"}}, True, "по valid"),
        ({"exists": False}, False, "отрицательный ответ"),
        ({"found": "false"}, False, "строкой false"),
    ):
        account = await Stub(reply).check_steam_login("kto")
        check(f"логин разобран {where}", account is not None
              and account.exists is exists, str(account))

    account = await Stub({"что-то": "своё"}).check_steam_login("kto")
    check("непонятный ответ не выдаётся за проверку", account is None, str(account))

    account = await Stub(fazer.DeliveryError("503")).check_steam_login("kto")
    check("сбой проверки — не подтверждение", account is None)

    stub = Stub({"order": {"id": "1", "status": "completed"}})
    await stub.deliver_steam("mypal", 500)
    check("заказ уходит на нужный адрес",
          stub.last[1] == "/api/v2/steam-topup/order", str(stub.last))
    check("в заказе логин и сумма",
          stub.last[2] == {"login": "mypal", "amount": 500}, str(stub.last[2]))


# ─────────────────────────────────────────────────── покупка клиентом


async def flow(conn) -> None:
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    provider = SteamProvider()
    bot = FakeBot()

    await runtime.set_value(conn, "steam_price_e4", "1400")
    await runtime.set_value(conn, "steam_cost_e4", "1200")
    await runtime.set_value(conn, "steam_enabled", "1")
    await db.upsert_user(conn, BUYER, "buyer", "Клиент")
    await db.credit(conn, BUYER, 200_00, as_deposit=True)

    # На кнопке входа в игры и Steam стоят только значки, без слов —
    # ищем её по адресу, а не по подписи.
    check("раздел появился в меню",
          any(b.callback_data == "m:games"
              for row in keyboards.main_menu().inline_keyboard for b in row),
          str(buttons(keyboards.main_menu())))

    call = call_of("m:steam")
    await shop.cb_steam(call, state)
    check("экран Steam открывается", "Пур кардани Steam" in call.last)
    check("сказано, что пароль не нужен", "парол" in call.last.lower())
    check("суммы показаны кнопками",
          "🎮 500 RUB — 70.00 с." in buttons(call.markup), str(buttons(call.markup)))

    call = call_of("steam:500")
    await shop.cb_steam_amount(call, state, conn)
    check("бот просит логин", "Логини Steam" in call.last, call.last[:80])
    check("предупреждает, что это не ник", "дар профил ё почта нест" in call.last)
    check("ждём логин", await state.get_state() == "Steam:login")
    data = await state.get_data()
    check("сумма и цена сохранены",
          data["quantity"] == 500 and data["price"] == steam_cost(500), str(data))

    bad = msg("а")
    await shop.on_steam_login(bad, state, conn, provider)
    check("слишком короткий логин отклонён", "Чунин ҳисоб нест" in bad.last)

    unknown = msg("neznakomyi")
    await shop.on_steam_login(unknown, state, conn, provider)
    check("несуществующий логин отклонён",
          "Чунин ҳисоб нест" in unknown.last, unknown.last[:60])
    check("после отказа всё ещё ждём логин",
          await state.get_state() == "Steam:login")
    check("заказ при этом не создан",
          await db.list_orders(conn, user_id=BUYER) == [])

    # сервис не смог проверить — не продаём вслепую
    silent = msg("mypal")
    await shop.on_steam_login(silent, state, conn, SteamProvider(answer=False))
    check("без проверки заказ не идёт", "санҷида натавонистам" in silent.last,
          silent.last[:60])
    check("шаг закрыт, деньги не тронуты",
          (await db.get_user(conn, BUYER)).balance == 200_00)

    # верный логин
    await state.set_state(shop.Steam.login)
    await state.update_data(product_type="steam", quantity=500,
                            price=steam_cost(500))
    good = msg("mypal")
    await shop.on_steam_login(good, state, conn, provider)
    check("аккаунт показан на подтверждение", "Ҳисобро санҷед" in good.last)
    check("видно имя аккаунта", "Mypal Player" in good.last, good.last[:200])
    check("видно логин", "<code>mypal</code>" in good.last)
    check("видно сумму", "500 RUB" in good.last)
    check("видно списание", "70.00 с." in good.last)
    check("предупреждение о необратимости", "баргардонидани он мумкин нест" in good.last)
    check("ждём подтверждения", await state.get_state() == "Steam:confirm")
    check("есть кнопка смены логина",
          any("Логини дигар" in b for b in buttons(good.markup)))

    call = call_of("steam:again")
    await shop.cb_steam_again(call, state)
    check("можно вернуться к вводу логина",
          await state.get_state() == "Steam:login")
    check("сумма при этом не потерялась", "500 RUB" in call.last, call.last[:80])

    await state.set_state(shop.Steam.confirm)
    call = call_of("steam:ok")
    await shop.cb_steam_pay(call, state, conn, provider, bot)

    check("пополнение ушло в сервис", provider.topped == [("mypal", 500)],
          str(provider.topped))
    user = await db.get_user(conn, BUYER)
    check("деньги списаны", user.balance == 200_00 - steam_cost(500), fmt(user.balance))
    order = (await db.list_orders(conn, user_id=BUYER))[0]
    check("заказ создан", order.product_type == "steam" and order.quantity == 500)
    check("в заказе логин", order.recipient == "mypal")
    check("заказ выполнен", order.status == db.ORDER_DELIVERED)
    check("себестоимость записана", order.cost == 60_00, str(order.cost))
    check("прибыль считается", order.profit == steam_cost(500) - 60_00,
          str(order.profit))
    check("название заказа читаемое", "Steam 500 RUB" in order.title, order.title)

    done = [t for _, t in bot.sent if "иҷро шуд" in t]
    check("клиенту пришло своё сообщение о Steam", done and "Steam" in done[0],
          str(done[:1]))
    check("в нём логин, а не юзернейм Telegram", "mypal" in done[0])
    check("в нём сумма пополнения", "500 RUB" in done[0])

    # ------------------------------------------------ не хватает денег
    await state.clear()
    call = call_of("steam:1000")
    await shop.cb_steam_amount(call, state, conn)
    check("дорогая сумма упирается в баланс", "намерасад" in call.last.lower(),
          call.last[:80])

    # ------------------------------------------------ исчезнувшая сумма
    await runtime.set_value(conn, "steam_packs", "100,250")
    call = call_of("steam:500")
    await shop.cb_steam_amount(call, state, conn)
    check("снятая с продажи сумма отклоняется",
          any("дигар нест" in a for a in call.alerts), str(call.alerts))
    await runtime.set_value(conn, "steam_packs", "100,250,500,1000")

    # ------------------------------------------------ выключенный раздел
    await runtime.set_value(conn, "steam_enabled", "0")
    check("выключенный раздел пропадает из меню",
          not any("Steam" in b for b in buttons(keyboards.main_menu())))
    call = call_of("m:steam")
    await shop.cb_steam(call, state)
    check("и не открывается", any("баста аст" in a for a in call.alerts),
          str(call.alerts))
    await runtime.set_value(conn, "steam_enabled", "1")


# ─────────────────────────────────────────────────────────── панель


async def panel_screens(conn) -> None:
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=111, user_id=111))
    call = call_of("pn:steam", uid=111)
    await panel.cb_steam(call, state)
    check("раздел панели открывается", "Пополнение Steam" in call.last)
    check("видна цена продажи", "0.1400 с." in call.last, call.last[:250])
    check("видна прибыль с единицы", "Прибыль с единицы" in call.last)
    check("видны суммы кнопками", "500 RUB — <b>70.00 с.</b>" in call.last)

    await runtime.set_value(conn, "steam_enabled", "0")
    await runtime.set_value(conn, "steam_price_e4", "0")
    call = call_of("pn:steam_toggle", uid=111)
    await panel.cb_steam_toggle(call, conn)
    check("без цены раздел не включить", runtime.get_bool("steam_enabled") is False)
    check("и сказано почему", any("цену" in a for a in call.alerts), str(call.alerts))

    await runtime.set_value(conn, "steam_price_e4", "1400")
    await panel.cb_steam_toggle(call_of("pn:steam_toggle", uid=111), conn)
    check("с ценой включается", runtime.get_bool("steam_enabled") is True)

    # курс от сервиса
    await runtime.set_value(conn, "usd_rate_diram", "1090")
    call = call_of("pn:steam_rate", uid=111)
    await panel.cb_steam_rate(call, conn, SteamProvider())
    check("курс Steam показан", "0.0105" in call.last, call.last[:250])
    check("себестоимость сохранена автоматически",
          runtime.steam_cost_e4() == 1144, str(runtime.steam_cost_e4()))
    check("валюта подхвачена", runtime.steam_currency() == "RUB")

    class NoRate(DeliveryProvider):
        pass

    call = call_of("pn:steam_rate", uid=111)
    await panel.cb_steam_rate(call, conn, NoRate())
    check("режим без цен Steam объясняется", "недоступен" in call.last,
          call.last[:120])

    check("раздел есть в главном меню панели",
          any("Steam" in b.text for r in panel.home_kb().inline_keyboard for b in r))


async def main() -> None:
    await api_parsing()
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await flow(conn)
        await panel_screens(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
