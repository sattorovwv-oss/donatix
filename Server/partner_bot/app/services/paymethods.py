"""Способы оплаты для пополнения баланса покупателями: банк, номер, владелец.

Их может быть сколько угодно (Душанбе Сити, Алиф, Эсхата…). Покупатель выбирает
банк, вводит сумму и видит реквизиты именно этого банка. Хранятся в настройках
бота одним JSON — отдельная таблица для десятка строк не нужна.

Первый включённый способ дублируется в старые настройки pay_card_* — на них
опираются проверки готовности и прочие места бота.
"""
from __future__ import annotations

import json
import re
import secrets

import aiosqlite

from app import runtime

KEY = "pay_methods_json"

#: Банки для кнопок мастера. «Другой» — ввести название самому.
BANKS = ["Душанбе Сити", "Алиф", "Эсхата", "Корти Милли", "Спитамен", "Амонатбонк",
         "Humo", "Ориёнбонк", "Арванд", "Tcell / Babilon", "USDT TRC20", "USDT BEP20"]

#: Криптовалюта: название → (сеть, как её показать, как выглядит адрес).
#: Перевод в чужой сети теряется, поэтому сеть видна везде и адрес проверяется.
CRYPTO = {
    "USDT TRC20": ("TRC20", "Tron (TRC20)", r"T[1-9A-HJ-NP-Za-km-z]{33}", "начинается с T, 34 символа"),
    "USDT BEP20": ("BEP20", "BNB Smart Chain (BEP20)", r"0x[0-9a-fA-F]{40}", "начинается с 0x, 42 символа"),
}


def crypto(bank: str) -> tuple[str, str, str, str] | None:
    """Сеть криптоспособа или None для банка."""
    return CRYPTO.get((bank or "").strip())


def clean_wallet(bank: str, text: str) -> str | None:
    net = crypto(bank)
    raw = (text or "").strip()
    return raw if net and re.fullmatch(net[2], raw) else None


def network_warning(bank: str) -> str:
    net = crypto(bank)
    return f"Отправляйте USDT только в сети {net[1]} — в другой сети деньги не дойдут." if net else ""


def all_methods() -> list[dict]:
    raw = runtime.get(KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [m for m in data if isinstance(m, dict) and m.get("id")]
        except ValueError:
            pass
    # Старая схема: одна карта в pay_card_* — показываем её как первый способ
    number = runtime.get("pay_card_number")
    if number:
        return [{"id": "card", "bank": runtime.get("pay_card_bank") or "Корт", "number": number,
                 "holder": runtime.get("pay_card_holder"), "enabled": True}]
    return []


def enabled() -> list[dict]:
    return [m for m in all_methods() if m.get("enabled", True)]


def get(method_id: str) -> dict | None:
    return next((m for m in all_methods() if m["id"] == method_id), None)


async def save(conn: aiosqlite.Connection, methods: list[dict]) -> None:
    await runtime.set_value(conn, KEY, json.dumps(methods, ensure_ascii=False))
    first = next((m for m in methods if m.get("enabled", True)), None)
    await runtime.set_value(conn, "pay_card_number", first["number"] if first else "")
    await runtime.set_value(conn, "pay_card_holder", (first or {}).get("holder", ""))
    await runtime.set_value(conn, "pay_card_bank", (first or {}).get("bank", ""))


async def add(conn: aiosqlite.Connection, bank: str, number: str, holder: str) -> dict:
    method = {"id": secrets.token_hex(3), "bank": bank, "number": number, "holder": holder, "enabled": True}
    await save(conn, [*all_methods(), method])
    return method


async def update(conn: aiosqlite.Connection, method_id: str, **fields) -> None:
    methods = all_methods()
    for m in methods:
        if m["id"] == method_id:
            m.update(fields)
    await save(conn, methods)


async def delete(conn: aiosqlite.Connection, method_id: str) -> None:
    await save(conn, [m for m in all_methods() if m["id"] != method_id])


# ── Проверка ввода ───────────────────────────────────────────


def clean_bank(text: str) -> str | None:
    text = " ".join((text or "").split())
    return text[:40] if 2 <= len(text) <= 40 else None


def clean_number(text: str) -> str | None:
    """Карта (16–19 цифр) — группами по 4; телефон — как ввели; кошелёк — латиница/цифры."""
    raw = (text or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 9 <= len(digits) <= 15:
        return "+" + digits
    if 12 <= len(digits) <= 19 and re.fullmatch(r"[\d\s-]+", raw):
        return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
    if 8 <= len(digits) <= 11 and re.fullmatch(r"[\d\s-]+", raw):
        return digits
    if re.fullmatch(r"[A-Za-z0-9]{20,64}", raw):  # адрес криптокошелька
        return raw
    return None


def clean_holder(text: str) -> str | None:
    text = " ".join((text or "").split())
    return text[:60] if 2 <= len(text) <= 60 and not re.search(r"\d{4,}", text) else None


def masked(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    return f"•••• {digits[-4:]}" if len(digits) >= 8 else number
