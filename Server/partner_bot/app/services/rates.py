"""Курс доллара к сомони из бесплатных источников — без ключей и регистраций.

Зачем: себестоимость на стороне сервиса выдачи в долларах, продаём за сомони.
Пока курс вбит руками, он устаревает, и наценка расползается вместе с ним.

Источники пробуем по очереди — первый ответивший выигрывает. Все три
бесплатные и не требуют ключа; если один ляжет, останутся остальные.

Важно: это биржевой курс. В Душанбе доллар в обменнике дороже, поэтому
есть надбавка (`usd_rate_spread`) — процент, который прибавляется к
полученному курсу, чтобы он сошёлся с тем, по которому вы реально
покупаете.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)

#: Разумные границы курса TJS/USD. За ними — явно мусор в ответе, а не курс:
#: применить такое молча значит поломать все цены разом.
MIN_RATE = Decimal("2")
MAX_RATE = Decimal("100")


@dataclass
class Rate:
    diram: int          # сколько дирам в одном долларе (10.90 сомони = 1090)
    source: str         # человекочитаемое имя источника
    value: Decimal      # исходное число, как отдал источник


def _dig(data, path: list[str]):
    """Достать вложенное значение по пути, не падая на любом промахе."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
        if data is None:
            return None
    return data


#: (имя, url, путь до числа внутри JSON) — сколько сомони в долларе
SOURCES: list[tuple[str, str, list[str]]] = [
    ("open.er-api.com",
     "https://open.er-api.com/v6/latest/USD", ["rates", "TJS"]),
    ("currency-api (jsDelivr)",
     "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
     ["usd", "tjs"]),
    ("currency-api (зеркало)",
     "https://latest.currency-api.pages.dev/v1/currencies/usd.json",
     ["usd", "tjs"]),
]



async def _read(session: aiohttp.ClientSession, url: str, path: list[str]) -> Decimal | None:
    async with session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        # Некоторые из этих адресов отдают JSON под text/plain.
        data = await response.json(content_type=None)
    raw = _dig(data, path)
    if raw is None:
        raise RuntimeError("в ответе нет курса TJS")
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise RuntimeError(f"курс не похож на число: {raw!r}") from exc


async def fetch(spread_percent: int = 0) -> Rate:
    """Курс доллара к сомони. Бросает RuntimeError, если все молчат."""
    return await _fetch(SOURCES, MIN_RATE, MAX_RATE, spread_percent)


async def _fetch(
    sources: list, low: Decimal, high: Decimal, spread_percent: int,
) -> Rate:
    problems = []
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for name, url, path in sources:
            try:
                value = await _read(session, url, path)
            except Exception as exc:  # noqa: BLE001 — пробуем следующий источник
                problems.append(f"{name}: {exc}")
                log.info("Курс от %s не пришёл: %s", name, exc)
                continue

            if not low <= value <= high:
                problems.append(f"{name}: {value} — вне разумных границ")
                continue

            with_spread = value * (100 + max(spread_percent, 0)) / 100
            return Rate(diram=int(with_spread * 100), source=name, value=value)

    raise RuntimeError("; ".join(problems) or "источники не ответили")
