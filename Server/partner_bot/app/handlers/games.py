"""Покупка игровых пополнений: выбор пакета, проверка ID, заказ."""
from __future__ import annotations

import asyncio
import logging
import re

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, runtime, texts
from app.money import fmt
from app.services import games as svc
from app.services import suppliers
from app.services import nicknames
from app.services import regions
from app.services import volsever
from app.services.fragment import DeliveryError, DeliveryProvider, DeliveryUncertain
from app.states import Game

log = logging.getLogger(__name__)
router = Router(name="games")

#: Пакеты живут недолго в памяти: список одинаков для всех, а дёргать
#: поставщика на каждое нажатие незачем.
_offers: dict[str, list[dict]] = {}


async def offers_of(
    provider, game: db.Game, conn=None, *, for_owner: bool = False,
    cached: bool = False,
) -> list[dict]:
    """Пакеты игры с ценой в сомони.

    Своя цена, название и скрытие, если заданы, важнее того, что прислал
    поставщик: владелец мог поставить ровную сумму, назвать пакет
    по-человечески или убрать его из продажи.

    for_owner — показать и скрытые: в панели их надо видеть, чтобы
    вернуть обратно.
    """
    if conn is not None:
        from app.handlers import donatix
        await donatix.sync_rate(conn, donatix.RATE_PAYMENT)  # цена в сомони — по курсу не старше 30 с
    raw = await svc.offers_raw(provider, game.category_id, cached)
    margin = svc.margin_of(game)
    setup = await db.game_offers_setup(conn, game.category_id) if conn else {}

    offers = []
    for item in raw:
        auto = svc.offer_price(item["usd"], margin)
        own = setup.get(item["offer_id"], {})
        price = own.get("price")
        offers.append({
            "offer_id": item["offer_id"],
            "name": own.get("title") or item["name"],
            "supplier_name": item["name"],
            "usd": item["usd"],
            "price": price if price is not None else auto,
            "auto": auto,
            "manual": price is not None,
            "renamed": bool(own.get("title")),
            "hidden": bool(own.get("hidden")),
            "cost": svc.offer_cost(item["usd"]),
        })

    offers = svc.sort_packs([o for o in offers if o["price"] > 0])
    if not for_owner:
        offers = [o for o in offers if not o["hidden"]]
    _offers[game.category_id] = offers
    return offers


@router.callback_query(F.data == "m:games")
async def cb_games(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    await state.clear()
    games = await db.list_games(conn, only_enabled=True)
    if not runtime.get_bool("games_enabled"):
        games = []
    if not games and not runtime.steam_on():
        await call.answer("Бахш муваққатан баста аст.", show_alert=True)
        return
    await call.message.edit_text(
        texts.GAMES_ENTRY, reply_markup=keyboards.games_menu(games)
    )
    await call.answer()


#: О какой игре уже писали владельцу. Иначе каждый зашедший клиент
#: присылал бы ему одно и то же сообщение.
_told: set[str] = set()


async def _tell_admins_broken(bot: Bot, game: db.Game, error: str) -> None:
    """Сказать владельцу, что код игры больше не работает."""
    if game.category_id in _told or bot is None:
        return
    _told.add(game.category_id)
    log.warning("Игры: код %s не принят поставщиком — %s",
                game.category_id, error)

    from app.services import delivery

    await delivery.notify_admins(
        bot,
        "⚠️ <b>Игра не открывается у клиентов</b>\n"
        f"├ {game.title}\n"
        f"└ Код: <code>{game.category_id}</code>\n\n"
        f"<blockquote expandable>{error[:400]}</blockquote>\n\n"
        "<blockquote>Поставщик не знает такого кода. Панель → 🕹 Игры → "
        "эта игра → «📦 Проверить пакеты»: бот покажет похожие коды из "
        "каталога и переставит одним нажатием.</blockquote>",
    )


def _region(game: db.Game) -> str:
    """Приписка с регионом. Пусто, если регион у игры один — лишний шум."""
    title = regions.title_of(game)
    return f" · {title}" if title else ""


@router.callback_query(F.data.startswith("gf:"))
async def cb_family(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Выбор региона: у одной игры их несколько, и ID ищется только в своём."""
    await state.clear()
    family = call.data.split(":", 1)[1]
    games = await db.list_games(conn, only_enabled=True)
    items = [g for g in games if regions.family_of(g) == family]
    # СНГ первым: он нужен чаще всего, и листать до него не надо.
    items.sort(key=regions.sort_key)
    if not items:
        await call.answer("Ин бозӣ дигар фурӯхта намешавад.", show_alert=True)
        return
    await call.message.edit_text(
        texts.GAME_REGION.format(title=items[0].title),
        reply_markup=keyboards.game_regions(items),
    )
    await call.answer()


@router.callback_query(F.data.startswith("g:"), ~F.data.in_({"g:ok"}))
async def cb_game(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider, bot: Bot = None,
) -> None:
    category_id = call.data.split(":", 1)[1]
    game = await db.get_game(conn, category_id)
    if game is None or not game.enabled:
        await call.answer("Ин бозӣ дигар фурӯхта намешавад.", show_alert=True)
        return

    await call.answer("Маҷмӯаҳоро мебинам…")
    provider = suppliers.for_games(provider)
    try:
        # Из памяти, а не с новым запросом к поставщику на каждое
        # нажатие. Пакеты у него меняются раз в дни, а клиентов может
        # быть тысяча разом — столько запросов подряд он просто не
        # выдержит, и вместе с ним встанет витрина. Свои цены и
        # названия при этом читаются заново каждый раз: правка в панели
        # видна клиенту сразу.
        offers = await offers_of(provider, game, conn, cached=True)
    except (DeliveryError, DeliveryUncertain) as exc:
        log.info("Игры: пакеты %s не пришли — %s", category_id, exc)
        # Неверный код категории сам не пройдёт: ждать бесполезно, и
        # владелец должен узнать об этом, а не клиент — переставить код.
        if "category" in str(exc).lower():
            await _tell_admins_broken(bot, game, str(exc))
            await call.message.edit_text(
                "😔 Эта игра сейчас недоступна. Мы уже разбираемся — "
                "попробуйте другую или напишите в поддержку.",
                reply_markup=keyboards.back(),
            )
        else:
            await call.message.edit_text(
                "😔 Пакеты этой игры сейчас недоступны. Попробуйте позже.",
                reply_markup=keyboards.back(),
            )
        return

    if not offers:
        await call.message.edit_text(
            "😔 У этой игры пока нет пакетов в продаже.",
            reply_markup=keyboards.back(),
        )
        return

    await state.clear()
    await call.message.edit_text(
        texts.GAME_PACKS.format(title=game.title, region=_region(game)),
        reply_markup=keyboards.game_packs(game, offers),
    )


@router.callback_query(F.data.startswith("gp:"))
async def cb_pack(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    _, category_id, raw_index = call.data.split(":", 2)
    game = await db.get_game(conn, category_id)
    offers = _offers.get(category_id) or []
    index = int(raw_index) if raw_index.isdigit() else -1

    if game is None or not 0 <= index < len(offers):
        await call.answer("Маҷмӯа кӯҳна шуд, бозиро аз нав кушоед.", show_alert=True)
        return

    offer = offers[index]
    user = await db.get_user(conn, call.from_user.id)
    balance = user.balance if user else 0
    if balance < offer["price"]:
        await call.message.edit_text(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(offer["price"]), balance=fmt(balance),
                missing=fmt(offer["price"] - balance),
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        await call.answer()
        return

    await state.set_state(Game.player)
    await state.update_data(
        category_id=category_id, offer_index=index,
        offer_id=offer["offer_id"], pack=offer["name"],
        price=offer["price"], cost=offer["cost"],
    )
    await call.message.edit_text(
        _ask_text(game, offer["name"]),
        reply_markup=keyboards.cancel(),
    )
    await call.answer()


def _ask_text(game: db.Game, pack: str) -> str:
    """Что просим у клиента: один ID или ID вместе с номером сервера."""
    names = game.field_names
    if len(names) < 2:
        return texts.GAME_ASK_ID.format(
            title=game.title, region=_region(game), pack=pack
        )
    return texts.GAME_ASK_TWO.format(
        title=game.title, region=_region(game), pack=pack,
        first=svc.field_label(names[0]), second=svc.field_label(names[1]),
        example=EXAMPLE,
    )


#: Пример ввода двух чисел. Настоящие ID выглядят именно так.
EXAMPLE = "123456789 1234"

#: ID и сервер клиенты пишут по-разному: «123 456», «123(456)», «123-456».
_NUMBERS = re.compile(r"\d+")


def parse_ids(raw: str, count: int) -> list[str] | None:
    """Разобрать введённые ID. None — введено не то.

    Разделителем считаем что угодно, кроме цифр: клиент копирует ID из
    игры вместе со скобками, и придираться к пробелу — терять покупку.
    """
    found = _NUMBERS.findall(raw or "")
    if len(found) != count:
        return None
    if not all(1 <= len(value) <= 20 for value in found):
        return None
    if len(found[0]) < 5:
        return None
    return found


@router.message(Game.player, F.text)
async def on_player_id(
    message: Message, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider,
) -> None:
    data = await state.get_data()
    game = await db.get_game(conn, data.get("category_id", ""))
    if game is None:
        await state.clear()
        await message.answer("Ин бозӣ дигар фурӯхта намешавад. Менюро кушоед: /menu")
        return

    names = game.field_names
    values = parse_ids(message.text or "", len(names))
    if values is None:
        if len(names) < 2:
            await message.answer(texts.GAME_ID_FORMAT)
        else:
            await message.answer(texts.GAME_TWO_FORMAT.format(
                first=svc.field_label(names[0]),
                second=svc.field_label(names[1]), example=EXAMPLE,
            ))
        return

    fields = dict(zip(names, values))
    player = shown_id(values)

    notice = await message.answer(texts.GAME_CHECKING.format(player=player))
    client = suppliers.for_games(provider)
    name, verdict = await _lookup(client, game, fields)

    if verdict == "bad":
        # Чаще всего дело не в ID, а в регионе: аккаунт есть, но на другом
        # сервере. Ищем его там сами, чтобы клиент не гадал.
        others = await _other_regions(conn, game)
        found = await _search_regions(client, others, values)
        if found:
            other_game, other_name = found
            await notice.edit_text(
                texts.GAME_WRONG_REGION.format(
                    player=player, name=other_name or "—",
                    region=regions.region_title(other_game),
                ),
                reply_markup=keyboards.game_found_in([other_game]),
            )
            return
        # Нигде не нашли — но запирать покупку нельзя: проверка работает
        # не у всех серверов, а пополнение идёт по ID. Предупреждаем и
        # оставляем решение за клиентом.
        user = await db.get_user(conn, message.from_user.id)
        balance = user.balance if user else 0
        price = data["price"]
        await state.update_data(player=player, player_name="", fields=fields)
        await state.set_state(Game.confirm)
        await notice.edit_text(
            texts.GAME_UNVERIFIED.format(
                player=player, pack=data["pack"], price=fmt(price),
                rest=fmt(max(balance - price, 0)),
            ),
            reply_markup=keyboards.confirm_unverified(game, len(others) + 1),
        )
        return

    user = await db.get_user(conn, message.from_user.id)
    balance = user.balance if user else 0
    price = data["price"]
    await state.update_data(player=player, player_name=name or "", fields=fields)
    await state.set_state(Game.confirm)

    template = texts.GAME_CONFIRM if name else texts.GAME_NO_NAME
    await notice.edit_text(
        template.format(
            name=name or "—", player=player, pack=data["pack"],
            price=fmt(price), rest=fmt(max(balance - price, 0)),
        ),
        reply_markup=keyboards.confirm_game(game.category_id),
    )


async def _other_regions(conn, game: db.Game) -> list[db.Game]:
    """Остальные регионы этой же игры, в порядке спроса."""
    family = regions.family_of(game)
    others = [
        other for other in await db.list_games(conn, only_enabled=True)
        if regions.family_of(other) == family
        and other.category_id != game.category_id
    ]
    others.sort(key=regions.sort_key)
    return others


async def _search_regions(
    provider, games: list[db.Game], values: list[str],
) -> tuple[db.Game, str | None] | None:
    """Поискать ID по остальным регионам. None — нигде не нашёлся."""
    for game in games:
        names = game.field_names
        if len(names) != len(values):
            continue          # у этого региона другой набор полей
        try:
            name, verdict = await provider.validate_game_id(
                game.category_id, dict(zip(names, values))
            )
        except Exception as exc:  # noqa: BLE001 — это подсказка, не покупка
            log.info("Игры: регион %s не проверился — %s", game.category_id, exc)
            continue
        if verdict == "ok":
            return game, name
    return None


def shown_id(values: list[str]) -> str:
    """Как показать ID клиенту и записать в заказ: «123456789 (1234)»."""
    if len(values) < 2:
        return values[0] if values else ""
    return f"{values[0]} ({', '.join(values[1:])})"


async def _volsever(game: db.Game, fields: dict[str, str]) -> tuple[str | None, str]:
    """Проверка через Volsever — он сделан ровно для этого.

    В отличие от справочников ников, работает не только с Free Fire и
    принимает второе поле: у Magic Chess и Mobile Legends аккаунт задан
    парой «ID игрока + сервер».
    """
    key = runtime.get("volsever_key") or db.settings.volsever_key
    if not key or not game.checker:
        return None, "unknown"

    values = list(fields.values())
    name, verdict, note = await volsever.check(
        key, game.checker, values[0] if values else "",
        values[1] if len(values) > 1 else "",
    )
    if note:
        log.info("Volsever: %s — %s", game.checker, note)
    return name, verdict


async def _lookup(
    provider, game: db.Game, fields: dict[str, str],
) -> tuple[str | None, str]:
    """Ник игрока. Сначала сервис выдачи, для Free Fire — отдельный источник.

    Блокируем покупку только на явном «такого игрока нет». Если ник просто
    не пришёл — продаём: пополнение идёт по ID, ник нужен для сверки глазами.
    """
    # Volsever спрашиваем первым: он для этого и сделан, и знает больше
    # игр, чем поставщик выдачи.
    name, verdict = await _volsever(game, fields)
    if verdict == "ok" and name:
        return name, "ok"
    refused = verdict == "bad"

    name, verdict = await provider.validate_game_id(game.category_id, fields)
    if verdict == "ok" and name:
        return name, "ok"
    if verdict == "bad":
        return None, "bad"

    if "free_fire" in game.category_id or "freefire" in game.category_id:
        key = runtime.get("gameskinbo_key") or db.settings.gameskinbo_key
        community = (runtime.get("ff_community_key")
                     or db.settings.ff_community_key)
        # Регион берём из кода категории: он там точнее, чем в подсказке.
        region = regions.nick_region(game) or game.region
        player = next(iter(fields.values()), "")
        found = await nicknames.free_fire(player, key=key, region=region,
                                          community_key=community)
        if found.verdict == "ok":
            return found.name, "ok"
        # А вот «нет такого» от него покупку НЕ рубит. Это бесплатный
        # сторонний справочник: он знает не все регионы и спокойно
        # отвечает «не найден» на живого игрока. Пополнение идёт по ID
        # через поставщика — его слово здесь единственное весомое.
        log.info("Игры: справочник ников не нашёл %s (%s) — продаём дальше",
                 player, found.verdict)
    # Отказ Volsever учитываем только здесь, в самом конце: у него могла
    # быть не та игра в настройке, а поставщик выдачи — промолчать.
    return None, ("bad" if refused else "unknown")


@router.callback_query(Game.confirm, F.data == "g:ok")
async def cb_buy(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider, bot: Bot,
) -> None:
    from app.handlers.menu import main_markup
    from app.services import delivery

    data = await state.get_data()
    game = await db.get_game(conn, data.get("category_id", ""))
    if game is None:
        await state.clear()
        await call.answer("Ин бозӣ дигар фурӯхта намешавад.", show_alert=True)
        return
    await state.clear()
    # Игры списываются с того счёта, чей ключ задан для игр.
    provider = suppliers.for_games(provider)

    if not await db.charge(conn, call.from_user.id, data["price"]):
        user = await db.get_user(conn, call.from_user.id)
        balance = user.balance if user else 0
        await call.message.edit_text(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(data["price"]), balance=fmt(balance),
                missing=fmt(data["price"] - balance),
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        await call.answer()
        return

    # Деньги списаны строкой выше. Заказ может не завестись — занятая
    # база, кончившийся диск, — и тогда клиент остался бы без денег и без
    # заказа, а мы без следа. Возвращаем сразу.
    try:
        order = await db.create_order(
            conn, user_id=call.from_user.id, product_type=game.product_type,
            quantity=1, recipient=data["player"],
            price=data["price"], cost=data.get("cost", 0),
        )
    except Exception:
        await db.credit(conn, call.from_user.id, data["price"])
        raise
    await call.answer()

    try:
        external = await svc.place(
            provider, game=game, offer_id=data["offer_id"],
            fields=data.get("fields") or {game.field_names[0]: data["player"]},
            quantity=1, order_id=order.id, created_at=order.created_at,
        )
    except DeliveryError as exc:
        # Явный отказ — выдачи не было. Возврат, починка поля и письмо
        # владельцу живут в сервисе: тем же путём идут заказы по API.
        await svc.refund_place(bot, conn, order, game, exc)
        await call.message.answer(
            texts.REFUNDED.format(
                order_id=order.id, price=fmt(order.price), support=texts.support()
            ),
            reply_markup=keyboards.back(),
        )
        return
    except DeliveryUncertain as exc:
        # Номера заказа у поставщика нет — статус спросить нечем, и сам он
        # не разрешится. Зовём владельца сразу, а не когда сработает
        # возврат по таймауту.
        await svc.hold_place(bot, conn, order, game, exc, data["player"])
        await call.message.edit_text(
            texts.GAME_ACCEPTED.format(
                order_id=order.id, pack=data["pack"],
                player=data["player"], price=fmt(order.price), wait=svc.wait_text(game.title),
            )
        )
        return

    await db.update_order(conn, order.id, fragment_order_id=external)
    await call.message.edit_text(
        texts.GAME_ACCEPTED.format(
            order_id=order.id, pack=data["pack"],
            player=data["player"], price=fmt(order.price), wait=svc.wait_text(game.title),
        )
    )

    # Заказ почти всегда уходит в processing, поэтому проверяем сразу —
    # вдруг он уже готов, — а дальше за ним следит фоновая задача.
    fresh = await db.get_order(conn, order.id)
    done = False
    if fresh:
        done = await svc.check(bot, conn, provider, fresh) != "waiting"

    if not done:
        # Выдача обычно занимает секунды. Опрашиваем часто, чтобы клиент
        # узнал сразу, а не через пятиминутный обход.
        asyncio.create_task(svc.follow(bot, provider, order.id))

    await call.message.answer(
        "Меню:", reply_markup=await main_markup(conn)
    )
