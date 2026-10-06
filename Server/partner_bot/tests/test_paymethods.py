"""Реквизиты бота из конструктора: мастер, проверка ввода, выбор банка покупателем."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

os.environ["DONATIX_URL"] = "http://donatix.test"

from app import db, runtime  # noqa: E402
from app.config import settings  # noqa: E402

settings.fazer_api_key = settings.fazer_api_key or "dx_live_test"
from app.handlers.deposit import _requisites  # noqa: E402
from app.keyboards import deposit_methods  # noqa: E402
from app.services import paymethods as pm  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


async def main() -> None:
    check("карта группами по 4", pm.clean_number("5058270012345678") == "5058 2700 1234 5678")
    check("телефон с +992", pm.clean_number("+992 900 12 34 56") == "+992900123456")
    check("мусор не принимаем", pm.clean_number("привет") is None)
    check("имя без цифр", pm.clean_holder("Алиджон М.") == "Алиджон М." and pm.clean_holder("1234 5678") is None)
    check("маска номера", pm.masked("5058 2700 1234 5678") == "•••• 5678")

    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await runtime.set_value(conn, pm.KEY, "")
    await runtime.set_value(conn, "pay_card_number", "")
    check("пусто — способов нет", pm.all_methods() == [])
    a = await pm.add(conn, "Душанбе Сити", "5058 2700 1234 5678", "Алиджон М.")
    b = await pm.add(conn, "Алиф", "+992900123456", "Алиджон М.")
    check("два способа", [m["bank"] for m in pm.enabled()] == ["Душанбе Сити", "Алиф"])
    check("первый — в старых настройках", runtime.get("pay_card_number") == "5058 2700 1234 5678")
    kb = [b_.text for row in deposit_methods().inline_keyboard for b_ in row]
    check("покупатель видит оба банка", "🏦 Душанбе Сити" in kb and "🏦 Алиф" in kb, str(kb))
    await pm.update(conn, a["id"], enabled=False)
    kb = [b_.text for row in deposit_methods().inline_keyboard for b_ in row]
    check("скрытый не показывается", "🏦 Душанбе Сити" not in kb and "🏦 Алиф" in kb)
    check("старые настройки — следующий способ", runtime.get("pay_card_number") == "+992900123456")
    body, _ = _requisites(1004, "R1", pm.get(b["id"]))
    check("реквизиты выбранного банка", "+992900123456" in body and "Алиф" in body)
    await pm.delete(conn, b["id"])
    check("удаление", pm.get(b["id"]) is None)

    trc = "TXyzABCDEFGHJKLMNPQRSTUVWXYZabcdef"
    check("адрес TRC20", pm.clean_wallet("USDT TRC20", trc) == trc)
    check("чужая сеть не проходит", pm.clean_wallet("USDT TRC20", "0x" + "a" * 40) is None)
    check("адрес BEP20", pm.clean_wallet("USDT BEP20", "0x" + "a" * 40) == "0x" + "a" * 40)
    check("банк — не крипта", pm.crypto("Алиф") is None)
    c = await pm.add(conn, "USDT TRC20", trc, "")
    kb = [b_.text for row in deposit_methods().inline_keyboard for b_ in row]
    check("кнопка USDT", "💎 USDT TRC20" in kb, str(kb))
    await runtime.set_value(conn, "usd_rate_diram", "1090")
    body, markup = _requisites(109000, "R2", pm.get(c["id"]))
    check("сеть и сумма в USDT", "Tron (TRC20)" in body and "100.00 USDT" in body and "Только в сети" in body, body)
    copy = [b_ for row in markup.inline_keyboard for b_ in row if b_.copy_text]
    check("копируется адрес", copy and copy[0].copy_text.text == trc)

    # Курс — тот же, что у Donatix; свой поиск курса выключается
    from app.handlers import donatix
    calls = []

    async def fake_call(method, path, **kw):
        calls.append(path)
        return {"ok": True, "tjs_rate": "10.75"}
    donatix._call, real_call = fake_call, donatix._call
    await runtime.set_value(conn, "usd_auto", "1")
    check("курс взят у Donatix", await donatix.sync_rate(conn, 0) and runtime.usd_rate() == 1075)
    check("свой поиск курса выключен", not runtime.get_bool("usd_auto"))
    check("не чаще, чем раз в 30 с", not await donatix.sync_rate(conn, donatix.RATE_PAYMENT) and len(calls) == 1)
    from app.handlers.donatix import _auto_kb, _auto_text
    trc = {"id": 7, "method_title": "USDT", "pay_amount": "20.0037", "pay_currency": "USDT", "amount_usd": "20.0000",
           "auto": "trc20", "address": "TXyzABCDEFGHJKLMNPQRSTUVWXYZabcdef", "pay_url": ""}
    text = _auto_text(trc)
    check("автоплатёж TRC20: точная сумма и адрес", "20.0037" in text and "TXyz" in text and "чек присылать не нужно" in text)
    bn = dict(trc, auto="binance", pay_url="https://pay.binance.com/x", address="")
    urls = [b.url for row in _auto_kb(bn).inline_keyboard for b in row if b.url]
    check("Binance Pay: кнопка оплаты", urls == ["https://pay.binance.com/x"])
    donatix._call = real_call
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
