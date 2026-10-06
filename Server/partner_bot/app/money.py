"""Деньги хранятся в дирамах (1 сомони = 100 дирам) целыми числами.

Float для денег не используем никогда: 0.1 + 0.2 != 0.3, а на балансе
пользователя такие копейки превращаются в расхождение с кассой.
"""
from __future__ import annotations

import re

from app import runtime
from app.config import settings

_AMOUNT_RE = re.compile(r"^\d{1,9}([.,]\d{1,2})?$")


def parse(raw: str) -> int | None:
    """'12.50' -> 1250 дирам. None, если строка не похожа на сумму."""
    text = raw.strip().replace(" ", "")
    if not _AMOUNT_RE.match(text):
        return None
    text = text.replace(",", ".")
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int(frac.ljust(2, "0")) if frac else int(whole) * 100


def fmt(diram: int) -> str:
    """1250 -> '12.50 с.'"""
    sign = "-" if diram < 0 else ""
    whole, frac = divmod(abs(diram), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f".{frac:02d} {settings.currency}"


def fmt_short(diram: int) -> str:
    """То же, но без копеек, когда их нет: 1000 -> '10 с.'

    Нужно для кнопок: их по две в ряд, и лишние «.00» съедают место,
    из-за которого обрезается название пакета.
    """
    whole, frac = divmod(abs(diram), 100)
    if frac:
        return fmt(diram)
    sign = "-" if diram < 0 else ""
    return f"{sign}{whole:,}".replace(",", " ") + f" {settings.currency}"


def parse4(raw: str) -> int | None:
    """'0.1629' -> 1629 (десятитысячные сомони). None, если не число."""
    text = raw.strip().replace(" ", "").replace(",", ".")
    if not re.match(r"^\d{1,7}(\.\d{1,4})?$", text):
        return None
    whole, _, frac = text.partition(".")
    return int(whole) * 10_000 + int(frac.ljust(4, "0")) if frac else int(whole) * 10_000


def fmt4(e4: int) -> str:
    """Цена за штуку с четырьмя знаками: 1629 -> '0.1629 с.'

    Дирама для цены одной звезды мало: он равен ~7% её стоимости, и
    наценки 10% и 15% округлялись бы в одно и то же число.
    """
    whole, frac = divmod(abs(e4), 10_000)
    sign = "-" if e4 < 0 else ""
    return f"{sign}{whole}.{frac:04d} {settings.currency}"


#: Как приглаживать итоговую сумму заказа: ключ -> (шаг в дирамах, вверх ли)
ROUNDING: dict[str, tuple[int, bool]] = {
    "off":   (0, False),     # как посчиталось, до дирама
    "up1":   (100, True),    # вверх до целого сомони
    "near1": (100, False),   # к ближайшему сомони
    "up5":   (500, True),    # вверх до пяти сомони
}


def round_price(diram: int, mode: str | None = None) -> int:
    """Пригладить сумму по выбранному в панели правилу.

    Считаем от точной суммы, а не от уже приглаженной: иначе цена ползла бы
    вверх при каждом пересчёте. mode задаётся явно только для примеров в
    панели — в остальных местах берётся из настроек.
    """
    step, up = ROUNDING.get(mode or runtime.get("round_prices") or "off", (0, False))
    if step <= 0 or diram <= 0:
        return diram
    if up:
        return -(-diram // step) * step        # деление вверх, без float
    return (diram + step // 2) // step * step


def stars_cost(quantity: int) -> int:
    """Стоимость quantity звёзд в дирамах.

    Цена хранится в десятитысячных сомони, поэтому округляем один раз —
    на итоговой сумме, а не на каждой звезде.
    """
    return round_price(exact_stars_cost(quantity))


def exact_stars_cost(quantity: int) -> int:
    """Цена без приглаживания — от неё считается округление."""
    total_e4 = quantity * runtime.star_price_e4()
    return (total_e4 + 50) // 100          # e4 -> дирамы, с округлением


def steam_cost(amount: int) -> int:
    """Сколько сомони стоит пополнение Steam на amount единиц его валюты."""
    return round_price((amount * runtime.steam_price_e4() + 50) // 100)


def affordable_stars(balance: int) -> int:
    """Сколько звёзд можно купить на данный баланс.

    Приглаживание вверх делает заказ дороже точной цены, поэтому идём вниз,
    пока сумма правда влезает: обещать больше, чем можно купить, нельзя.
    """
    price_e4 = runtime.star_price_e4()
    if price_e4 <= 0:
        return 0
    quantity = balance * 100 // price_e4
    while quantity > 0 and stars_cost(quantity) > balance:
        # Шаг округления делится на цену звезды — столько штук и лишние.
        quantity -= max((stars_cost(quantity) - balance) * 100 // price_e4, 1)
    return max(quantity, 0)


def discount_of(price: int, percent: int) -> int:
    """Размер скидки в дирамах. Округляем к ближайшему дираму."""
    if percent <= 0 or price <= 0:
        return 0
    return min((price * percent + 50) // 100, price)
