"""Каталог товаров для API.

Своей таблицы товаров у бота нет и заводить её нельзя: звёзды считаются
по цене за штуку, Premium живёт в настройках, игровые пакеты приходят от
поставщика и меняются каждый день. Таблица-копия разъехалась бы с
настоящими ценами в первый же час, и разработчик покупал бы по цене,
которой уже нет.

Поэтому каталог собирается на лету из тех же источников, что и кнопки в
боте, а API отдаёт его в одном формате.

Деньги в ответах — целые числа в дирамах (1 сомони = 100 дирам). Дробных
чисел в деньгах нет нарочно: 0.1 + 0.2 в любом языке даёт не 0.3, и
разработчик, сложив цены на своей стороне, получил бы копеечные расхождения.
Рядом кладём amount_text — строку для показа человеку.
"""
from __future__ import annotations

import asyncio
import time
from decimal import ROUND_HALF_UP, Decimal

from app import db, runtime
from app.money import fmt, stars_cost, steam_cost

#: Валюта расчётов. Меняется вместе с настройками бота.
def currency() -> str:
    from app.config import settings

    return getattr(settings, "currency_code", "") or "TJS"


def money(diram: int) -> dict:
    """Одна сумма в двух видах: для расчётов и для показа.

    Знак выносим перед числом руками: у целочисленного деления в Python
    −779 дирам превратились бы в «−8.21» вместо «−7.79», и списание в
    выписке показывалось бы неверной суммой.
    """
    whole, part = divmod(abs(diram), 100)
    sign = "-" if diram < 0 else ""
    return {"amount": diram, "amount_text": f"{sign}{whole}.{part:02d}",
            "currency": currency()}


def _stars() -> dict | None:
    if not runtime.get_bool("stars_enabled") or runtime.star_price_e4() <= 0:
        return None
    lo, hi = runtime.min_stars(), runtime.max_stars()
    return {
        "id": "stars",
        "type": "stars",
        "name": "Telegram Stars",
        "unit": "star",
        "variable": True,
        "min_quantity": lo,
        "max_quantity": hi,
        "unit_price": runtime.star_price_e4(),   # десятитысячные сомони
        "price_example": money(stars_cost(lo)) | {"quantity": lo},
        "cost_unit_e4": runtime.star_cost_e4(),  # внутреннее: в ответ не уходит
        "customer": "Юзернейм получателя в Telegram, например @durov",
        "customer_required": True,
    }


def _premium() -> list[dict]:
    if not runtime.get_bool("premium_enabled"):
        return []
    out = []
    for plan in runtime.premium_plans():
        months = int(plan["months"])
        out.append({
            "id": f"premium:{months}",
            "type": "premium",
            "name": f"Telegram Premium — {months} мес.",
            "unit": "month",
            "variable": False,
            "quantity": months,
            "customer": "Юзернейм получателя в Telegram, например @durov",
            "customer_required": True,
            "cost": runtime.cost_of("premium", months),   # внутреннее
            **money(int(plan["price"])),
        })
    return out


def _steam() -> list[dict]:
    if not runtime.steam_on():
        return []
    out = []
    for amount in runtime.steam_packs():
        out.append({
            "id": f"steam:{amount}",
            "type": "steam",
            "name": f"Steam — {amount} {runtime.steam_currency()}",
            "unit": runtime.steam_currency(),
            "variable": False,
            "quantity": amount,
            "customer": "Логин аккаунта Steam",
            "customer_required": True,
            "cost": runtime.cost_of("steam", amount),     # внутреннее
            **money(steam_cost(amount)),
        })
    return out


def _game_item(game, offer: dict, shadow: bool = False) -> dict:
    """Один пакет игры в виде товара API."""
    return {
        "id": f"game:{game.category_id}:{offer['offer_id']}",
        "type": "game",
        "name": f"{game.title} — {offer['name']}",
        "game": game.title,
        "game_id": game.category_id,
        "unit": "pack",
        "variable": False,
        "quantity": 1,
        "customer": ", ".join(game.field_names),
        "customer_required": True,
        "fields": list(game.field_names),
        "cost": offer["cost"],       # внутреннее: в ответ не уходит
        "shadow": shadow,            # внутреннее: игры нет в боте
        **money(offer["price"]),
    }


async def _games(conn, provider) -> list[dict]:
    """Игровые пакеты. Молчим о сломанном поставщике: остальной каталог
    должен отдаваться, даже если игры сейчас не отвечают."""
    from app.handlers.games import offers_of
    from app.services import suppliers

    if not runtime.get_bool("games_enabled"):
        return []
    if runtime.get_bool("api_all_games"):
        return await all_supplier_games(conn, provider)

    out = []
    for game in await db.list_games(conn, only_enabled=True):
        try:
            # Из памяти: каталог просят часто, а у поставщика он меняется
            # раз в дни. Живьём его тянет только владелец в панели.
            offers = await offers_of(suppliers.for_games(provider), game, conn,
                                      cached=True)
        except Exception:  # noqa: BLE001 — одна игра не должна гасить каталог
            continue
        out += [_game_item(game, offer) for offer in offers]
    return out


# ──────────────────────────────────── весь каталог поставщика

#: Пакеты каждой категории — отдельный запрос к поставщику, а категорий
#: у него десятки. Поэтому собранный каталог живёт FULL_TTL, собирается
#: строго в одном потоке (иначе десяток разработчиков устроил бы
#: поставщику лавину) и по нескольку категорий за раз.
FULL_TTL = 10 * 60
FETCH_AT_ONCE = 8
#: Предел на всякий случай: ответ на сотню тысяч позиций не осилит ни
#: наш сервер, ни клиент на той стороне.
MAX_GAME_ITEMS = 5000

_full: tuple[float, list[dict]] | None = None
_building = asyncio.Lock()


def forget_full() -> None:
    """Забыть собранный каталог — после смены ключа или правки игры."""
    global _full
    _full = None


def _shadow_game(entry: dict):
    """Игра, которой в боте нет: берём её прямо из каталога поставщика.

    Запись в базу не заводим до первой покупки — иначе один заход в
    каталог засорил бы список игр владельца сотней строк, которые он не
    заводил.
    """
    names = []
    for field in entry.get("fields") or []:
        name = field.get("name") if isinstance(field, dict) else field
        if name:
            names.append(str(name))
    code = str(entry.get("category_id") or "")
    return db.Game(
        category_id=code, title=str(entry.get("name") or code),
        field=",".join(names), region="", margin=0, enabled=0, created_at="",
    )


async def all_supplier_games(conn, provider) -> list[dict]:
    """Весь игровой каталог поставщика, а не только включённое в боте."""
    global _full

    if _full and time.time() - _full[0] < FULL_TTL:
        return _full[1]
    async with _building:
        # Пока ждали очереди, каталог мог собрать кто-то другой.
        if _full and time.time() - _full[0] < FULL_TTL:
            return _full[1]
        items = await _build_supplier_games(conn, provider)
        _full = (time.time(), items)
        return items


async def _build_supplier_games(conn, provider) -> list[dict]:
    from app.handlers.games import offers_of
    from app.services import games as gsvc
    from app.services import suppliers

    supplier = suppliers.for_games(provider)
    try:
        categories = await gsvc.full_catalog(supplier, cached=True)
    except Exception:  # noqa: BLE001 — поставщик молчит, каталог не пуст
        return []

    # Настройки владельца важнее каталога поставщика: где игра заведена
    # в боте, берём её название, наценку и свои цены пакетов.
    known = {game.category_id: game for game in await db.list_games(conn)}
    gate = asyncio.Semaphore(FETCH_AT_ONCE)

    async def one(entry: dict) -> list[dict]:
        code = str(entry.get("category_id") or "")
        if not code:
            return []
        game = known.get(code)
        shadow = game is None
        if shadow:
            game = _shadow_game(entry)
        async with gate:
            try:
                offers = await offers_of(supplier, game, conn, cached=True)
            except Exception:  # noqa: BLE001 — одна игра не гасит каталог
                return []
        return [_game_item(game, offer, shadow) for offer in offers]

    out: list[dict] = []
    for chunk in await asyncio.gather(*(one(entry) for entry in categories)):
        out += chunk
        if len(out) >= MAX_GAME_ITEMS:
            return out[:MAX_GAME_ITEMS]
    return out


async def game_for(conn, item: dict):
    """Игра под товар из каталога. None — её больше нет.

    Игру, которой в боте не было, заводим здесь, при первой покупке:
    дальше всё — выдача, разбор отказа, починка полей, отчёты — идёт по
    общей дороге с играми из бота. Заводим выключенной: в меню бота она
    не появится, пока владелец сам её не включит.
    """
    game = await db.get_game(conn, item["game_id"])
    if game is not None:
        return game
    if not item.get("shadow"):
        return None
    return await db.add_game(
        conn, category_id=item["game_id"], title=item["game"],
        field=",".join(item.get("fields") or ["user_id"]),
    )


#: Поля, которые наружу не уходят никогда: себестоимость и служебные
#: пометки. Перечислены явно — новое внутреннее поле лучше пусть
#: сломает проверку, чем однажды тихо уедет разработчику.
HIDDEN = ("cost", "cost_unit_e4", "wholesale", "shadow")


def usd_rate() -> int:
    """Курс доллара в дирамах. 0 — курс не задан."""
    return runtime.usd_rate()


def public(item: dict) -> dict:
    """Товар так, как его видит чужой разработчик.

    Себестоимость и внутренние поля не отдаём: сколько товар стоит нам —
    не его дело, и по этой цифре считается наша наценка.

    Рядом с ценой в сомони кладём её же в долларах. Конкуренты
    разработчика считают в долларах, и без этого он не может сравнить
    наши цены со своими, не заводя себе калькулятор курса. Курс берётся
    наш собственный — тот, по которому мы сами считаем.
    """
    out = {k: v for k, v in item.items() if k not in HIDDEN}
    rate = usd_rate()
    if rate <= 0:
        return out
    if "amount" in out:
        out["usd"] = round(out["amount"] / rate, 4)
    elif out.get("unit_price"):
        # Цена за штуку живёт в десятитысячных сомони, а курс — в дирамах.
        out["usd_per_unit"] = round(out["unit_price"] / 100 / rate, 6)
    return out


def margin_percent() -> int:
    """Наценка для разработчиков. 0 — продаём им по ценам витрины."""
    return runtime.api_margin_percent()


def _plus(value: int, percent: int) -> int:
    """value плюс percent процентов, без дробных денег."""
    return int((Decimal(value) * (100 + percent) / 100)
               .to_integral_value(rounding=ROUND_HALF_UP))


def _stars_total(unit_e4: int, quantity: int) -> int:
    """Цена за quantity звёзд из цены за штуку в десятитысячных."""
    return (unit_e4 * quantity + 50) // 100


def wholesale(item: dict) -> dict:
    """Пересчитать цену от себестоимости, если задана наценка для API.

    Разработчик перепродаёт наш товар, и на витринной цене бота ему
    заработать нечем — он просто уйдёт. Поэтому у API своя цена:
    закупка плюс заданный процент.

    Витринные приглаживания (до 5 или 10 дирам) тут не применяем нарочно:
    разработчик складывает цены у себя, и ему нужна ровно та сумма, что
    спишется, а не красивая.

    Там, где себестоимость неизвестна — поставщик её не отдал, цена
    выставлена руками, — оставляем цену бота. Продать ниже закупки хуже,
    чем продать дороже, чем хотелось.
    """
    percent = margin_percent()
    if percent <= 0:
        return item

    if item["type"] == "stars":
        cost_e4 = int(item.get("cost_unit_e4", 0))
        if cost_e4 <= 0:
            return item
        item = dict(item)
        item["unit_price"] = max(_plus(cost_e4, percent), cost_e4)
        low = int(item["min_quantity"])
        item["price_example"] = (money(_stars_total(item["unit_price"], low))
                                 | {"quantity": low})
        item["wholesale"] = True
        return item

    cost = int(item.get("cost", 0))
    if cost <= 0:
        return item
    item = dict(item)
    # max с себестоимостью — страховка от округления вниз на копеечных
    # товарах: ниже закупки цена не опустится ни при каком проценте.
    item.update(money(max(_plus(cost, percent), cost)))
    item["wholesale"] = True
    return item


async def listing(conn, provider, kind: str = "") -> list[dict]:
    """Весь каталог или одна его часть."""
    items: list[dict] = []
    stars = _stars()
    if stars:
        items.append(stars)
    items += _premium()
    items += _steam()
    items += await _games(conn, provider)
    # Пересчёт здесь, а не в каждом сборщике: и каталог, и поиск товара,
    # и списание при заказе идут через этот список — цена не разойдётся
    # с той, что разработчик увидел секунду назад.
    items = [wholesale(item) for item in items]
    return [item for item in items if not kind or item["type"] == kind]


async def find(conn, provider, product_id: str) -> dict | None:
    """Один товар по его id. Ищем по тому же списку, что и отдаём:
    иначе появился бы товар, который виден в каталоге, но не покупается."""
    for item in await listing(conn, provider):
        if item["id"] == product_id:
            return item
    return None


def price_of(item: dict, quantity: int) -> int:
    """Во сколько обойдётся quantity этого товара, в дирамах.

    Считаем по цене самого товара, а не по витрине: товар пришёл из
    listing(), где цена уже могла быть пересчитана под API.
    """
    if item["type"] == "stars":
        if item.get("wholesale"):
            return _stars_total(int(item["unit_price"]), quantity)
        return stars_cost(quantity)
    return int(item["amount"])


def order_plan(item: dict, quantity: int) -> tuple[str, int]:
    """Во что превращается товар при заказе: (product_type, quantity).

    product_type — тот же, что у заказов из бота, чтобы отчёты, возвраты
    и статистика считали их вместе, а не двумя отдельными кучами.
    """
    if item["type"] == "stars":
        return "stars", quantity
    if item["type"] == "premium":
        return "premium", int(item["quantity"])
    if item["type"] == "steam":
        return "steam", int(item["quantity"])
    _, category_id, _offer = item["id"].split(":", 2)
    return f"game:{category_id}", 1


def offer_id_of(item: dict) -> str:
    return item["id"].split(":", 2)[2] if item["type"] == "game" else ""
