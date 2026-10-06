"""Деньги. Балансы храним целыми числами в десятитысячных долях доллара (4 знака,
как у FazerCards: "15.3750"). Цены за единицу — строками Decimal, они бывают точнее
(цена одной звезды — 0.0150000)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

SCALE = 10_000  # 1 USD = 10 000 «микро»
FOUR = Decimal("0.0001")


class MoneyError(ValueError):
    pass


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise MoneyError(f"не число: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError(f"не число: {value!r}")
    return result


def to_micro(value) -> int:
    """Сумма → микро, с обычным округлением (для ввода админом)."""
    return int((to_decimal(value) * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_micro_ceil(value: Decimal) -> int:
    """Сумма → микро, округление вверх: клиенту никогда не выставляем меньше себестоимости."""
    return int((to_decimal(value) * SCALE).quantize(Decimal(1), rounding=ROUND_CEILING))


def fmt(micro: int) -> str:
    """Микро → строка "12.3456" (как в API поставщика)."""
    sign = "-" if micro < 0 else ""
    micro = abs(int(micro))
    return f"{sign}{micro // SCALE}.{micro % SCALE:04d}"


def apply_markup(base: Decimal, markup_pct: Decimal) -> Decimal:
    """Цена за единицу для клиента: закупка × (1 + наценка%)."""
    return to_decimal(base) * (Decimal(100) + to_decimal(markup_pct)) / Decimal(100)


def order_total_micro(unit_price: Decimal, quantity) -> int:
    return to_micro_ceil(to_decimal(unit_price) * to_decimal(quantity))


def fmt_unit(value: Decimal) -> str:
    """Цена за единицу для показа: не меньше 4 знаков, без хвостовых нулей после 4-го."""
    value = to_decimal(value)
    text = f"{value:.7f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(4, '0')}"
