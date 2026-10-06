"""Игры: цены пакетов, проверка ID, заказ, присмотр и возврат по таймауту."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
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
from app.handlers import games as gh
from app.handlers import panel
from app.money import fmt
from app.services import games as svc
from app.services import nicknames
from app.services.fragment import DeliveryError, DeliveryProvider, DeliveryUncertain

BUYER = 777
ADMIN = 111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    def to(self, chat_id):
        return [t for cid, t in self.sent if cid == chat_id]


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
    user = User(id=uid, is_bot=False, first_name="Игрок", username="gamer")
    return SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=user, text=text,
    )


def call_of(data: str, uid=BUYER) -> SpyCallback:
    user = User(id=uid, is_bot=False, first_name="Игрок", username="gamer")
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=msg(uid=uid),
    )


def buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class GameProvider(DeliveryProvider):
    """Поставщик игр: по умолчанию принимает заказ и держит его в обработке."""

    def __init__(self, *, validate=("Ник", "ok"), status="processing",
                 order_error=None):
        self.validate_reply = validate
        self.status = status
        self.order_error = order_error
        self.orders: list[dict] = []
        self.idempotency: list[str] = []

    async def game_catalog(self):
        return [{"category_id": "free_fire_br", "name": "Free Fire",
                 "fields": [{"name": "player_id", "label": "ID игрока"}]}]

    async def game_offers(self, category_id):
        return [
            {"offer_id": "off_1", "name": "100 алмазов",
             "usd": Decimal("1.00"), "raw": {}},
            {"offer_id": "off_2", "name": "500 алмазов",
             "usd": Decimal("4.50"), "raw": {}},
        ]

    async def validate_game_id(self, category_id, fields):
        return self.validate_reply

    async def order_game(self, *, category_id, offer_id, fields, quantity,
                         idempotency_key):
        if self.order_error:
            raise self.order_error
        self.idempotency.append(idempotency_key)
        self.orders.append({"category_id": category_id, "offer_id": offer_id,
                            "fields": fields, "quantity": quantity})
        return {"order_id": "ord-906267", "status": "processing"}

    async def order_status(self, order_id):
        return {"order_id": order_id, "status": self.status}


# ─────────────────────────────────────────────────────── цены и ключи


async def pricing(conn) -> None:
    await runtime.set_value(conn, "usd_rate_diram", "1090")   # 10.90 с. за $
    await runtime.set_value(conn, "margin_percent", "20")
    await runtime.set_value(conn, "round_prices", "up1")

    # $1.00 × 10.90 = 10.90 с., +20% = 13.08 с., округление вверх → 14.00 с.
    price = svc.offer_price(Decimal("1.00"), 20)
    check("цена пакета = себестоимость × наценка, округлённая",
          price == 1400, str(price))
    check("себестоимость считается по курсу",
          svc.offer_cost(Decimal("1.00")) == 1090, str(svc.offer_cost(Decimal("1"))))

    await runtime.set_value(conn, "usd_rate_diram", "0")
    check("без курса цены нет", svc.offer_price(Decimal("1.00"), 20) == 0)
    await runtime.set_value(conn, "usd_rate_diram", "1090")

    game = db.Game(category_id="x", title="X", field="user_id", region="",
                   margin=0, enabled=1, created_at="")
    check("без своей наценки берётся общая", svc.margin_of(game) == 20)
    game.margin = 35
    check("своя наценка важнее общей", svc.margin_of(game) == 35)

    keys = {svc.idempotency_key(n) for n in range(50)}
    check("ключ идемпотентности уникален на заказ", len(keys) == 50)
    check("ключ одного заказа не меняется",
          svc.idempotency_key(7) == svc.idempotency_key(7))
    check("у заказа №1 в пересозданном боте другой ключ",
          svc.idempotency_key(1, "2026-01-01 10:00:00") != svc.idempotency_key(1, "2026-02-01 12:00:00"))
    from app.config import settings as _st
    _old = _st.bot_token
    try:
        a = svc.idempotency_key(1, "2026-01-01 10:00:00")
        object.__setattr__(_st, "bot_token", "999:AAA")
        check("у заказа №1 второго бота другой ключ", svc.idempotency_key(1, "2026-01-01 10:00:00") != a)
    finally:
        object.__setattr__(_st, "bot_token", _old)


# ────────────────────────────────────────────────────────── ники


async def nick_lookup() -> None:
    nicknames.forget_all()

    class Resp:
        def __init__(self, payload, status=200):
            self.payload, self.status = payload, status

        async def json(self, content_type=None):
            return self.payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    calls = []

    class Session:
        def __init__(self, reply):
            self.reply = reply

        def get(self, url, headers=None):
            calls.append(url)
            return self.reply

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    real = nicknames.aiohttp.ClientSession
    nicknames.aiohttp.ClientSession = lambda *a, **kw: Session(
        Resp({"AccountInfo": {"AccountName": "ProPlayer", "AccountLevel": 70}})
    )
    found = await nicknames.free_fire("1724367212", key="k", region="BR")
    check("ник получен", found.name == "ProPlayer" and found.verdict == "ok",
          str(found))
    check("первым спрашиваем источник без ключа",
          "glob-info" in calls[0], str(calls[:2]))

    before = len(calls)
    again = await nicknames.free_fire("1724367212", key="k", region="BR")
    check("повтор берётся из кэша", again.source == "кэш", again.source)
    check("лимит при этом не тратится", len(calls) == before, str(len(calls)))

    nicknames.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({}, status=402))
    bad = await nicknames.free_fire("999", key="k")
    check("402 — это «неверный ID»", bad.verdict == "bad", str(bad))

    nicknames.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({}, status=429))
    over = await nicknames.free_fire("555", key="k")
    check("429 не считается неверным ID", over.verdict == "unknown", str(over))
    check("неудача не кэшируется", nicknames.cached("555") is None)

    # ---------------------------------------- три источника по очереди
    nicknames.forget_all()
    calls.clear()

    class ByUrl:
        """Каждому адресу — свой ответ. Так видно, кого спросили и в каком
        порядке."""

        def __init__(self, replies):
            self.replies = replies

        def get(self, url, headers=None):
            calls.append(url)
            for mark, reply in self.replies.items():
                if mark in url:
                    return reply
            return Resp({}, status=404)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({}, status=502),
        "gameskinbo": Resp({}, status=429),
        "freefirecommunity": Resp({"nickname": "CommunityMan"}),
    })
    found = await nicknames.free_fire("111222333", key="k", region="BR")
    check("следующий справочник подхватывает",
          found.name == "CommunityMan" and found.verdict == "ok", str(found))
    check("источник назван", found.source == "freefirecommunity", found.source)
    check("очередь идёт сверху вниз",
          "glob-info" in calls[0] and "gameskinbo" in calls[1]
          and "freefirecommunity" in calls[2], str(calls[:3]))
    check("регион ушёл в запрос к gameskinbo",
          "region=BR" in calls[1], calls[1])
    check("источнику без региона его и не шлём",
          "region" not in calls[0], calls[0])
    check("до запасного не дошли",
          not any("onrender" in c for c in calls), str(calls))

    # первый источник ответил — второй не трогаем, лимит не тратим
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({"basicInfo": {"accountId": "444555666",
                                         "nickname": "FirstOne"}}),
        "gameskinbo": Resp({"AccountInfo": {"AccountName": "Второй"}}),
    })
    found = await nicknames.free_fire("444555666", key="k")
    check("первый нашёл — остальных не спрашиваем",
          found.name == "FirstOne", str(found))
    check("лишних запросов нет", len(calls) == 1, str(calls))
    check("месячный лимит при этом цел",
          not any("gameskinbo" in c for c in calls), str(calls))

    # без ключа первого спрашиваем сразу второго
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({}, status=503),
        "freefirecommunity": Resp({"AccountInfo": {"AccountName": "NoKeyMan"}}),
    })
    found = await nicknames.free_fire("777888999")
    check("без ключей ник всё равно находится",
          found.name == "NoKeyMan", str(found))
    check("к источнику с ключом без ключа не ходим",
          not any("gameskinbo" in c for c in calls), str(calls))

    # оба отказали по игроку — это «нет такого»
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({"error": "not found"}),
        "gameskinbo": Resp({}, status=402),
        "freefirecommunity": Resp({}, status=402),
        "onrender": Resp({}, status=404),
    })
    found = await nicknames.free_fire("000111222", key="k")
    check("отказ обоих — «нет такого»", found.verdict == "bad", str(found))

    # 404 у второго справочника двусмысленно: так отвечают и на
    # переехавший адрес
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "freefirecommunity": Resp({}, status=404),
        "onrender": Resp({}, status=404),
    })
    found = await nicknames.free_fire("121212121")
    check("404 не выдаём за «нет игрока»", found.verdict == "unknown",
          str(found))
    check("но очередь всё равно прошли до конца",
          any("freefirecommunity" in c for c in calls), str(calls))

    # один отказал, другой нашёл — верим нашедшему
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({}, status=500),
        "gameskinbo": Resp({}, status=402),
        "freefirecommunity": Resp({"basicInfo": {"nickname": "Живой"}}),
    })
    found = await nicknames.free_fire("333444555", key="k")
    check("нашедший важнее отказавшего",
          found.name == "Живой" and found.verdict == "ok", str(found))

    # ------------------------------- чужой ник не проходит никогда
    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        # сервис ответил про ДРУГОГО игрока — так бывает при путанице
        # с кэшем на бесплатном хостинге
        "glob-info": Resp({"basicInfo": {"accountId": "999999999",
                                         "nickname": "ЧужойИгрок"}}),
        "gameskinbo": Resp({}, status=429),
        "freefirecommunity": Resp({}, status=500),
        "onrender": Resp({}, status=500),
    })
    found = await nicknames.free_fire("123123123")
    check("ник про чужой ID отбрасывается",
          found.name is None, str(found))
    check("и «нет такого» из этого не делаем",
          found.verdict == "unknown", str(found))
    check("чужой ник не попадает в кэш",
          nicknames.cached("123123123") is None)

    # тот же ответ, но ID совпадает — ник берём
    nicknames.forget_all()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({"basicInfo": {"accountId": "123123123",
                                         "nickname": "Свой"}}),
    })
    found = await nicknames.free_fire("123123123")
    check("совпал ID — ник принят", found.name == "Свой", str(found))

    check("ID из ответа достаётся",
          nicknames.pick_id({"basicInfo": {"accountId": "42"}}) == "42")
    check("ID ищется и глубже",
          nicknames.pick_id({"data": {"player": {"uid": 77}}}) == "77")
    check("нечисловой ID за ID не считается",
          nicknames.pick_id({"accountId": "abc"}) is None)
    check("без ID в ответе ник всё равно берём",
          nicknames.trusted_name({"AccountInfo": {"AccountName": "X"}}, "55")
          == "X")
    check("несовпадение ID режет ник",
          nicknames.trusted_name(
              {"basicInfo": {"accountId": "1", "nickname": "X"}}, "2") is None)
    check("совпадение ID ник пропускает",
          nicknames.trusted_name(
              {"basicInfo": {"accountId": "2", "nickname": "X"}}, "2") == "X")

    # ------------------------------- у каждого источника свой список регионов
    check("СНГ понятен запасному источнику",
          nicknames.known_region("CIS", nicknames.FALLBACK_REGIONS) == "CIS")
    check("а первому справочнику — нет",
          nicknames.known_region("CIS") == "")
    check("общий регион понятен обоим",
          nicknames.known_region("BR") == "BR"
          and nicknames.known_region("BR", nicknames.FALLBACK_REGIONS) == "BR")

    nicknames.forget_all()
    calls.clear()
    nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
        "glob-info": Resp({}, status=500),
        "onrender": Resp({"basicInfo": {"accountId": "55", "nickname": "СНГшник"}}),
    })
    found = await nicknames.free_fire("55", region="CIS")
    check("запасному источнику СНГ уходит как есть",
          any("region=CIS" in c for c in calls), str(calls))
    check("и ник оттуда приходит", found.name == "СНГшник", str(found))

    # ник узнаётся в любой обёртке
    check("AccountInfo разбирается",
          nicknames.pick_name({"AccountInfo": {"AccountName": "A"}}) == "A")
    check("basicInfo разбирается",
          nicknames.pick_name({"basicInfo": {"nickname": "B"}}) == "B")
    check("вложенный data разбирается",
          nicknames.pick_name({"data": {"player": {"name": "C"}}}) == "C")
    check("список аккаунтов разбирается",
          nicknames.pick_name({"result": [{"nickname": "D"}]}) == "D")
    check("пустое поле за ник не считается",
          nicknames.pick_name({"AccountInfo": {"AccountName": "  "}}) is None)
    check("чужой ответ не выдумывает ник",
          nicknames.pick_name({"error": "not found"}) is None)

    nicknames.aiohttp.ClientSession = real
    nicknames.forget_all()


# ───────────────────────────────────────────────────── покупка


async def flow(conn) -> None:
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    provider = GameProvider()
    bot = FakeBot()

    await db.upsert_user(conn, BUYER, "gamer", "Игрок")
    await db.credit(conn, BUYER, 200_00, as_deposit=True)
    game = await db.add_game(conn, category_id="free_fire_br",
                             title="🔥 Free Fire", region="BR")
    await db.update_game(conn, game.category_id, enabled=1)
    await db.load_game_titles(conn)
    game = await db.get_game(conn, "free_fire_br")

    check("игра стала товаром", game.product_type == "game:free_fire_br")
    check("у товара человеческое название",
          db.product_title(game.product_type) == "🔥 Free Fire")

    call = call_of("m:games")
    await gh.cb_games(call, state, conn)
    check("раздел игр открывается", "Пур кардани бозиҳо" in call.last)
    check("сказано, что пароль не нужен", "пароли ҳисобро" in call.last.lower())
    check("игра показана кнопкой",
          any("Free Fire" in b for b in buttons(call.markup)),
          str(buttons(call.markup)))

    # Steam и игры — за одной кнопкой меню
    await runtime.set_value(conn, "steam_price_e4", "1400")
    await runtime.set_value(conn, "steam_enabled", "1")
    entries = [b for row in keyboards.main_menu(games=True).inline_keyboard
               for b in row if b.callback_data == "m:games"]
    check("в меню один вход на игры и Steam", len(entries) == 1, str(entries))
    check("на кнопке понятная подпись «Бозиҳо»",
          entries and "Бозиҳо" in entries[0].text,
          entries[0].text if entries else "")
    check("отдельной кнопки Steam в меню нет",
          not any(b.strip().endswith("Пополнить Steam")
                  for b in buttons(keyboards.main_menu(games=True))))

    call = call_of("m:games")
    await gh.cb_games(call, state, conn)
    inner = buttons(call.markup)
    check("внутри раздела есть Steam", any("Steam" in b for b in inner), str(inner))
    check("и обе игры", any("Free Fire" in b for b in inner), str(inner))

    # только Steam, без игр — раздел всё равно открывается
    await db.update_game(conn, "free_fire_br", enabled=0)
    call = call_of("m:games")
    await gh.cb_games(call, state, conn)
    check("с одним Steam раздел работает",
          any("Steam" in b for b in buttons(call.markup)), str(buttons(call.markup)))
    await db.update_game(conn, "free_fire_br", enabled=1)
    await runtime.set_value(conn, "steam_enabled", "0")

    call = call_of("g:free_fire_br")
    await gh.cb_game(call, state, conn, provider)
    check("пакеты показаны", "Маҷмӯаро интихоб кунед" in call.last, call.last[:80])
    labels = buttons(call.markup)
    check("на кнопке пакет и цена в сомони",
          "100 алмазов — 14 с." in labels, str(labels))
    check("дорогой пакет тоже посчитан",
          "500 алмазов — 59 с." in labels, str(labels))
    check("копейки без нужды не пишутся",
          not any(".00" in b for b in labels), str(labels))

    # пакеты идут по две кнопки в ряд — иначе список на два экрана
    game_row = await db.get_game(conn, "free_fire_br")
    grid = keyboards.game_packs(game_row, [
        {"offer_id": f"o{i}", "name": f"{i} алмазов", "price": 1000 + i}
        for i in range(5)
    ])
    widths = [len(row) for row in grid.inline_keyboard[:-1]]
    check("пакеты по два в ряд", widths == [2, 2, 1], str(widths))
    check("кнопка «назад» остаётся своей строкой",
          len(grid.inline_keyboard[-1]) == 1)

    # длинное название в половину ширины не влезает — такому пакету
    # отдаём всю строку, иначе клиент видит обрезок вместо названия
    grid = keyboards.game_packs(game_row, [
        {"offer_id": "o1", "name": "110 алмазов", "price": 900},
        {"offer_id": "o2", "name": "341 алмаз", "price": 2900},
        {"offer_id": "o3", "name": "Еженедельный ваучер", "price": 1700},
        {"offer_id": "o4", "name": "Доступ EVO (3 дня)", "price": 720},
        {"offer_id": "o5", "name": "572 алмаза", "price": 4900},
        {"offer_id": "o6", "name": "1166 алмазов", "price": 9800},
    ])
    rows = [[b.text for b in row] for row in grid.inline_keyboard[:-1]]
    widths = [len(row) for row in rows]
    check("короткие пакеты в паре, длинные на всю строку",
          widths == [2, 1, 1, 2], str(rows))
    check("длинный пакет один в строке",
          rows[1] == ["Еженедельный ваучер — 17 с."], str(rows))
    check("второй длинный тоже один",
          rows[2] == ["Доступ EVO (3 дня) — 7.20 с."], str(rows))
    check("алмазы стоят парой",
          rows[0] == ["110 алмазов — 9 с.", "341 алмаз — 29 с."], str(rows))

    # длинный между двумя короткими не склеивает соседей через себя
    grid = keyboards.game_packs(game_row, [
        {"offer_id": "o1", "name": "Набор новичка, большой", "price": 600},
        {"offer_id": "o2", "name": "110 алмазов", "price": 900},
    ])
    widths = [len(row) for row in grid.inline_keyboard[:-1]]
    check("после длинного короткий остаётся один",
          widths == [1, 1], str(widths))

    call = call_of("gp:free_fire_br:0")
    await gh.cb_pack(call, state, conn)
    check("бот просит ID", "ID-и бозигар" in call.last, call.last[:80])
    check("ждём ID", await state.get_state() == "Game:player")

    bad = msg("abc")
    await gh.on_player_id(bad, state, conn, provider)
    check("нечисловой ID отклонён", "ба ID монанд нест" in bad.last, bad.last[:80])

    good = msg("1724367212")
    await gh.on_player_id(good, state, conn, provider)
    check("ник показан на подтверждение", "Ҳисобро санҷед" in good.last)
    check("виден ник игрока", "Ник" in good.last, good.last[:200])
    check("виден ID", "1724367212" in good.last)
    check("предупреждение о необратимости", "баргардонидани он мумкин нест" in good.last)
    check("ждём подтверждения", await state.get_state() == "Game:confirm")

    call = call_of("g:ok")
    await gh.cb_buy(call, state, conn, provider, bot)

    check("заказ ушёл поставщику", len(provider.orders) == 1, str(provider.orders))
    sent = provider.orders[0]
    check("в заказе верная игра", sent["category_id"] == "free_fire_br")
    check("в заказе верный пакет", sent["offer_id"] == "off_1")
    check("в заказе ID игрока", sent["fields"] == {"user_id": "1724367212"})
    check("ключ идемпотентности передан", len(provider.idempotency) == 1)

    user = await db.get_user(conn, BUYER)
    check("деньги списаны", user.balance == 200_00 - 1400, fmt(user.balance))
    order = (await db.list_orders(conn, user_id=BUYER))[0]
    check("заказ записан как игровой",
          order.product_type == "game:free_fire_br", order.product_type)
    check("номер у поставщика сохранён", order.fragment_order_id == "ord-906267")
    check("себестоимость записана", order.cost == 1090, str(order.cost))
    check("прибыль с игры считается", order.profit == 1400 - 1090, str(order.profit))
    check("заказ ещё в работе", order.status == db.ORDER_DELIVERING, order.status)
    said = [t for t, _ in call.message._log]
    check("клиенту сказано, что заказ принят",
          any("қабул шуд" in t for t in said), str(said[-2:]))
    check("и что нужно подождать", any("камтар аз як дақиқа" in t for t in said),
          str(said[-1:]))

    # ------------------------------------------------ доглядчик доводит до конца
    provider.status = "completed"
    result = await svc.check(bot, conn, provider, order)
    check("доглядчик видит выполнение", result == "done", result)
    order = await db.get_order(conn, order.id)
    check("заказ закрыт как выполненный", order.status == db.ORDER_DELIVERED)
    done = [t for t in bot.to(BUYER) if "иҷро шуд" in t]
    check("клиенту пришло сообщение о выдаче", bool(done), str(bot.to(BUYER)))
    check("в нём ID игрока", done and "1724367212" in done[0])

    # ------------------------------------------------ отказ поставщика
    provider = GameProvider(order_error=DeliveryError("INSUFFICIENT_BALANCE"))
    bot = FakeBot()
    await state.set_state(gh.Game.confirm)
    await state.update_data(category_id="free_fire_br", offer_id="off_1",
                            pack="100 алмазов", price=1400, cost=1090,
                            player="1724367212", player_name="Ник")
    before = (await db.get_user(conn, BUYER)).balance
    call = call_of("g:ok")
    await gh.cb_buy(call, state, conn, provider, bot)
    check("при отказе деньги возвращаются",
          (await db.get_user(conn, BUYER)).balance == before, fmt(before))
    check("владельцу сказали о нехватке баланса",
          any("INSUFFICIENT_BALANCE" in t for t in bot.to(ADMIN)), str(bot.to(ADMIN)))
    check("клиенту сказали о возврате", "иҷро нашуд" in call.last, call.last[:80])

    # ------------------------------------------------ таймаут: не возвращаем, ждём поставщика
    provider = GameProvider(status="processing")
    bot = FakeBot()
    stuck = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, stuck.id, fragment_order_id="ord-old")
    old = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat(timespec="seconds")
    await conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (old, stuck.id))
    await conn.commit()
    stuck = await db.get_order(conn, stuck.id)

    before = (await db.get_user(conn, BUYER)).balance
    result = await svc.check(bot, conn, provider, stuck)
    check("долгий заказ продолжает ждать ответа поставщика", result == "waiting", result)
    check("деньги клиенту сами не возвращаются",
          (await db.get_user(conn, BUYER)).balance == before)
    check("заказ остаётся в обработке",
          (await db.get_order(conn, stuck.id)).status == db.ORDER_DELIVERING)
    warn = [t for t in bot.to(ADMIN) if "долго в обработке" in t]
    check("владельца предупредили", bool(warn), str(bot.to(ADMIN)))
    check("в предупреждении номер у поставщика",
          warn and "ord-old" in warn[0], str(warn[:1]))

    # свежий заказ в обработке не трогаем
    fresh = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1", price=1400, cost=1090,
    )
    await db.update_order(conn, fresh.id, fragment_order_id="ord-new")
    result = await svc.check(bot, conn, provider, await db.get_order(conn, fresh.id))
    check("свежий заказ продолжает ждать", result == "waiting", result)
    check("деньги за него не вернулись",
          (await db.get_order(conn, fresh.id)).status == db.ORDER_DELIVERING)

    pending = await db.unfinished_game_orders(conn)
    check("незавершённые заказы находятся", any(o.id == fresh.id for o in pending))
    check("завершённых среди них нет",
          all(o.status in (db.ORDER_DELIVERING, db.ORDER_FAILED) for o in pending))


# ────────────────────────────────────────────── ID без ника не блокирует


async def unknown_nick(conn) -> None:
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    provider = GameProvider(validate=(None, "unknown"))
    nicknames.forget_all()

    real = nicknames.free_fire

    async def silent(uid, key="", region="", community_key=""):
        return nicknames.Nickname(uid=uid, name=None, verdict="unknown")

    nicknames.free_fire = silent
    try:
        await state.set_state(gh.Game.player)
        await state.update_data(category_id="free_fire_br", offer_id="off_1",
                                pack="100 алмазов", price=1400, cost=1090)
        message = msg("5555555555")
        await gh.on_player_id(message, state, conn, provider)
        check("без ника покупку не блокируем", "ID қабул шуд" in message.last,
              message.last[:80])
        check("но просим проверить ID самому",
              "ID-ро худатон санҷед" in message.last, message.last)
        check("до подтверждения всё равно доходит",
              await state.get_state() == "Game:confirm")

        # явный отказ — другое дело
        provider.validate_reply = (None, "bad")
        await state.set_state(gh.Game.player)
        message = msg("4444444444")
        await gh.on_player_id(message, state, conn, provider)
        # проверка не нашла ID — но покупку это не запирает
        check("непроверенный ID не запирает покупку",
              "тасдиқ нашуд" in message.last, message.last[:80])
        check("и подсказано про регион", "минтақа" in message.last.lower(),
              message.last)
        check("до подтверждения доходит и без проверки",
              await state.get_state() == "Game:confirm")

        # справочник ников говорит «нет такого» — а он бесплатный и
        # знает не все регионы. Его слово покупку рубить не должно.
        async def lying(uid, key="", region="", community_key=""):
            return nicknames.Nickname(uid=uid, name=None, verdict="bad")

        nicknames.free_fire = lying
        provider.validate_reply = (None, "unknown")
        await state.set_state(gh.Game.player)
        await state.update_data(category_id="free_fire_br", offer_id="off_1",
                                pack="100 алмазов", price=1400, cost=1090)
        message = msg("6666666666")
        await gh.on_player_id(message, state, conn, provider)
        check("справочник ников покупку не рубит", "ID қабул шуд" in message.last,
              message.last[:120])
        check("и до подтверждения доходит",
              await state.get_state() == "Game:confirm")
    finally:
        nicknames.free_fire = real


async def full_catalog(conn) -> None:
    """Каталог категорий и каталог проверки ID — разные списки.

    Заказ падал с «Unknown or unavailable category_id» именно потому, что
    владелец видел только второй: в нём перечислены игры с проверкой ID,
    а продаются и остальные.
    """
    class Rich(GameProvider):
        async def game_categories(self):
            return [
                {"category_id": "freefire_br", "name": "Free Fire (BR)",
                 "fields": []},
                {"category_id": "freefire_ru", "name": "Free Fire (RU)",
                 "fields": []},
                {"category_id": "pubgm", "name": "PUBG Mobile", "fields": []},
            ]

        async def game_catalog(self):
            return [{"category_id": "freefire_br", "name": "Free Fire (BR)",
                     "fields": [{"name": "player_id"}]}]

    merged = await svc.full_catalog(Rich())
    codes = {i["category_id"] for i in merged}
    check("видны все категории, не только проверяемые",
          codes == {"freefire_br", "freefire_ru", "pubgm"}, str(codes))
    ff = next(i for i in merged if i["category_id"] == "freefire_br")
    check("поля подтянулись из списка проверки",
          ff["fields"] == [{"name": "player_id"}], str(ff))
    check("проверяемые помечены", ff["checkable"] is True)
    check("непроверяемые помечены тоже",
          next(i for i in merged if i["category_id"] == "pubgm")["checkable"]
          is False)

    # один список отвалился — работаем по второму
    class HalfBroken(Rich):
        async def game_categories(self):
            raise DeliveryError("HTTP 404")

    half = await svc.full_catalog(HalfBroken())
    check("без списка категорий берём проверяемые",
          {i["category_id"] for i in half} == {"freefire_br"}, str(half))

    class OldProvider(GameProvider):
        """Поставщик без нового метода — так было до правки."""

    old_style = await svc.full_catalog(OldProvider())
    check("старый поставщик тоже понятен",
          {i["category_id"] for i in old_style} == {"free_fire_br"},
          str(old_style))

    class Dead(GameProvider):
        async def game_categories(self):
            raise DeliveryError("HTTP 401: ключ не принят")

        async def game_catalog(self):
            raise DeliveryError("HTTP 401: ключ не принят")

    failed = False
    try:
        await svc.full_catalog(Dead())
    except DeliveryError:
        failed = True
    check("полный отказ не выдаём за пустой каталог", failed)


async def catalog_search(conn) -> None:
    """Поиск по каталогу: у поставщика две сотни игр, листать их нельзя."""
    class Catalog(GameProvider):
        async def game_categories(self):
            return [
                {"category_id": "pubg_mobile_global", "name": "PUBG Mobile (Global)"},
                {"category_id": "pubg_mobile_id", "name": "PUBG Mobile (ID)"},
                {"category_id": "pubgm_uc", "name": "PUBG UC"},
                {"category_id": "free_fire_br", "name": "Free Fire (BR)"},
                {"category_id": "mobile_legends_ph", "name": "Mobile Legends (PH)"},
                {"category_id": "roblox", "name": "Roblox"},
            ]

        async def game_catalog(self):
            return await self.game_categories()

    from app.services import games as gsvc

    gsvc.forget_catalog()
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))

    call = call_of("pn:game_find", uid=ADMIN)
    await panel.cb_game_find(call, state)
    check("бот просит название", "Найти игру" in call.last, call.last[:60])
    check("показан пример с pubg", "pubg" in call.last, call.last[:250])

    message = msg("p", uid=ADMIN)
    await panel.on_game_find(message, state, conn, Catalog())
    check("слишком короткий запрос отклонён",
          "две буквы" in message.last, message.last[:80])

    message = msg("pubg", uid=ADMIN)
    await panel.on_game_find(message, state, conn, Catalog())
    check("PUBG найден", "Найдено: 3" in message.last, message.last[:80])
    check("видны коды", "pubg_mobile_global" in message.last, message.last[:400])
    check("и названия", "PUBG Mobile (Global)" in message.last,
          message.last[:400])
    check("чужие игры не попали",
          "roblox" not in message.last and "free_fire" not in message.last,
          message.last[:500])
    check("регион распознан", "Индонезия" in message.last, message.last[:500])
    check("есть кнопка добавления",
          any("Добавить" in b for b in buttons(message.markup)),
          str(buttons(message.markup)))

    # поиск по двум словам, даже если между ними что-то стоит
    message = msg("mobile legends", uid=ADMIN)
    await panel.on_game_find(message, state, conn, Catalog())
    check("ищется по нескольким словам",
          "mobile_legends_ph" in message.last, message.last[:300])

    # уже добавленные помечены
    await db.add_game(conn, category_id="pubgm_uc", title="PUBG UC")
    message = msg("pubg", uid=ADMIN)
    await panel.on_game_find(message, state, conn, Catalog())
    check("добавленная игра помечена", "✅" in message.last, message.last[:400])
    check("не добавленная — тоже помечена", "◻️" in message.last,
          message.last[:400])
    await db.delete_game(conn, "pubgm_uc")

    message = msg("counter strike", uid=ADMIN)
    await panel.on_game_find(message, state, conn, Catalog())
    check("ненайденное названо прямо", "ничего нет" in message.last,
          message.last[:80])
    check("и подсказано, что писать по-английски",
          "по-английски" in message.last, message.last[:300])

    gsvc.forget_catalog()


async def wrong_code(conn) -> None:
    """Неверный код игры: бот подсказывает похожие и переставляет его."""
    class Catalog(GameProvider):
        async def game_categories(self):
            return [
                {"category_id": "freefire_br", "name": "Free Fire (BR)",
                 "fields": [{"name": "player_id"}]},
                {"category_id": "freefire_ru", "name": "Free Fire (RU)",
                 "fields": [{"name": "player_id"}]},
                {"category_id": "roblox", "name": "Roblox", "fields": []},
            ]

        async def game_catalog(self):
            return await self.game_categories()

        async def game_offers(self, category_id):
            if category_id not in ("freefire_br", "freefire_ru", "roblox"):
                raise DeliveryError("Unknown or unavailable category_id.")
            return await super().game_offers(category_id)

    await db.add_game(conn, category_id="free_fire_xx", title="🔥 Free Fire",
                      field="user_id")
    await db.update_game(conn, "free_fire_xx", enabled=1, margin=35)
    await db.set_game_price(conn, "free_fire_xx", "off_1", 2500)
    await db.load_game_titles(conn)

    game = await db.get_game(conn, "free_fire_xx")
    call = call_of("pn:game_packs:free_fire_xx", uid=ADMIN)
    await panel.show_packs(call, conn, game, Catalog())
    check("отказ показан как есть",
          "Unknown or unavailable" in call.last, call.last[:200])
    check("объяснено, что код просто другой",
          "Такого кода у поставщика нет" in call.last, call.last[:300])
    picks = buttons(call.markup)
    check("похожие коды предложены",
          any("freefire_br" in b for b in picks), str(picks))
    check("непохожие не лезут", not any("roblox" in b for b in picks), str(picks))
    check("есть выход на весь список",
          any("Все категории" in b for b in picks), str(picks))

    call = call_of("pn:game_code:free_fire_xx:freefire_ru", uid=ADMIN)
    await panel.cb_game_recode(call, conn, Catalog())
    moved = await db.get_game(conn, "freefire_ru")
    check("игра переставлена на новый код", moved is not None)
    check("старого кода не осталось",
          await db.get_game(conn, "free_fire_xx") is None)
    check("наценка не потерялась", moved and moved.margin == 35, str(moved))
    check("игра осталась в меню", moved and moved.enabled == 1)
    check("поля взяты у поставщика", moved and moved.field == "player_id",
          moved.field if moved else "")
    prices = await db.game_prices(conn, "freefire_ru")
    check("своя цена пакета переехала", prices.get("off_1") == 2500, str(prices))
    check("владельцу показали новый код",
          "freefire_ru" in call.last, call.last[:200])

    # занятый код не занимаем повторно
    await db.add_game(conn, category_id="freefire_br", title="🔥 Free Fire BR",
                      field="player_id")
    call = call_of("pn:game_code:freefire_ru:freefire_br", uid=ADMIN)
    await panel.cb_game_recode(call, conn, Catalog())
    check("на занятый код не переставляем",
          any("уже есть" in a for a in call.alerts), str(call.alerts))

    # клиенту в такой ситуации не говорят «попробуйте позже»
    await db.update_game(conn, "freefire_br", enabled=1)
    await db.add_game(conn, category_id="broken_code", title="🎲 Сломанная",
                      field="user_id")
    await db.update_game(conn, "broken_code", enabled=1)
    await db.load_game_titles(conn)
    gh._told.clear()

    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    bot = FakeBot()
    call = call_of("g:broken_code")
    await gh.cb_game(call, state, conn, Catalog(), bot)
    check("клиенту не обещаем «позже»",
          "позже" not in call.last, call.last[:120])
    told = [t for t in bot.to(ADMIN) if "не открывается" in t]
    check("владельца позвали", bool(told), str(bot.to(ADMIN)))
    check("в сообщении код игры", told and "broken_code" in told[0])
    check("и что делать", told and "Проверить пакеты" in told[0], str(told[:1]))

    bot2 = FakeBot()
    await gh.cb_game(call_of("g:broken_code"), state, conn, Catalog(), bot2)
    check("второй клиент владельца не будит", not bot2.to(ADMIN),
          str(bot2.sent))

    for code in ("broken_code", "freefire_br", "freefire_ru"):
        await db.delete_game(conn, code)
    gh._told.clear()


async def two_fields(conn) -> None:
    """Игры, где аккаунт задан парой: ID игрока и номер сервера."""
    check("одно поле читается как раньше",
          db.Game("x", "X", "player_id", "", 0, 1, "").field_names == ["player_id"])
    check("список полей разбирается",
          db.Game("x", "X", "player_id,server_id", "", 0, 1, "").field_names
          == ["player_id", "server_id"])
    check("пустое поле не роняет покупку",
          db.Game("x", "X", "", "", 0, 1, "").field_names == ["user_id"])

    check("синоним заменяет, а не удваивает",
          svc.with_field("user_id", "player_id") == "player_id",
          svc.with_field("user_id", "player_id"))
    check("второе поле добавляется",
          svc.with_field("player_id", "server_id") == "player_id,server_id",
          svc.with_field("player_id", "server_id"))
    check("ID игрока встаёт первым",
          svc.with_field("server_id", "player_id") == "player_id,server_id",
          svc.with_field("server_id", "player_id"))
    check("уже известное поле ничего не меняет",
          svc.with_field("player_id,server_id", "server_id")
          == "player_id,server_id")
    check("поле сервера названо по-человечески",
          svc.field_label("server_id") == "ID сервера")

    check("два числа разбираются", gh.parse_ids("123456789 1234", 2)
          == ["123456789", "1234"], str(gh.parse_ids("123456789 1234", 2)))
    check("скобки не мешают", gh.parse_ids("123456789 (1234)", 2)
          == ["123456789", "1234"], str(gh.parse_ids("123456789 (1234)", 2)))
    check("одно число вместо двух — отказ",
          gh.parse_ids("123456789", 2) is None)
    check("лишнее число — тоже отказ",
          gh.parse_ids("1 2 3", 2) is None)
    check("короткий ID не принимается", gh.parse_ids("12 1234", 2) is None)
    check("в заказе видны оба числа",
          gh.shown_id(["123456789", "1234"]) == "123456789 (1234)",
          gh.shown_id(["123456789", "1234"]))

    class TwoField(GameProvider):
        async def game_catalog(self):
            return [{"category_id": "magic_chess_ru", "name": "Magic Chess (RU)",
                     "fields": [{"name": "player_id"}, {"name": "server_id"}]}]

        async def validate_game_id(self, category_id, fields):
            if set(fields) != {"player_id", "server_id"}:
                raise DeliveryError('Field "server_id" is required.')
            return "ChessMan", "ok"

        async def order_game(self, **kw):
            if set(kw["fields"]) != {"player_id", "server_id"}:
                raise DeliveryError('Field "server_id" is required.')
            return await super().order_game(**kw)

    provider = TwoField()
    check("оба поля читаются из каталога",
          await svc.detect_fields(provider, "magic_chess_ru")
          == ["player_id", "server_id"],
          str(await svc.detect_fields(provider, "magic_chess_ru")))

    await db.add_game(conn, category_id="magic_chess_ru",
                      title="Magic Chess Go Go (RU)", field="player_id,server_id")
    await db.update_game(conn, "magic_chess_ru", enabled=1)
    await db.load_game_titles(conn)

    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    call = call_of("gp:magic_chess_ru:0")
    _offers_backup = dict(gh._offers)
    await gh.cb_game(call_of("g:magic_chess_ru"), state, conn, provider)
    await gh.cb_pack(call, state, conn)
    check("бот просит оба числа", "ID сервера" in call.last, call.last[:200])
    check("и показывает пример", "123456789 1234" in call.last, call.last[:250])

    message = msg("123456789")
    await gh.on_player_id(message, state, conn, provider)
    check("одного числа мало", "ду рақам лозим аст" in message.last.lower(),
          message.last[:120])

    message = msg("123456789 (1234)")
    await gh.on_player_id(message, state, conn, provider)
    check("пара принята", "Ҳисобро санҷед" in message.last, message.last[:120])
    check("ник получен по паре", "ChessMan" in message.last, message.last[:200])
    check("в подтверждении видны оба числа",
          "123456789 (1234)" in message.last, message.last[:200])

    bot = FakeBot()
    call = call_of("g:ok")
    await gh.cb_buy(call, state, conn, provider, bot)
    check("в заказ ушли оба поля",
          provider.orders and provider.orders[0]["fields"]
          == {"player_id": "123456789", "server_id": "1234"},
          str(provider.orders))

    order = (await db.last_game_orders(conn))[0]
    check("в заказе записаны оба числа",
          order.recipient == "123456789 (1234)", order.recipient)

    # отказ «нужно ещё одно поле» чинится добавлением, а не заменой
    await db.update_game(conn, "magic_chess_ru", field="player_id")
    bot = FakeBot()
    await state.set_state(gh.Game.confirm)
    await state.update_data(category_id="magic_chess_ru", offer_id="off_1",
                            pack="100 алмазов", price=1400, cost=1090,
                            player="123456789", player_name="ChessMan",
                            fields={"player_id": "123456789"})
    await gh.cb_buy(call_of("g:ok"), state, conn, provider, bot)
    fixed = await db.get_game(conn, "magic_chess_ru")
    check("недостающее поле добавлено, прежнее цело",
          fixed.field == "player_id,server_id", fixed.field)
    told = [t for t in bot.to(ADMIN) if "Поля исправлены" in t]
    check("владельцу объяснили по-человечески",
          told and "ID игрока, ID сервера" in told[0], str(told[:1]))

    await db.delete_game(conn, "magic_chess_ru")
    gh._offers.clear()
    gh._offers.update(_offers_backup)


async def volsever_check(conn) -> None:
    """Volsever: проверка ID и ника перед покупкой, для любых игр."""
    from app.services import volsever

    calls: list[str] = []

    class Resp:
        def __init__(self, payload, status=200):
            self.payload, self.status = payload, status

        async def json(self, content_type=None):
            return self.payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, reply):
            self.reply = reply

        def get(self, url, headers=None):
            calls.append(url)
            self.headers = headers
            return self.reply

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    real = volsever.aiohttp.ClientSession
    try:
        # ---- обычная проверка
        volsever.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({
            "status": True, "code": 200,
            "data": {"game": "free-fire-asia", "username": "ProPlayer",
                     "user_id": "1724367212"},
        }))
        name, verdict, note = await volsever.check(
            "pk_test", "free-fire-asia", "1724367212")
        check("ник получен", name == "ProPlayer" and verdict == "ok", str(name))
        check("код игры попал в адрес",
              "game/free-fire-asia" in calls[-1], calls[-1])
        check("ID попал в адрес", "id=1724367212" in calls[-1], calls[-1])
        check("без сервера лишнего параметра нет",
              "zone=" not in calls[-1], calls[-1])

        # ---- игра с сервером: второе число уходит как зона
        await volsever.check("pk_test", "magic-chess", "123456789", "1234")
        check("номер сервера уходит отдельно",
              "zone=1234" in calls[-1], calls[-1])

        # ---- сервис прямо говорит «нет такого»
        volsever.aiohttp.ClientSession = lambda *a, **kw: Session(
            Resp({"status": False, "message": "Account not found"}, status=404))
        name, verdict, note = await volsever.check("pk_test", "g", "1")
        check("отказ по аккаунту распознан", verdict == "bad", verdict)
        check("и причина сохранена", "not found" in note.lower(), note)

        # ---- поломки сервиса покупку не рубят
        for status, mark in ((401, "ключ"), (429, "лимит"), (500, "HTTP 500")):
            volsever.aiohttp.ClientSession = lambda *a, s=status, **kw: Session(
                Resp({}, status=s))
            name, verdict, note = await volsever.check("pk_test", "g", "1")
            check(f"HTTP {status} — не «нет игрока»", verdict == "unknown",
                  f"{verdict} / {note}")
            check(f"причина HTTP {status} названа", mark in note, note)

        # ---- чужой ник не пропускаем
        volsever.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({
            "data": {"username": "Чужой", "user_id": "999"}}))
        name, verdict, note = await volsever.check("pk_test", "g", "111")
        check("ответ про чужой ID отброшен", name is None, str(name))
        check("и это не «нет игрока»", verdict == "unknown", verdict)
        check("в пояснении виден чужой ID", "999" in note, note)

        # ---- без ключа не ходим никуда
        before = len(calls)
        name, verdict, note = await volsever.check("", "g", "1")
        check("без ключа запрос не уходит", len(calls) == before, str(calls[-1:]))
        check("и сказано почему", "ключ" in note, note)

        # ---- список игр
        volsever.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({
            "data": [
                {"code": "free-fire-asia", "name": "Free Fire Asia"},
                {"code": "magic-chess", "name": "Magic Chess", "zone": True},
            ]}))
        catalog = await volsever.games("pk_test")
        check("список игр разобран", len(catalog) == 2, str(catalog))
        check("видно, где нужен сервер",
              catalog[1]["zone"] is True and catalog[0]["zone"] is False,
              str(catalog))

        # ---- привязка игры к коду проверки
        await db.add_game(conn, category_id="free_fire_br",
                          title="🔥 Free Fire", field="user_id")
        await db.update_game(conn, "free_fire_br", checker="")
        game = await db.get_game(conn, "free_fire_br")
        check("новая игра без проверки", game.checker == "", game.checker)

        match = panel._best_match(game, catalog)
        check("код подобрался по названию",
              match and match["code"] == "free-fire-asia", str(match))

        await db.update_game(conn, "free_fire_br", checker="free-fire-asia")
        game = await db.get_game(conn, "free_fire_br")
        check("код сохранился", game.checker == "free-fire-asia", game.checker)

        # ---- и он же используется при покупке
        await runtime.set_value(conn, "volsever_key", "pk_test")
        volsever.aiohttp.ClientSession = lambda *a, **kw: Session(Resp({
            "data": {"username": "ИзVolsever", "user_id": "1724367212"}}))
        name, verdict = await gh._lookup(
            GameProvider(validate=(None, "unknown")), game,
            {"user_id": "1724367212"})
        check("при покупке ник берётся у Volsever",
              name == "ИзVolsever" and verdict == "ok", f"{name} / {verdict}")

        # без привязки игры к коду — молчит и не мешает
        await db.update_game(conn, "free_fire_br", checker="")
        bare = await db.get_game(conn, "free_fire_br")
        name, verdict = await gh._volsever(bare, {"user_id": "1"})
        check("без привязки проверка пропускается", verdict == "unknown",
              verdict)

        await runtime.set_value(conn, "volsever_key", "")
        await db.update_game(conn, "free_fire_br", checker="")
    finally:
        volsever.aiohttp.ClientSession = real


async def nick_probe(conn) -> None:
    """/nick — опрос всех источников разом: видно, кто врёт и кто молчит."""
    from aiogram.filters import CommandObject

    from app.handlers import admin

    real = nicknames.aiohttp.ClientSession
    nicknames.forget_all()

    class Resp:
        def __init__(self, payload, status=200):
            self.payload, self.status = payload, status

        async def json(self, content_type=None):
            return self.payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class ByUrl:
        def __init__(self, replies):
            self.replies = replies

        def get(self, url, headers=None):
            for mark, reply in self.replies.items():
                if mark in url:
                    return reply
            return Resp({}, status=404)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def run(args: str):
        message = msg("/nick " + args, uid=ADMIN)
        await admin.cmd_nick(message, CommandObject(command="nick", args=args))
        return message

    try:
        told = await run("")
        check("без ID показана подсказка", "Использование" in told.last,
              told.last[:60])

        # все согласны
        nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
            "glob-info": Resp({"basicInfo": {"accountId": "777", "nickname": "Один"}}),
            "freefirecommunity": Resp({"nickname": "Один"}),
            "onrender": Resp({"basicInfo": {"accountId": "777", "nickname": "Один"}}),
        })
        told = await run("777")
        check("ник показан", "Один" in told.last, told.last[:200])
        check("сказано, сколько источников согласны",
              "Согласны источников" in told.last, told.last)
        check("вывод — ник верный", "Ник: Один" in told.last, told.last[-200:])
        check("видно, кто из источников ответил",
              "glob-info" in told.last, told.last)
        check("без ключа источник помечен отдельно",
              "ключ не задан" in told.last, told.last)

        # источники разошлись — это тревога
        nicknames.forget_all()
        nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
            "glob-info": Resp({"basicInfo": {"accountId": "777", "nickname": "Первый"}}),
            "freefirecommunity": Resp({"nickname": "Второй"}),
            "onrender": Resp({}, status=500),
        })
        told = await run("777")
        check("расхождение замечено", "не согласны" in told.last, told.last[-300:])
        check("и сказано, что делать",
              "разработчику" in told.last, told.last[-300:])

        # никто не ответил
        nicknames.forget_all()
        nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({})
        told = await run("777")
        check("молчание всех источников названо прямо",
              "не узнал никто" in told.last, told.last[-300:])
        check("у каждого написана причина",
              told.last.count("HTTP 404") >= 2, told.last)
        check("подсказано, что проверить",
              "ID не существует" in told.last, told.last[-300:])

        # связь оборвалась совсем — это другая беда, и лечится иначе
        class Dead:
            def get(self, url, headers=None):
                raise OSError("Cannot connect to host")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        nicknames.forget_all()
        nicknames.aiohttp.ClientSession = lambda *a, **kw: Dead()
        told = await run("777")
        check("обрыв связи отличается от «нет игрока»",
              "не отозвался" in told.last, told.last[-400:])
        check("названа вероятная причина",
              "выход в интернет" in told.last, told.last[-400:])
        check("дана команда для проверки на сервере",
              "curl" in told.last, told.last[-400:])
        check("видна и сама ошибка связи",
              "OSError" in told.last or "Cannot connect" in told.last,
              told.last[:400])

        # кэш не должен подменять проверку
        nicknames.remember("777", "ИзКэша")
        nicknames.aiohttp.ClientSession = lambda *a, **kw: ByUrl({
            "glob-info": Resp({"basicInfo": {"accountId": "777", "nickname": "Свежий"}}),
        })
        told = await run("777")
        check("проверка не берёт ник из кэша",
              "ИзКэша" not in told.last and "Свежий" in told.last,
              told.last[:200])

        check("команда есть в справке админа", "/nick" in texts.ADMIN_HELP)
    finally:
        nicknames.aiohttp.ClientSession = real
        nicknames.forget_all()


async def verdict_reading(conn) -> None:
    """Отказ поставщика читается по смыслу: «нет игрока» ≠ «нет категории»."""
    from app.services.fazer import FazerProvider

    client = FazerProvider(api_key="x")

    async def raising(*args, **kw):
        raise DeliveryError(raising.text)

    client._request = raising
    cases = [
        ("Invalid player id", "bad", "неверный ID — это отказ"),
        ("Player not found", "bad", "не найден игрок — это отказ"),
        ("Invalid category_id", "unknown", "неизвестная категория — не про игрока"),
        ('Field "player_id" is required', "unknown", "нехватка поля — не про игрока"),
        ("Validation unsupported for this category", "unknown",
         "проверка не поддерживается — не про игрока"),
        ("Rate limit exceeded", "unknown", "превышен лимит — не про игрока"),
        ("Internal server error", "unknown", "поломка сервиса — не про игрока"),
    ]
    for text, want, name in cases:
        raising.text = text
        _, verdict = await client.validate_game_id("free_fire_br", {"user_id": "1"})
        check(name, verdict == want, f"{text} → {verdict}")
    await client.close()


async def wrong_region(conn) -> None:
    """ID не нашёлся в выбранном регионе — ищем его в остальных."""
    real = nicknames.free_fire

    async def silent(uid, key="", region="", community_key=""):
        return nicknames.Nickname(uid=uid, name=None, verdict="unknown")

    nicknames.free_fire = silent
    nicknames.forget_all()

    from app.services import regions as reg

    for code in ("free_fire_cis", "free_fire_id"):
        await db.add_game(conn, category_id=code, title="🔥 Free Fire",
                          field="user_id", region=reg.nick_region(code))
        await db.update_game(conn, code, enabled=1)
    await db.load_game_titles(conn)

    class OnlyBrazil(GameProvider):
        """Игрок живёт в Бразилии, а клиент выбрал СНГ."""

        def __init__(self):
            super().__init__()
            self.asked: list[str] = []

        async def validate_game_id(self, category_id, fields):
            self.asked.append(category_id)
            if category_id == "free_fire_br":
                return "BrPlayer", "ok"
            return None, "bad"

    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    provider = OnlyBrazil()
    try:
        await state.set_state(gh.Game.player)
        await state.update_data(category_id="free_fire_cis", offer_id="off_1",
                                pack="100 алмазов", price=1400, cost=1090)
        message = msg("1234567890")
        await gh.on_player_id(message, state, conn, provider)

        check("бот сам обошёл остальные регионы",
              "free_fire_br" in provider.asked, str(provider.asked))
        check("клиенту сказали, где аккаунт нашёлся",
              "минтақаи дигар" in message.last, message.last[:120])
        check("назван нужный регион", "Бразилия" in message.last, message.last)
        check("и ник оттуда", "BrPlayer" in message.last, message.last)
        check("кнопка ведёт прямо в этот регион",
              any(b.callback_data == "g:free_fire_br"
                  for row in message.markup.inline_keyboard for b in row),
              str(message.markup))

        # нигде не нашёлся — тогда честный отказ, но с выходом
        class Nowhere(GameProvider):
            async def validate_game_id(self, category_id, fields):
                return None, "bad"

        await state.set_state(gh.Game.player)
        message = msg("1234567890")
        await gh.on_player_id(message, state, conn, Nowhere())
        check("если нигде нет — честно предупреждаем",
              "тасдиқ нашуд" in message.last, message.last[:80])
        check("но купить всё равно даём",
              any(b.callback_data == "g:ok"
                  for row in message.markup.inline_keyboard for b in row),
              str(message.markup))
        check("и даём сменить регион",
              any(b.callback_data == "gf:free_fire"
                  for row in message.markup.inline_keyboard for b in row),
              str(message.markup))
        check("сумма списания показана", "Барои пардохт" in message.last,
              message.last[:200])

        # и покупка после этого действительно проходит
        bot = FakeBot()
        call = call_of("g:ok")
        seller = Nowhere()
        await gh.cb_buy(call, state, conn, seller, bot)
        check("покупка без подтверждения ID доходит до поставщика",
              len(seller.orders) == 1, str(seller.orders))
        check("в заказ ушёл именно введённый ID",
              seller.orders and seller.orders[0]["fields"]
              == {"user_id": "1234567890"}, str(seller.orders))

        # у игры один регион — кнопки смены не предлагаем
        await db.update_game(conn, "free_fire_cis", enabled=0)
        await db.update_game(conn, "free_fire_id", enabled=0)
        await db.update_game(conn, "free_fire_br", enabled=0)
        await db.add_game(conn, category_id="solo_game", title="🎲 Solo",
                          field="user_id")
        await db.update_game(conn, "solo_game", enabled=1)
        await state.set_state(gh.Game.player)
        await state.update_data(category_id="solo_game", offer_id="off_1",
                                pack="100 алмазов", price=1400, cost=1090)
        message = msg("1234567890")
        await gh.on_player_id(message, state, conn, Nowhere())
        check("у игры без регионов лишней кнопки нет",
              not any(b.callback_data.startswith("gf:")
                      for row in message.markup.inline_keyboard for b in row),
              str(message.markup))
    finally:
        nicknames.free_fire = real
        await db.delete_game(conn, "solo_game")
        for code in ("free_fire_br", "free_fire_cis", "free_fire_id"):
            await db.update_game(conn, code, enabled=1)


# ─────────────────────────────────────────────────────────── панель


async def panel_screens(conn) -> None:
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    call = call_of("pn:games", uid=ADMIN)
    await panel.cb_games(call, state, conn)
    check("раздел игр в панели открывается", "Игры" in call.last)
    check("игра видна", "Free Fire" in call.last)
    check("есть кнопка добавления", "➕ Добавить игру" in buttons(call.markup))

    call = call_of("pn:game_new", uid=ADMIN)
    await panel.cb_game_new(call, state)
    check("просит код и название", "код название" in call.last, call.last[:120])

    bad = msg("ЕРУНДА", uid=ADMIN)
    await panel.on_game_new(bad, state, conn, GameProvider())
    check("кривой формат отклонён", "❌" in bad.last)

    good = msg("pubg_mobile 🎯 PUBG Mobile", uid=ADMIN)
    await panel.on_game_new(good, state, conn, GameProvider())
    pubg = await db.get_game(conn, "pubg_mobile")
    check("вторая игра добавлена", pubg is not None and pubg.title == "🎯 PUBG Mobile")
    check("новая игра сразу не продаётся", pubg.enabled == 0)
    check("незнакомой игре ставится поле по умолчанию",
          pubg.field == "user_id", pubg.field)

    call = call_of("pn:game_field:free_fire_br", uid=ADMIN)
    await panel.cb_game_field(call, state, conn, GameProvider())
    check("экран поля показывает текущее", "player_id" in call.last, call.last[:200])
    await panel.on_field_value(msg("uid", uid=ADMIN), state, conn)
    check("поле правится вручную",
          (await db.get_game(conn, "free_fire_br")).field == "uid")
    await db.update_game(conn, "free_fire_br", field="player_id")

    call = call_of("pn:game_on:pubg_mobile", uid=ADMIN)
    await panel.cb_game_toggle(call, conn)
    check("игра включается", (await db.get_game(conn, "pubg_mobile")).enabled == 1)

    call = call_of("pn:game_packs:free_fire_br", uid=ADMIN)
    await panel.cb_game_offers(call, conn, GameProvider())
    check("пакеты показаны владельцу", "100 алмазов" in call.last, call.last[:200])
    check("рядом видна себестоимость", "себест." in call.last, call.last[:250])

    call = call_of("pn:game_margin:free_fire_br", uid=ADMIN)
    await panel.cb_game_margin(call, state, conn)
    await panel.on_field_value(msg("35", uid=ADMIN), state, conn)
    check("своя наценка сохранена",
          (await db.get_game(conn, "free_fire_br")).margin == 35)

    call = call_of("pn:game_del:pubg_mobile", uid=ADMIN)
    await panel.cb_game_delete(call, conn)
    check("игра удаляется", await db.get_game(conn, "pubg_mobile") is None)

    check("раздел есть в главном меню панели",
          any("Игры" in b.text for r in panel.home_kb().inline_keyboard for b in r))
    check("кнопка балансов ключей есть",
          any("Балансы ключей" in b.text
              for r in panel.home_kb().inline_keyboard for b in r))

    await no_supplier_number(conn)
    await fast_follow(conn)
    await manual_prices(conn)
    await id_field(conn)
    await two_keys(conn)
    await key_balances(conn)

    # игру можно закрепить за партнёром
    partner = await db.create_partner(conn, "Напарник", 0)
    await db.set_product_owner(conn, "game:free_fire_br", partner.id)
    rows = await db.sales_by_product(conn)
    ff = next((r for r in rows if r["product_type"] == "game:free_fire_br"), None)
    check("продажи игры видны отдельным товаром", ff is not None, str(rows))
    check("у него название игры", ff and ff["title"] == "🔥 Free Fire")
    check("игра закреплена за партнёром",
          (await db.product_owners(conn)).get("game:free_fire_br") == partner.id)


async def no_supplier_number(conn) -> None:
    """Заказ без номера у поставщика зовёт владельца сразу."""
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))

    class Nameless(GameProvider):
        async def order_game(self, **kw):
            return {"status": "processing"}       # номера нет

    bot = FakeBot()
    await state.set_state(gh.Game.confirm)
    await state.update_data(category_id="free_fire_br", offer_id="off_1",
                            pack="100 алмазов", price=1400, cost=1090,
                            player="1724367212", player_name="Ник")
    await gh.cb_buy(call_of("g:ok"), state, conn, Nameless(), bot)

    told = [t for t in bot.to(ADMIN) if "без номера" in t]
    check("владельцу сказали сразу", bool(told), str(bot.to(ADMIN)))
    check("в сообщении есть ID игрока", told and "1724367212" in told[0])
    check("подсказаны команды", told and "/done" in told[0] and "/refund" in told[0])
    check("сказано, что без решения заказ так и ждёт",
          told and "останется в обработке" in told[0], str(told[:1]))

    order = (await db.last_game_orders(conn))[0]
    check("заказ помечен зависшим", order.status == db.ORDER_FAILED, order.status)
    check("номера у поставщика и правда нет", not order.fragment_order_id)

    waiting = await svc.check(bot, conn, GameProvider(), order)
    check("без номера доглядчик не выдумывает статус", waiting == "waiting", waiting)


async def fast_follow(conn) -> None:
    """Свежий заказ опрашивается часто, а не раз в пять минут."""
    check("быстрый опрос чаще общего обхода",
          svc.FAST_EVERY < svc.WATCH_EVERY, f"{svc.FAST_EVERY} < {svc.WATCH_EVERY}")
    check("быстрый опрос короче таймаута возврата",
          svc.FAST_SECONDS < svc.TIMEOUT_MINUTES * 60)

    bot = FakeBot()
    provider = GameProvider(status="processing")
    order = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, order.id, fragment_order_id="ord-fast")

    # выдача происходит между опросами
    real_sleep = svc.asyncio.sleep
    ticks = {"n": 0}

    async def tick(_seconds):
        ticks["n"] += 1
        if ticks["n"] == 2:
            provider.status = "completed"
        await real_sleep(0)

    svc.asyncio.sleep = tick
    try:
        result = await svc.follow(bot, provider, order.id)
    finally:
        svc.asyncio.sleep = real_sleep

    check("быстрый опрос доводит заказ до конца", result == "done", result)
    check("хватило пары проверок", ticks["n"] <= 3, str(ticks["n"]))
    check("заказ закрыт",
          (await db.get_order(conn, order.id)).status == db.ORDER_DELIVERED)
    check("клиент получил сообщение сразу",
          any("иҷро шуд" in t for t in bot.to(BUYER)), str(bot.to(BUYER)))

    # уже закрытый заказ опрашивать незачем
    ticks["n"] = 0
    svc.asyncio.sleep = tick
    try:
        result = await svc.follow(bot, provider, order.id)
    finally:
        svc.asyncio.sleep = real_sleep
    check("закрытый заказ бросается сразу", result == "done" and ticks["n"] == 1,
          f"{result} за {ticks['n']}")


async def manual_prices(conn) -> None:
    """Цену каждого пакета можно поставить руками."""
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    provider = GameProvider()
    # Наценку фиксируем: раньше её меняли, и цифры зависели бы от порядка.
    await db.update_game(conn, "free_fire_br", margin=20)
    game = await db.get_game(conn, "free_fire_br")

    call = call_of("pn:game_packs:free_fire_br", uid=ADMIN)
    await panel.cb_game_offers(call, conn, provider)
    check("пакеты стали кнопками",
          any("100 алмазов" in b for b in buttons(call.markup)),
          str(buttons(call.markup)))
    check("сказано, что можно поставить свою цену",
          "свою цену" in call.last, call.last[-200:])

    call = call_of("pn:pk:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_card(call, conn)
    check("карточка пакета открывается", "100 алмазов" in call.last, call.last[:80])
    check("видна себестоимость", "Себестоимость" in call.last)
    check("видна прибыль в процентах", "Прибыль" in call.last and "%" in call.last)
    check("пока цена по наценке", "(по наценке)" in call.last, call.last[:300])
    check("кнопки возврата к наценке ещё нет",
          not any("Вернуть по наценке" in b for b in buttons(call.markup)))

    call = call_of("pn:pkset:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_price(call, state, conn)
    check("просит цену", "Пришлите цену" in call.last, call.last[-150:])

    bad = msg("дорого", uid=ADMIN)
    await panel.on_field_value(bad, state, conn)
    check("нечисловая цена отклонена", "❌" in bad.last)

    good = msg("25", uid=ADMIN)
    await panel.on_field_value(good, state, conn)
    prices = await db.game_prices(conn, "free_fire_br")
    check("своя цена сохранена", prices.get("off_1") == 25_00, str(prices))
    check("сказано, что курс её не тронет", "не трогает" in good.last, good.last)

    from app.handlers.games import offers_of

    offers = await offers_of(provider, game, conn)
    first = offers[0]
    check("клиент увидит свою цену", first["price"] == 25_00, str(first["price"]))
    check("пакет помечен как ручной", first["manual"] is True)
    check("расчётная цена рядом сохранена", first["auto"] == 1400, str(first["auto"]))
    check("остальные пакеты считаются как раньше",
          offers[1]["price"] == offers[1]["auto"] and not offers[1]["manual"])

    # смена курса не трогает ручную цену
    await runtime.set_value(conn, "usd_rate_diram", "2000")
    offers = await offers_of(provider, game, conn)
    check("при смене курса своя цена держится",
          offers[0]["price"] == 25_00, str(offers[0]["price"]))
    check("а расчётная меняется", offers[0]["auto"] != 1400, str(offers[0]["auto"]))
    await runtime.set_value(conn, "usd_rate_diram", "1090")

    call = call_of("pn:pk:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_card(call, conn)
    check("в карточке видно, что цена своя", "(своя)" in call.last, call.last[:300])
    check("показано, сколько было бы по наценке",
          "По наценке было бы" in call.last, call.last[:400])
    check("появилась кнопка возврата",
          any("Вернуть как у поставщика" in b for b in buttons(call.markup)),
          str(buttons(call.markup)))

    call = call_of("pn:pkauto:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_reset(call, conn, provider)
    check("цена вернулась к расчёту",
          await db.game_prices(conn, "free_fire_br") == {},
          str(await db.game_prices(conn, "free_fire_br")))
    offers = await offers_of(provider, game, conn)
    check("и клиент снова видит расчётную", offers[0]["price"] == 1400,
          str(offers[0]["price"]))

    # ---------------------------------------- своё название пакета
    call = call_of("pn:pkname:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_rename(call, state, conn)
    check("бот просит название", "Название пакета" in call.last, call.last[:80])
    check("показано название поставщика",
          "100 алмазов" in call.last, call.last[:200])

    message = msg("💎 Сто алмазов", uid=ADMIN)
    await panel.on_field_value(message, state, conn)
    check("название сохранено", "Сто алмазов" in message.last, message.last[:120])

    offers = await offers_of(provider, game, conn)
    check("клиент видит своё название",
          offers[0]["name"] == "💎 Сто алмазов", offers[0]["name"])
    check("название поставщика сохранено рядом",
          offers[0]["supplier_name"] == "100 алмазов", offers[0]["supplier_name"])
    check("пакет помечен переименованным", offers[0]["renamed"] is True)

    packs = buttons(keyboards.game_packs(game, offers))
    check("и на кнопке покупки оно же",
          any("Сто алмазов" in b for b in packs), str(packs))

    # ---------------------------------------- убрать пакет из продажи
    call = call_of("pn:pkhide:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_visibility(call, conn, provider)
    offers = await offers_of(provider, game, conn)
    check("клиенту скрытый пакет не показывается",
          all(o["offer_id"] != "off_1" for o in offers), str(offers))
    check("другие пакеты на месте", len(offers) == 1, str(len(offers)))

    owner_view = await offers_of(provider, game, conn, for_owner=True)
    check("владелец скрытый пакет видит", len(owner_view) == 2,
          str(len(owner_view)))
    check("и он помечен скрытым",
          any(o["hidden"] for o in owner_view), str(owner_view))

    call = call_of("pn:pk:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_card(call, conn)
    check("в карточке сказано, что убран из продажи",
          "убран из продажи" in call.last.lower(), call.last[:400])
    check("и есть кнопка вернуть",
          any("Вернуть в продажу" in b for b in buttons(call.markup)),
          str(buttons(call.markup)))

    call = call_of("pn:pkshow:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_visibility(call, conn, provider)
    offers = await offers_of(provider, game, conn)
    check("вернули — снова виден", len(offers) == 2, str(len(offers)))
    check("название при этом не потерялось",
          offers[0]["name"] == "💎 Сто алмазов", offers[0]["name"])

    # скрытие и своя цена живут вместе
    await db.set_game_price(conn, "free_fire_br", "off_1", 2000)
    await db.set_game_offer_hidden(conn, "free_fire_br", "off_1", True)
    setup = await db.game_offers_setup(conn, "free_fire_br")
    check("цена, название и скрытие не мешают друг другу",
          setup["off_1"] == {"price": 2000, "title": "💎 Сто алмазов",
                             "hidden": True}, str(setup.get("off_1")))

    # ---------------------------------------- полный сброс
    call = call_of("pn:pkauto:free_fire_br:0", uid=ADMIN)
    await panel.cb_pack_reset(call, conn, provider)
    owner_view = await offers_of(provider, game, conn, for_owner=True)
    first = next(o for o in owner_view if o["offer_id"] == "off_1")
    check("сброс вернул название поставщика",
          first["name"] == "100 алмазов", first["name"])
    check("и расчётную цену", first["manual"] is False)
    check("но из продажи не вернул сам",
          first["hidden"] is True, str(first["hidden"]))

    await db.set_game_offer_hidden(conn, "free_fire_br", "off_1", False)

    # удаление игры уносит её цены
    await db.set_game_price(conn, "free_fire_br", "off_1", 30_00)
    await db.delete_game(conn, "free_fire_br")
    check("цены удалённой игры не остаются",
          await db.game_prices(conn, "free_fire_br") == {})
    await db.add_game(conn, category_id="free_fire_br", title="🔥 Free Fire",
                      field="player_id", region="BR")
    await db.update_game(conn, "free_fire_br", enabled=1)
    await db.load_game_titles(conn)


async def id_field(conn) -> None:
    """Поле с ID у каждой игры своё — бот его узнаёт, а не угадывает."""
    check("имя поля читается из отказа",
          svc.missing_field('Field "player_id" is required.') == "player_id")
    check("другой формулировки тоже хватает",
          svc.missing_field("field uid is required") == "uid")
    check("посторонний отказ полем не считается",
          svc.missing_field("INSUFFICIENT_BALANCE") is None)

    provider = GameProvider()
    check("поле определяется по каталогу поставщика",
          await svc.detect_field(provider, "free_fire_br") == "player_id")
    check("для незнакомой игры поля нет",
          await svc.detect_field(provider, "нет_такой") is None)

    class NoCatalog(DeliveryProvider):
        async def game_catalog(self):
            raise DeliveryError("503")

    check("молчащий каталог не роняет",
          await svc.detect_field(NoCatalog(), "free_fire_br") is None)

    # отказ про поле чинит игру сам
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    await db.update_game(conn, "free_fire_br", field="user_id")
    broken = GameProvider(order_error=DeliveryError('Field "player_id" is required.'))
    bot = FakeBot()

    await state.set_state(gh.Game.confirm)
    await state.update_data(category_id="free_fire_br", offer_id="off_1",
                            pack="100 алмазов", price=1400, cost=1090,
                            player="1724367212", player_name="Ник")
    before = (await db.get_user(conn, BUYER)).balance
    call = call_of("g:ok")
    await gh.cb_buy(call, state, conn, broken, bot)

    check("деньги за неудачный заказ вернулись",
          (await db.get_user(conn, BUYER)).balance == before, fmt(before))
    check("поле игры исправлено на нужное",
          (await db.get_game(conn, "free_fire_br")).field == "player_id",
          (await db.get_game(conn, "free_fire_br")).field)
    told = [t for t in bot.to(ADMIN) if "Поля исправлены" in t]
    check("владельцу сказали, что починено", bool(told), str(bot.to(ADMIN)))
    check("и что можно повторить", told and "повторить" in told[0])

    # следующий заказ уходит уже с правильным полем
    ok = GameProvider()
    await state.set_state(gh.Game.confirm)
    await state.update_data(category_id="free_fire_br", offer_id="off_1",
                            pack="100 алмазов", price=1400, cost=1090,
                            player="1724367212", player_name="Ник")
    await gh.cb_buy(call_of("g:ok"), state, conn, ok, FakeBot())
    check("повторный заказ ушёл с правильным полем",
          ok.orders and ok.orders[0]["fields"] == {"player_id": "1724367212"},
          str(ok.orders))


async def two_keys(conn) -> None:
    """Игры идут с ключа напарника, звёзды — с основного."""
    from app.services import suppliers

    suppliers.forget()
    await runtime.set_value(conn, "fazer_games_key", "")
    main = GameProvider()
    check("без своего ключа игры идут с основного счёта",
          suppliers.for_games(main) is main)
    check("отдельного ключа нет", suppliers.has_own_games_key() is False)

    await runtime.set_value(conn, "fazer_games_key", "fc_partner_key")
    check("со своим ключом это другой клиент",
          suppliers.for_games(main) is not main)
    check("отдельный ключ виден", suppliers.has_own_games_key() is True)

    partner_client = suppliers.for_games(main)
    check("у клиента ключ напарника",
          partner_client.api_key == "fc_partner_key", partner_client.api_key)
    check("клиент переиспользуется",
          suppliers.for_games(main) is partner_client)

    await runtime.set_value(conn, "fazer_games_key", "fc_another")
    suppliers.forget()
    check("после смены ключа клиент новый",
          suppliers.for_games(main).api_key == "fc_another")

    # ключ можно задать и в .env, но панель важнее
    await runtime.set_value(conn, "fazer_games_key", "")
    db.settings.__dict__["fazer_games_key"] = "fc_iz_env"
    check("ключ берётся из .env, когда в панели пусто",
          suppliers.games_key() == "fc_iz_env", suppliers.games_key())
    await runtime.set_value(conn, "fazer_games_key", "fc_iz_paneli")
    check("ключ из панели важнее .env",
          suppliers.games_key() == "fc_iz_paneli", suppliers.games_key())
    db.settings.__dict__["fazer_games_key"] = ""

    # тот же ключ, что основной, — отдельным счётом не считается
    await runtime.set_value(conn, "fazer_games_key", db.settings.fazer_api_key)
    check("совпадающий ключ не считается отдельным",
          suppliers.has_own_games_key() is False)

    # ---- заказы через API идут со счёта игр, что бы ни заказали ----
    # Разработчик покупает у нас и игры, и звёзды. Пусть тратится один
    # счёт: владельцу видно расход на API одной цифрой.
    was_mode = db.settings.fragment_mode
    suppliers.forget()
    await runtime.set_value(conn, "fazer_games_key", "fc_partner_key")

    db.settings.__dict__["fragment_mode"] = "fazer"
    check("через API звёзды идут со счёта игр",
          suppliers.for_api(main).api_key == "fc_partner_key")
    check("регистр в названии режима не мешает",
          (db.settings.__dict__.__setitem__("fragment_mode", "FaZeR")
           or suppliers.for_api(main).api_key) == "fc_partner_key")

    # А вот при mystars звёзды уходят другим сервисом — подменять счёт
    # значило бы сменить поставщика молча.
    db.settings.__dict__["fragment_mode"] = "mystars"
    check("при mystars счёт не подменяется", suppliers.for_api(main) is main)
    db.settings.__dict__["fragment_mode"] = was_mode

    await suppliers.close_all()
    await runtime.set_value(conn, "fazer_games_key", "")
    suppliers.forget()


async def key_balances(conn) -> None:
    """Экран балансов: деньги поставщика и лимит сервиса ников."""
    from app.services import nicknames as nk

    class Rich(DeliveryProvider):
        async def get_balance(self):
            return "119.40 USD"

    class Broken(DeliveryProvider):
        async def get_balance(self):
            raise DeliveryError("HTTP 401: ключ не принят")

    await runtime.set_value(conn, "usd_rate_diram", "1090")
    await runtime.set_value(conn, "gameskinbo_key", "kluch")

    real = nk.usage

    async def usage(key):
        return {"used": 10, "limit": 100, "remaining": 90, "plan": "free"}

    nk.usage = usage
    try:
        call = call_of("pn:keys", uid=ADMIN)
        await panel.cb_keys(call, Rich())
        check("баланс поставщика показан", "119.40 USD" in call.last, call.last[:200])
        check("при одном ключе счёт назван общим",
              "Все товары" in call.last, call.last[:200])
        check("он пересчитан в сомони", "1 301.46" in call.last, call.last[:300])
        check("виден остаток лимита ников", "Осталось: <b>90</b>" in call.last,
              call.last)

        async def spent(key):
            return {"used": 95, "limit": 100, "remaining": 5, "plan": "free"}

        nk.usage = spent
        call = call_of("pn:keys", uid=ADMIN)
        await panel.cb_keys(call, Rich())
        check("малый остаток помечен другим цветом", "🟠" in call.last, call.last)

        call = call_of("pn:keys", uid=ADMIN)
        await panel.cb_keys(call, Broken())
        check("отказ поставщика виден", "не ответил" in call.last, call.last[:200])
        check("и причина показана", "401" in call.last)

        await runtime.set_value(conn, "gameskinbo_key", "")
        call = call_of("pn:keys", uid=ADMIN)
        await panel.cb_keys(call, Rich())
        check("без ключа ников так и сказано",
              "ключ не задан" in call.last, call.last[-300:])

        # два ключа — два счёта на экране
        from app.services import suppliers

        suppliers.forget()
        await runtime.set_value(conn, "fazer_games_key", "fc_partner_key")

        class PartnerRich(DeliveryProvider):
            async def get_balance(self):
                return "42.00 USD"

        suppliers._clients["fc_partner_key"] = PartnerRich()
        call = call_of("pn:keys", uid=ADMIN)
        await panel.cb_keys(call, Rich())
        check("показаны оба счёта",
              "Звёзды и Premium" in call.last and "Игры" in call.last, call.last)
        check("у каждого свой баланс",
              "119.40 USD" in call.last and "42.00 USD" in call.last, call.last)
        check("объяснено, зачем второй ключ",
              "не смешивались" in call.last or "не смешиваются" in call.last,
              call.last[-400:])
        suppliers.forget()
        await runtime.set_value(conn, "fazer_games_key", "")
    finally:
        nk.usage = real


async def after_refund(conn) -> None:
    """Заказ, выполнившийся уже после возврата денег, находится и не теряется.

    Возврат по таймауту — ставка: поставщик может отдать товар позже. Тогда
    клиент получил его бесплатно, и владелец должен узнать об этом один раз,
    а не на каждом круге присмотра.
    """
    bot = FakeBot()
    late = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, late.id, fragment_order_id="ord-late")
    await db.transition_order(
        conn, late.id, expected=db.ORDER_DELIVERING, new=db.ORDER_REFUNDED,
        error=f"{svc.TIMEOUT_MARK} не выполнился за отведённое время",
    )

    # поставщик всё ещё думает — трогать нечего
    found = await svc.check_after_refund(bot, conn, GameProvider(status="processing"))
    check("пока заказ в работе, тревоги нет", found == 0, str(found))
    check("метка возврата осталась на месте",
          (await db.get_order(conn, late.id)).error.startswith(svc.TIMEOUT_MARK))

    # а теперь он его выполнил
    found = await svc.check_after_refund(bot, conn, GameProvider(status="completed"))
    told = [t for t in bot.to(ADMIN) if "ord-late" in t]
    check("выполненный после возврата заказ найден", found >= 1 and bool(told),
          str(found))
    check("владельцу сказали", bool(told), str(bot.to(ADMIN)))
    check("в сообщении номер поставщика", told and "ord-late" in told[0])
    check("и ID игрока, кому ушёл товар", told and "1724367212" in told[0])
    check("и сумма возврата", told and fmt(1400) in told[0], str(told[:1]))
    check("подсказано, как списать обратно",
          told and "Клиенты" in told[0], str(told[:1]))

    # второй круг про тот же заказ молчит
    again = await svc.check_after_refund(bot, conn, GameProvider(status="completed"))
    check("повторно о том же не пишем", again == 0, str(again))
    check("причина возврата переписана",
          "выполнен после возврата" in (await db.get_order(conn, late.id)).error,
          (await db.get_order(conn, late.id)).error)

    # обычный возврат (отказ поставщика) присмотром не подхватывается
    plain = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="42", price=1400, cost=1090,
    )
    await db.update_order(conn, plain.id, fragment_order_id="ord-plain")
    await db.transition_order(
        conn, plain.id, expected=db.ORDER_DELIVERING, new=db.ORDER_REFUNDED,
        error="поставщик вернул статус failed",
    )
    quiet = await svc.check_after_refund(bot, conn, GameProvider(status="completed"))
    check("возврат по отказу присмотром не берётся", quiet == 0, str(quiet))

    # старые возвраты уходят из-под присмотра
    old_ts = (datetime.now(timezone.utc)
              - timedelta(hours=svc.AFTER_REFUND_HOURS + 1)).isoformat(timespec="seconds")
    aged = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="43", price=1400, cost=1090,
    )
    await db.update_order(conn, aged.id, fragment_order_id="ord-aged")
    await db.transition_order(
        conn, aged.id, expected=db.ORDER_DELIVERING, new=db.ORDER_REFUNDED,
        error=f"{svc.TIMEOUT_MARK} не выполнился за отведённое время",
    )
    await conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?",
                       (old_ts, aged.id))
    await conn.commit()
    stale = await svc.check_after_refund(bot, conn, GameProvider(status="completed"))
    check("давний возврат уже не сторожим", stale == 0, str(stale))


async def timeout_setting(conn) -> None:
    """Время ожидания выдачи меняется из панели."""
    check("по умолчанию ждём 20 минут",
          svc.timeout_minutes() == 20, str(svc.timeout_minutes()))

    await runtime.set_value(conn, "games_timeout_min", "45")
    check("значение из панели важнее",
          svc.timeout_minutes() == 45, str(svc.timeout_minutes()))

    # ждать меньше двух минут нельзя: выдача столько и идёт
    await runtime.set_value(conn, "games_timeout_min", "0")
    check("ноль не выключает ожидание", svc.timeout_minutes() >= 2,
          str(svc.timeout_minutes()))

    await runtime.set_value(conn, "games_timeout_min", "5")
    call = call_of("pn:games", uid=ADMIN)
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    await panel.cb_games(call, state, conn)
    check("в панели видно текущее ожидание",
          any("5 мин" in b for b in buttons(call.markup)), str(buttons(call.markup)))
    check("и оно же в пояснении", "5 мин" in call.last, call.last[-200:])

    # предупреждение приходит по новому сроку, а не по старому
    bot = FakeBot()
    order = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, order.id, fragment_order_id="ord-short")
    ago = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat(timespec="seconds")
    await conn.execute("UPDATE orders SET created_at = ? WHERE id = ?",
                       (ago, order.id))
    await conn.commit()
    result = await svc.check(bot, conn, GameProvider(status="processing"),
                             await db.get_order(conn, order.id))
    check("укороченное ожидание работает",
          result == "waiting" and any("долго в обработке" in t for t in bot.to(ADMIN)), result)

    await runtime.set_value(conn, "games_timeout_min", "20")


async def gorder_command(conn) -> None:
    """/gorder показывает сырой ответ поставщика по игровому заказу."""
    from aiogram.filters import CommandObject

    from app.handlers import admin

    order = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, order.id, fragment_order_id="ord-1296254")

    async def run(args: str, provider=None):
        message = msg("/gorder " + args, uid=ADMIN)
        await admin.cmd_game_order(
            message, CommandObject(command="gorder", args=args),
            conn, provider or GameProvider(status="processing"),
        )
        return message

    told = await run("")
    check("без номера показана подсказка", "Использование" in told.last, told.last[:80])

    told = await run(str(order.id))
    check("наш номер понимается", "ord-1296254" in told.last, told.last[:200])
    check("видно, что заказ ещё в работе", "в работе" in told.last, told.last[:300])
    check("показан сырой ответ поставщика", "processing" in told.last, told.last)
    check("и наша сторона заказа", f"№{order.id}" in told.last, told.last[:300])

    told = await run("ord-1296254", GameProvider(status="completed"))
    check("номер поставщика понимается тоже", "выполнен" in told.last, told.last[:300])

    told = await run("ord-1296254", GameProvider(status="failed"))
    check("отказ виден отдельно", "отклонён" in told.last, told.last[:300])

    told = await run("999999")
    check("несуществующий заказ назван", "нет" in told.last, told.last[:80])

    bare = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="7", price=1400, cost=1090,
    )
    told = await run(str(bare.id))
    check("про заказ без номера сказано прямо",
          "нет номера" in told.last, told.last[:120])

    class Silent(GameProvider):
        async def order_status(self, order_id):
            return None

    told = await run("ord-1296254", Silent())
    check("молчание поставщика не ломает команду",
          "не ответил" in told.last, told.last[:120])

    check("команда есть в справке админа", "/gorder" in texts.ADMIN_HELP)


async def game_icons(conn) -> None:
    """Премиум-значки: на кнопке игры свой, в заголовке раздела — оба."""
    from app import emoji

    check("Free Fire узнаётся по коду",
          emoji.game_key("free_fire_br") == "game",
          emoji.game_key("free_fire_br"))
    check("и с другим написанием тоже",
          emoji.game_key("freefire_asia") == "game")
    check("PUBG узнаётся", emoji.game_key("pubg_mobile") == "pubg")
    check("незнакомая игра значка не получает",
          emoji.game_key("magic_chess_ru") == "", emoji.game_key("magic_chess_ru"))

    await runtime.set_value(conn, "custom_emoji_on", "1")
    await db.add_game(conn, category_id="pubg_mobile", title="🎯 PUBG",
                      field="user_id")
    await db.update_game(conn, "pubg_mobile", enabled=1)
    await db.load_game_titles(conn)

    games = await db.list_games(conn, only_enabled=True)
    marks = {}
    for row in keyboards.games_menu(games).inline_keyboard:
        for b in row:
            marks[b.text] = b.icon_custom_emoji_id

    check("на Free Fire стоит его значок",
          marks.get("Free Fire") == "6012423622730192070", str(marks))
    check("на PUBG — свой", marks.get("PUBG") == "5204252919565657978", str(marks))
    check("обычный значок из подписи убран",
          "🔥 Free Fire" not in marks, str(list(marks)))

    # свой ID владельца важнее справочника
    await db.update_game(conn, "pubg_mobile", emoji="1111222233334444")
    games = await db.list_games(conn, only_enabled=True)
    marks = {b.text: b.icon_custom_emoji_id
             for row in keyboards.games_menu(games).inline_keyboard for b in row}
    check("заданный вручную значок важнее",
          marks.get("PUBG") == "1111222233334444", str(marks))

    await db.update_game(conn, "pubg_mobile", emoji="")

    # без премиум-эмодзи кнопки не ломаются
    await runtime.set_value(conn, "custom_emoji_on", "0")
    games = await db.list_games(conn, only_enabled=True)
    plain = keyboards.games_menu(games).inline_keyboard
    check("с выключенными премиум-эмодзи значков нет",
          all(b.icon_custom_emoji_id is None for row in plain for b in row))
    check("и подписи остались целыми",
          any("Free Fire" in b.text for row in plain for b in row),
          str([b.text for row in plain for b in row]))
    await runtime.set_value(conn, "custom_emoji_on", "1")

    # в заголовке раздела помещаются оба
    from app.emoji import substitute

    head = substitute(texts.GAMES_ENTRY)
    check("в заголовке раздела значок Free Fire",
          "6012423622730192070" in head, head[:200])
    check("и значок PUBG рядом", "5204252919565657978" in head, head[:200])

    await db.delete_game(conn, "pubg_mobile")
    await runtime.set_value(conn, "custom_emoji_on", "0")


async def region_step(conn) -> None:
    """Сначала игра, потом регион: ID игрока живёт на конкретном сервере."""
    from app.services import regions as reg

    check("регион читается из кода категории",
          reg.split("free_fire_br") == ("free_fire", "br"),
          str(reg.split("free_fire_br")))
    check("у кода без региона семья — он сам",
          reg.split("pubg_mobile") == ("pubg_mobile", ""),
          str(reg.split("pubg_mobile")))
    check("СНГ распознаётся", reg.title_of("free_fire_cis") == "🌍 СНГ")
    check("Индонезия распознаётся", reg.title_of("free_fire_id") == "🇮🇩 Индонезия")
    check("Бразилия распознаётся", reg.title_of("free_fire_br") == "🇧🇷 Бразилия")
    check("для ников берётся код сервера",
          reg.nick_region("free_fire_id") == "ID", reg.nick_region("free_fire_id"))

    # поставщик часто пишет регион не в коде, а в названии
    check("регион читается из названия в скобках",
          reg.split("mcgg_1", "Magic Chess Go Go (RU)") == ("magic chess go go", "ru"),
          str(reg.split("mcgg_1", "Magic Chess Go Go (RU)")))
    check("регион читается и через тире",
          reg.suffix_of("ff_x", "Free Fire - Brazil") == "br",
          reg.suffix_of("ff_x", "Free Fire - Brazil"))
    check("название на кнопке без приписки региона",
          reg.clean_title("mcgg_1", "Magic Chess Go Go (RU)") == "Magic Chess Go Go",
          reg.clean_title("mcgg_1", "Magic Chess Go Go (RU)"))
    check("код важнее названия",
          reg.suffix_of("free_fire_br", "Free Fire (RU)") == "br",
          reg.suffix_of("free_fire_br", "Free Fire (RU)"))
    check("случайные скобки регионом не считаются",
          reg.split("abc", "Genshin Impact (новинка)") == ("abc", ""),
          str(reg.split("abc", "Genshin Impact (новинка)")))

    for code, name in (("mcgg_1", "Magic Chess Go Go (RU)"),
                       ("mcgg_2", "Magic Chess Go Go (ID)"),
                       ("mcgg_3", "Magic Chess Go Go (BR)")):
        await db.add_game(conn, category_id=code, title=name, field="user_id")
        await db.update_game(conn, code, enabled=1)
    await db.load_game_titles(conn)
    menu = buttons(keyboards.games_menu(await db.list_games(conn, only_enabled=True)))
    check("игра с регионом в названии — одна кнопка",
          sum(1 for b in menu if "Magic Chess" in b) == 1, str(menu))
    check("и без приписки региона на ней",
          any(b.strip() == "Magic Chess Go Go" for b in menu), str(menu))
    for code in ("mcgg_1", "mcgg_2", "mcgg_3"):
        await db.delete_game(conn, code)

    for code in ("free_fire_cis", "free_fire_id"):
        await db.add_game(conn, category_id=code, title="🔥 Free Fire",
                          field="user_id", region=reg.nick_region(code))
        await db.update_game(conn, code, enabled=1)
    await db.load_game_titles(conn)

    games = await db.list_games(conn, only_enabled=True)
    menu = buttons(keyboards.games_menu(games))
    check("три региона показаны одной кнопкой",
          sum(1 for b in menu if "Free Fire" in b) == 1, str(menu))

    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=BUYER, user_id=BUYER))
    call = call_of("m:games")
    await gh.cb_games(call, state, conn)
    check("в меню кнопка ведёт к выбору региона",
          any(b.callback_data == "gf:free_fire"
              for row in call.markup.inline_keyboard for b in row),
          str(buttons(call.markup)))

    call = call_of("gf:free_fire")
    await gh.cb_family(call, state, conn)
    check("экран региона открылся", "минтақа" in call.last.lower(), call.last[:120])
    check("объяснено, зачем регион",
          "ёфт намешавад" in call.last, call.last)
    picks = buttons(call.markup)
    check("предложены все три региона",
          all(any(name in b for b in picks)
              for name in ("СНГ", "Индонезия", "Бразилия")), str(picks))
    check("СНГ стоит первым", "СНГ" in picks[0], str(picks))
    check("каждый регион ведёт в свою категорию",
          {b.callback_data for row in call.markup.inline_keyboard for b in row}
          >= {"g:free_fire_cis", "g:free_fire_id", "g:free_fire_br"},
          str([b.callback_data for row in call.markup.inline_keyboard for b in row]))

    call = call_of("g:free_fire_id")
    await gh.cb_game(call, state, conn, GameProvider())
    check("после региона показаны пакеты", "Маҷмӯаро интихоб кунед" in call.last,
          call.last[:120])
    check("регион виден в заголовке", "Индонезия" in call.last, call.last[:120])

    call = call_of("gp:free_fire_id:0")
    await gh.cb_pack(call, state, conn)
    check("регион виден и при вводе ID", "Индонезия" in call.last, call.last[:120])

    # выключенный регион исчезает из выбора
    await db.update_game(conn, "free_fire_id", enabled=0)
    call = call_of("gf:free_fire")
    await gh.cb_family(call, state, conn)
    picks = buttons(call.markup)
    check("выключенный регион не предлагается",
          not any("Индонезия" in b for b in picks), str(picks))

    # игра с одним регионом лишнего шага не просит
    await db.add_game(conn, category_id="genshin_impact", title="⚔️ Genshin",
                      field="uid")
    await db.update_game(conn, "genshin_impact", enabled=1)
    games = await db.list_games(conn, only_enabled=True)
    codes = {b.callback_data
             for row in keyboards.games_menu(games).inline_keyboard for b in row}
    check("одиночная игра ведёт сразу к пакетам",
          "g:genshin_impact" in codes, str(codes))

    await db.delete_game(conn, "genshin_impact")
    await db.update_game(conn, "free_fire_id", enabled=1)


async def catalog_pick(conn) -> None:
    """Каталог поставщика в панели: коды и регионы не переписываются руками."""
    class Catalog(GameProvider):
        async def game_catalog(self):
            return [
                {"category_id": "free_fire_br", "name": "Free Fire",
                 "fields": [{"name": "player_id"}]},
                {"category_id": "free_fire_id", "name": "Free Fire",
                 "fields": [{"name": "player_id"}]},
                {"category_id": "free_fire_cis", "name": "Free Fire",
                 "fields": [{"name": "player_id"}]},
                {"category_id": "mobile_legends_ph", "name": "Mobile Legends",
                 "fields": [{"name": "user_id"}]},
            ]

    call = call_of("pn:game_pick", uid=ADMIN)
    await panel.cb_game_pick(call, conn, Catalog())
    picks = buttons(call.markup)
    check("каталог показан", "Каталог поставщика" in call.last, call.last[:80])
    check("игры сгруппированы, а не по регионам",
          sum(1 for b in picks if "Free Fire" in b) == 1, str(picks))
    check("видно число регионов",
          any("регионов: 3" in b for b in picks), str(picks))
    check("вторая игра тоже в списке",
          any("Mobile Legends" in b for b in picks), str(picks))

    call = call_of("pn:game_add:mobile_legends", uid=ADMIN)
    await panel.cb_game_add_family(call, conn, Catalog())
    added = await db.get_game(conn, "mobile_legends_ph")
    check("регион добавлен одной кнопкой", added is not None)
    check("поле для ID взято из каталога", added and added.field == "user_id",
          added.field if added else "")
    check("регион для ников проставлен", added and added.region == "PH",
          added.region if added else "")
    check("новая игра сразу не продаётся", added and added.enabled == 0)
    check("показано, что именно добавлено",
          "Филиппины" in call.last, call.last[:300])

    call = call_of("pn:game_all_on:mobile_legends", uid=ADMIN)
    await panel.cb_game_family_on(call, conn)
    check("все регионы включаются одной кнопкой",
          (await db.get_game(conn, "mobile_legends_ph")).enabled == 1)

    # название, поставленное владельцем, при добавлении регионов не теряется
    call = call_of("pn:game_add:free_fire", uid=ADMIN)
    await panel.cb_game_add_family(call, conn, Catalog())
    for code in ("free_fire_br", "free_fire_cis", "free_fire_id"):
        game = await db.get_game(conn, code)
        check(f"название сохранено у {code}",
              game is not None and game.title == "🔥 Free Fire",
              game.title if game else "")
    check("включённый регион не выключился",
          (await db.get_game(conn, "free_fire_br")).enabled == 1)

    class Broken(GameProvider):
        async def game_catalog(self):
            raise DeliveryError("HTTP 401: ключ не принят")

    call = call_of("pn:game_pick", uid=ADMIN)
    await panel.cb_game_pick(call, conn, Broken())
    check("поломка каталога объяснена", "не пришёл" in call.last, call.last[:120])
    check("и подсказано, куда смотреть", "ключ" in call.last.lower(), call.last)

    await db.delete_game(conn, "mobile_legends_ph")


# ────────────────────────────────────────────── прайс списком


class UcProvider(GameProvider):
    """Поставщик с длинным прайсом: у PUBG пакетов за тридцать."""

    async def game_catalog(self):
        return [{"category_id": "pubg_mobile", "name": "PUBG Mobile",
                 "fields": [{"name": "player_id", "label": "ID игрока"}]}]

    async def game_offers(self, category_id):
        uc = [60, 120, 325, 660, 985, 1320, 1800, 2460, 3850, 5650,
              8100, 11950, 16200]
        offers = [
            {"offer_id": f"uc_{n}", "name": f"PUBG Mobile {n} UC",
             "usd": Decimal("1.00"), "raw": {}}
            for n in uc
        ]
        offers += [
            {"offer_id": f"extra_{i}", "name": name,
             "usd": Decimal("2.00"), "raw": {}}
            for i, name in enumerate([
                "First Purchase", "Prime (1 месяц)", "Prime (3 месяца)",
                "Prime Plus (1 месяц)", "Elite Pass (уровни 1-50)",
                "Elite Pass Plus (уровни 1-100)", "Еженедельный набор",
                "Пакет Mythic Emblem",
            ])
        ]
        return offers


async def price_list(conn) -> None:
    from app.services import pricelist

    offers = [
        {"offer_id": "a", "name": "60 UC", "supplier_name": "PUBG Mobile 60 UC",
         "price": 1100},
        {"offer_id": "b", "name": "120 UC", "supplier_name": "PUBG Mobile 120 UC",
         "price": 2200},
        {"offer_id": "c", "name": "Elite Pass Plus (уровни 1-100)",
         "supplier_name": "Elite Pass Plus 1-100", "price": 26000},
        {"offer_id": "d", "name": "1320 UC", "supplier_name": "PUBG 1320 UC",
         "price": 18500},
    ]

    plan = pricelist.build(
        "Махсулоти PUBG Mobile-ро интихоб кунед:\n"
        "60 UC - 10 сомонӣ\n"
        "120 UC — 21 с.\n"
        "1320 UC 180\n"
        "Elite Pass Plus (уровни 1-100) - 250 сомонӣ\n"
        "9999 UC - 12\n",
        offers,
    )
    got = {r.offer["offer_id"]: r.price for r in plan.matched}
    check("прайс разложен по пакетам",
          got == {"a": 1000, "b": 2100, "d": 18000, "c": 25000}, str(got))
    check("тире внутри названия не путает разбор",
          got.get("c") == 25000, str(got))
    check("цена без валюты понимается", got.get("d") == 18000, str(got))
    check("строка без пакета показана отдельно",
          [r.name for r in plan.lost] == ["9999 UC"],
          str([r.name for r in plan.lost]))
    check("заголовок не считается пакетом", len(plan.bad) == 1, str(plan.bad))
    check("меняются только те, где цена другая", len(plan.changed) == 4)

    # две строки на один пакет — вторую молча применять нельзя
    twice = pricelist.build("60 UC - 10\n60 UC - 99\n", offers)
    check("один пакет занимается один раз", len(twice.matched) == 1,
          str([r.line for r in twice.matched]))
    check("вторая строка на тот же пакет не теряется",
          len(twice.lost) == 1, str([r.line for r in twice.lost]))

    # неоднозначность: два пакета с тем же числом и без общих слов
    murky = pricelist.build("60 - 10\n", [
        {"offer_id": "x", "name": "60 UC", "supplier_name": "", "price": 100},
        {"offer_id": "y", "name": "60 алмазов", "supplier_name": "", "price": 100},
    ])
    check("спорную строку не применяем молча",
          not murky.matched and murky.lost[0].why == "подходит сразу несколько",
          murky.lost[0].why if murky.lost else "—")

    check("копейки в прайсе не теряются",
          pricelist.build("60 UC - 7,20\n", offers).matched[0].price == 720)

    # ─── экран панели
    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    provider = UcProvider()
    await db.add_game(conn, category_id="pubg_mobile", title="🎯 PUBG",
                      field="player_id")
    await db.update_game(conn, "pubg_mobile", enabled=1)

    call = call_of("pn:game_packs:pubg_mobile", uid=ADMIN)
    await panel.cb_game_offers(call, conn, provider)
    check("в пакетах есть прайс списком",
          "📋 Прайс списком" in buttons(call.markup), str(buttons(call.markup)))
    check("длинный список разбит на страницы",
          "Дальше ›" in buttons(call.markup), str(buttons(call.markup)))
    check("на первой странице нет кнопки назад по страницам",
          "‹ Раньше" not in buttons(call.markup))
    check("видно, сколько всего пакетов",
          "из 21" in call.last, call.last[-300:])

    call = call_of("pn:game_packs:pubg_mobile:1", uid=ADMIN)
    await panel.cb_game_offers(call, conn, provider)
    check("вторая страница открывается",
          "‹ Раньше" in buttons(call.markup), str(buttons(call.markup)))
    check("на второй странице видны последние пакеты",
          "Mythic Emblem" in call.last, call.last[:400])
    check("на второй странице нет первых пакетов",
          "60 UC" not in call.last, call.last[:300])

    call = call_of("pn:price_list:pubg_mobile", uid=ADMIN)
    await panel.cb_price_list(call, state, conn)
    check("бот просит прайс", "Прайс списком" in call.last, call.last[:80])
    check("показан пример строки", "60 UC - 10" in call.last, call.last[:400])
    check("ждём текст прайса", await state.get_state() == "Panel:value")

    sent = msg("60 UC - 10\n120 UC - 21\n325 UC - 45\n"
               "Elite Pass Plus (уровни 1-100) - 250\n"
               "777 UC - 5\n", uid=ADMIN)
    await panel.on_field_value(sent, state, conn, provider)
    check("показан предпросмотр", "Прайс:" in sent.last, sent.last[:80])
    check("в предпросмотре новая цена", "10.00 с." in sent.last, sent.last[:400])
    check("в предпросмотре видна старая цена", "<s>" in sent.last, sent.last[:400])
    check("непонятая строка названа",
          "777 UC" in sent.last, sent.last[-600:])
    check("цены ещё не поставлены",
          not await db.game_prices(conn, "pubg_mobile"))
    check("кнопка применения показывает счёт",
          any("Поставить цены (4)" in b for b in buttons(sent.markup)),
          str(buttons(sent.markup)))

    check("можно забрать из прайса и названия",
          any("Цены и названия" in b for b in buttons(sent.markup)),
          str(buttons(sent.markup)))

    call = call_of("pn:price_go:pubg_mobile", uid=ADMIN)
    await panel.cb_price_apply(call, conn, provider)
    saved = await db.game_prices(conn, "pubg_mobile")
    check("цены из прайса встали",
          saved.get("uc_60") == 1000 and saved.get("uc_120") == 2100
          and saved.get("uc_325") == 4500, str(saved))
    check("длинное название тоже нашло свой пакет",
          saved.get("extra_5") == 25000, str(saved))
    check("пакеты вне прайса не тронуты", "uc_660" not in saved, str(saved))
    check("после применения снова видны пакеты",
          "Пакеты:" in call.last, call.last[:80])

    # прайс применяется один раз: второй нажим — список уже устарел
    call = call_of("pn:price_go:pubg_mobile", uid=ADMIN)
    await panel.cb_price_apply(call, conn, provider)
    check("повторное применение не проходит",
          any("устарел" in a for a in call.alerts), str(call.alerts))

    # вторая кнопка забирает из прайса ещё и названия
    call = call_of("pn:price_list:pubg_mobile", uid=ADMIN)
    await panel.cb_price_list(call, state, conn)
    again = msg("60 UC - 10\n120 UC - 21\n", uid=ADMIN)
    await panel.on_field_value(again, state, conn, provider)
    call = call_of("pn:price_name:pubg_mobile", uid=ADMIN)
    await panel.cb_price_apply(call, conn, provider)
    setup = await db.game_offers_setup(conn, "pubg_mobile")
    check("название пакета взято из прайса",
          setup["uc_60"]["title"] == "60 UC", str(setup.get("uc_60")))
    check("длинное название поставщика больше не показывается",
          "PUBG Mobile 60 UC" not in call.last, call.last[:300])
    check("короткое название видно клиенту",
          "✏️ 60 UC — <b>10.00" in call.last, call.last[:300])

    # прайс совсем не про эту игру
    call = call_of("pn:price_list:pubg_mobile", uid=ADMIN)
    await panel.cb_price_list(call, state, conn)
    junk = msg("привет как дела", uid=ADMIN)
    await panel.on_field_value(junk, state, conn, provider)
    check("пустой разбор объяснён",
          "Ни одна строка" in junk.last, junk.last[:80])

    await db.delete_game(conn, "pubg_mobile")


async def crowd(conn) -> None:
    """Тысяча клиентов, открывших игру разом, — один запрос к поставщику.

    Память сама по себе от этого не спасает: все промахиваются мимо неё
    одновременно, пока она ещё пуста, и уходят к поставщику толпой.
    Поставщик такого не выдержит, а вместе с ним встанет витрина.
    """
    from app.handlers.games import offers_of
    from app.services import games as gsvc

    class Slow:
        def __init__(self):
            self.calls = 0

        async def game_offers(self, category_id):
            self.calls += 1
            await asyncio.sleep(0.02)          # сеть
            return [{"offer_id": "p1", "name": "100 💎",
                     "usd": Decimal("1.00"), "raw": {}}]

    await runtime.set_value(conn, "usd_rate_diram", "1090")
    game = await db.add_game(conn, category_id="crowd_game", title="Толпа")

    gsvc.forget_catalog()
    alone = Slow()
    await asyncio.gather(*[offers_of(alone, game, conn, cached=True)
                           for _ in range(500)])
    check("пятьсот нажатий разом — один запрос к поставщику",
          alone.calls == 1, f"{alone.calls} запросов")

    # А владельцу нужно живьём: он смотрит именно затем, чтобы увидеть,
    # что у поставщика прямо сейчас.
    fresh = Slow()
    await offers_of(fresh, game, conn, for_owner=True)
    await offers_of(fresh, game, conn, for_owner=True)
    check("владельцу пакеты приходят живьём", fresh.calls == 2,
          f"{fresh.calls} запросов")

    gsvc.forget_catalog()


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await pricing(conn)
        await nick_lookup()
        await flow(conn)
        await unknown_nick(conn)
        await panel_screens(conn)
        await after_refund(conn)
        await timeout_setting(conn)
        await gorder_command(conn)
        await game_icons(conn)
        await region_step(conn)
        await full_catalog(conn)
        await catalog_search(conn)
        await wrong_code(conn)
        await two_fields(conn)
        await volsever_check(conn)
        await crowd(conn)
        await nick_probe(conn)
        await verdict_reading(conn)
        await wrong_region(conn)
        await catalog_pick(conn)
        await price_list(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
