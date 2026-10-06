"""Названия игровых пакетов по-русски, значок и порядок на витрине.

Поставщик присылает названия по-английски: «100 Diamonds», «Weekly Membership»,
«Level Up Pass». Покупателю понятнее «💎 100 алмазов», «🎫 Ваучер на неделю»,
«🚀 Прокачка уровня». Порядок: сначала валюта игры (алмазы, UC), потом ваучеры
и пропуска, потом прокачка, остальное в конце; внутри группы — по количеству/цене.

Перевод только для показа: в заказе поставщику уходит его собственный id пакета.
"""

from __future__ import annotations

import re

CURRENCY, PASS, LEVEL, OTHER = 0, 1, 2, 3
GROUP_TITLES = {CURRENCY: "Алмазы и валюта", PASS: "Ваучеры и пропуска", LEVEL: "Прокачка", OTHER: "Другое"}
GROUP_EMOJI = {CURRENCY: "💎", PASS: "🎫", LEVEL: "🚀", OTHER: "🎁"}

_NUM = r"(\d[\d\s.,]*\d|\d)"
_DIAMONDS = re.compile(rf"{_NUM}\s*(?:\+\s*{_NUM})?\s*(?:diamonds?|dm|алмаз\w*|💎)", re.I)
_DIAMONDS_BEFORE = re.compile(rf"(?:diamonds?|алмаз\w*|💎)\s*[x×:]?\s*{_NUM}", re.I)
_UC = re.compile(rf"{_NUM}\s*(?:\+\s*{_NUM})?\s*(?:uc|unknown cash)\b", re.I)

_LEVEL = re.compile(r"level[\s-]*up|прокач|\bevo\b|эволюц|upgrade|апгрейд|уровн", re.I)
_PASS = re.compile(r"voucher|ваучер|membership|weekly|monthly|недел|месяч|pass\b|пропуск|booyah|prime|"
                   r"подписк|airdrop|аирдроп", re.I)

# Фразы целиком — от длинных к коротким, чтобы «Weekly Lite» не стал просто «Weekly»
_PHRASES: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), r) for p, r in (
    (r"weekly\s+lite\s+(membership|voucher)", "Ваучер на неделю (лайт)"),
    (r"weekly\s+(membership|voucher|pass)", "Ваучер на неделю"),
    (r"monthly\s+(membership|voucher|pass)", "Ваучер на месяц"),
    (r"^weekly$", "Ваучер на неделю"),
    (r"^monthly$", "Ваучер на месяц"),
    (r"level[\s-]*up\s+pass", "Прокачка уровня"),
    (r"booyah\s+pass", "Booyah Pass — боевой пропуск"),
    (r"elite\s+pass\s+plus", "Элитный пропуск Plus"),
    (r"elite\s+pass", "Элитный пропуск"),
    (r"royale\s+pass", "Royale Pass — боевой пропуск"),
    (r"prime\s+plus", "Prime Plus — подписка"),
    (r"^prime$", "Prime — подписка"),
    (r"super\s+airdrop", "Супер аирдроп"),
    (r"airdrop", "Аирдроп"),
    (r"evo\s+access", "Доступ к Evo-оружию"),
    (r"first\s+top[\s-]*up", "первое пополнение"),
    (r"\blevels?\s+(\d+)\s*[-–]\s*(\d+)", r"уровни \1–\2"),
    (r"\b(\d+)\s*d(ays?)?\b", r"\1 дн."),
    (r"\b(\d+)\s*h(ours?)?\b", r"\1 ч"),
)]


def _int(raw: str) -> int:
    try:
        return int(re.sub(r"[\s.,]", "", raw))
    except ValueError:
        return 0


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def group(name: str) -> int:
    """0 — алмазы/UC, 1 — ваучеры и пропуска, 2 — прокачка, 3 — прочее."""
    text = name or ""
    if _LEVEL.search(text):          # «Level Up Pass» — это прокачка, хоть и «pass»
        return LEVEL
    if _DIAMONDS.search(text) or _DIAMONDS_BEFORE.search(text) or _UC.search(text):
        return CURRENCY
    if _PASS.search(text):
        return PASS
    if re.fullmatch(rf"\s*{_NUM}\s*(\+\s*{_NUM})?\s*", text):   # «100» или «100 + 10» — это валюта
        return CURRENCY
    return OTHER


def _amount(name: str) -> int:
    m = _DIAMONDS.search(name) or _UC.search(name)
    if m:
        return _int(m.group(1)) + (_int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0)
    m = _DIAMONDS_BEFORE.search(name) or re.search(_NUM, name)
    return _int(m.group(1)) if m else 0


def label(name: str) -> str:
    """Название по-русски, без значка: «100 Diamonds» → «100 алмазов»."""
    raw = (name or "").strip()
    m = _DIAMONDS.search(raw)
    if m:
        base, bonus = _int(m.group(1)), (_int(m.group(2)) if m.group(2) else 0)
        word = plural(base + bonus, "алмаз", "алмаза", "алмазов")
        return f"{base} + {bonus} {word}" if bonus else f"{base} {word}"
    m = _DIAMONDS_BEFORE.search(raw)
    if m:
        n = _int(m.group(1))
        return f"{n} {plural(n, 'алмаз', 'алмаза', 'алмазов')}"
    m = _UC.search(raw)
    if m:
        base, bonus = _int(m.group(1)), (_int(m.group(2)) if m.group(2) else 0)
        return f"{base} + {bonus} UC" if bonus else f"{base} UC"
    text = raw
    for rx, repl in _PHRASES:
        text = rx.sub(repl, text)
    # «Прокачка уровня (Level Up Pass)» после перевода → «Прокачка уровня (Прокачка уровня)» — повтор убираем
    text = re.sub(r"^(.+?)\s*\(\1\)$", r"\1", text, flags=re.I).strip()
    return text[:1].upper() + text[1:] if text else raw


def emoji(name: str) -> str:
    g = group(name)
    if g == CURRENCY and _UC.search(name or ""):
        return "🪙"
    return GROUP_EMOJI[g]


def full(name: str) -> str:
    """Со значком: «💎 100 алмазов»."""
    return f"{emoji(name)} {label(name)}"


def order_key(name: str, price: float = 0) -> tuple[int, float, float, str]:
    """Для сортировки: группа, количество (валюта), цена, название."""
    g = group(name)
    return g, (_amount(name) if g == CURRENCY else 0), price, label(name)
