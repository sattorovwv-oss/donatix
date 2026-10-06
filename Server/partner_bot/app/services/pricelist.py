"""Прайс списком: разложить присланный прайс по пакетам игры.

У PUBG и Mobile Legends пакетов тридцать с лишним. Заходить в каждый и
вбивать цену руками — полчаса тыканья, и на середине сбиваешься. Куда
проще прислать боту весь прайс сразу, как он выглядит в объявлении:

    60 UC - 10 сомонӣ
    120 UC - 21
    325 UC — 45 с.

Разбор нарочно осторожный: строки, для которых пакет не нашёлся
однозначно, не применяются молча, а показываются владельцу. Тихо
поставить цену не тому пакету — хуже, чем не поставить вовсе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from app.money import parse

#: Строка прайса: название, разделитель, цена и (не обязательно) валюта.
#: Название берём нежадно, чтобы «Elite Pass (уровни 1-100) - 250»
#: разошлось по последнему тире, а не по тому, что внутри скобок.
LINE = re.compile(
    r"^(?P<name>.*?)[\s\-–—―:=]+"
    r"(?P<price>\d{1,6}(?:[.,]\d{1,2})?)"
    r"\s*(?:сомонӣ|сомони|somoni|сомон|сом|tjs|с)?\.?\s*$",
    re.IGNORECASE,
)

#: Мусор, который часто приезжает вместе с прайсом.
SKIP = re.compile(r"^[\s\W_]*$|^(прайс|price|нарх|цены|цена)\b", re.IGNORECASE)

#: Сколько строк разбираем за раз. Прайс длиннее — почти наверняка
#: прислали не прайс, а переписку.
MAX_LINES = 200


@dataclass
class Row:
    """Одна разобранная строка прайса."""
    line: str
    name: str
    price: int                      # в дирамах
    offer: dict | None = None
    why: str = ""                   # почему не легла, если не легла


@dataclass
class Plan:
    """Что случится, если применить прайс."""
    rows: list[Row] = dc_field(default_factory=list)
    bad: list[str] = dc_field(default_factory=list)

    @property
    def matched(self) -> list[Row]:
        return [r for r in self.rows if r.offer is not None]

    @property
    def lost(self) -> list[Row]:
        return [r for r in self.rows if r.offer is None]

    @property
    def changed(self) -> list[Row]:
        """Только те, где цена правда другая."""
        return [r for r in self.matched if r.offer["price"] != r.price]


def norm(text: str) -> str:
    """Название к виду, по которому его можно сравнивать.

    Значки, регистр и знаки препинания у нас и у поставщика разные —
    сравнивать по ним нельзя. Остаются буквы, цифры и пробелы.
    """
    low = text.lower().replace("ё", "е")
    keep = [ch if ch.isalnum() else " " for ch in low]
    return " ".join("".join(keep).split())


def numbers(text: str) -> tuple[int, ...]:
    """Числа названия: «1320 UC» -> (1320,).

    Для пакетов число — главное, что их различает. Совпали числа —
    почти наверняка это тот же пакет.
    """
    return tuple(int(n) for n in re.findall(r"\d+", text))


def words(text: str) -> set[str]:
    """Буквенные слова названия: «1320 UC» -> {'uc'}."""
    return {w for w in norm(text).split() if not w.isdigit()}


def parse_line(line: str) -> tuple[str, int] | None:
    """Строка прайса -> (название, цена в дирамах)."""
    hit = LINE.match(line.strip())
    if hit is None:
        return None
    name = hit.group("name").strip(" \t.-–—:=•*·")
    price = parse(hit.group("price"))
    if not name or price is None or price <= 0:
        return None
    return name, price


def _candidates(name: str, pool: list[dict]) -> list[dict]:
    """Пакеты, подходящие под название — от точного совпадения к слабым.

    Уровни перебираем по очереди и останавливаемся на первом, где
    хоть кто-то нашёлся: слабое совпадение не должно спорить с точным.
    """
    want, want_num, want_words = norm(name), numbers(name), words(name)

    def both(offer: dict) -> list[str]:
        return [offer["name"], offer.get("supplier_name") or ""]

    levels = [
        # то же название
        lambda o: any(norm(t) == want for t in both(o) if t),
        # одно название внутри другого и числа те же
        lambda o: any(
            (want in norm(t) or norm(t) in want) and numbers(t) == want_num
            for t in both(o) if t
        ),
        # те же числа и общее слово: «1320 UC» и «PUBG 1320 UC»
        lambda o: bool(want_num) and any(
            numbers(t) == want_num and (words(t) & want_words)
            for t in both(o) if t
        ),
        # просто те же числа — берём, только если такой пакет один
        lambda o: bool(want_num) and any(
            numbers(t) == want_num for t in both(o) if t
        ),
    ]
    for fits in levels:
        found = [o for o in pool if fits(o)]
        if found:
            return found
    return []


def build(text: str, offers: list[dict]) -> Plan:
    """Разобрать прайс и разложить его по пакетам.

    Пакет занимается один раз: две строки на один и тот же пакет —
    это опечатка в прайсе, и вторую лучше показать владельцу.
    """
    plan = Plan()
    pool = list(offers)

    for line in text.splitlines()[:MAX_LINES]:
        line = line.strip()
        if not line or SKIP.match(line):
            continue
        parsed = parse_line(line)
        if parsed is None:
            plan.bad.append(line)
            continue

        name, price = parsed
        row = Row(line=line, name=name, price=price)
        found = _candidates(name, pool)
        if not found:
            row.why = "пакет не нашёлся"
        elif len(found) > 1:
            row.why = "подходит сразу несколько"
        else:
            row.offer = found[0]
            pool.remove(found[0])
        plan.rows.append(row)

    return plan
