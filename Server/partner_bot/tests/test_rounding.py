"""Округление цен: сами суммы, доступное количество и экран выбора."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import PrivateAttr

from app import db, keyboards, runtime
from app.handlers import panel
from app.money import affordable_stars, exact_stars_cost, fmt, round_price, stars_cost

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class SpyMessage(Message):
    _log: list = PrivateAttr(default_factory=list)

    async def edit_text(self, text, reply_markup=None, **kw):
        self._log.append((text, reply_markup))
        return self

    async def answer(self, text, reply_markup=None, **kw):
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


def call_of(data: str) -> SpyCallback:
    user = User(id=111, is_bot=False, first_name="Админ")
    message = SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=111, type="private"), from_user=user,
    )
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=message,
    )


def texts_of(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def run(conn) -> None:
    # цена как на боевом боте
    await runtime.set_value(conn, "star_price_e4", "1604")
    packs = [50, 100, 250, 500, 1000, 2500]

    # ------------------------------------------------------ сами суммы
    await runtime.set_value(conn, "round_prices", "off")
    check("без округления цена точная",
          [stars_cost(q) for q in packs] == [802, 1604, 4010, 8020, 16040, 40100],
          str([stars_cost(q) for q in packs]))

    await runtime.set_value(conn, "round_prices", "up1")
    got = [stars_cost(q) for q in packs]
    check("вверх до сомони даёт ровные суммы",
          got == [900, 1700, 4100, 8100, 16100, 40100], str(got))
    check("ровная сумма не растёт лишний раз", stars_cost(2500) == 40100)
    check("округление всегда не в убыток",
          all(stars_cost(q) >= exact_stars_cost(q) for q in packs))

    await runtime.set_value(conn, "round_prices", "near1")
    got = [stars_cost(q) for q in packs]
    check("к ближайшему сомони", got == [800, 1600, 4000, 8000, 16000, 40100], str(got))

    await runtime.set_value(conn, "round_prices", "up5")
    got = [stars_cost(q) for q in packs]
    check("вверх до пяти сомони", got == [1000, 2000, 4500, 8500, 16500, 40500], str(got))

    check("незнакомый режим не ломает счёт",
          round_price(802, "чепуха") == 802)
    check("ноль остаётся нулём", round_price(0, "up5") == 0)

    # цена не ползёт вверх при повторных пересчётах
    await runtime.set_value(conn, "round_prices", "up1")
    first = stars_cost(100)
    check("повторный пересчёт цену не двигает",
          stars_cost(100) == first == 1700, str((first, stars_cost(100))))

    # ------------------------------------------- сколько звёзд по карману
    await runtime.set_value(conn, "round_prices", "up5")
    for balance in (500, 802, 1000, 5000, 12345, 100000):
        quantity = affordable_stars(balance)
        check(f"на {fmt(balance)} обещанное количество правда влезает",
              quantity == 0 or stars_cost(quantity) <= balance,
              f"{quantity} звёзд = {fmt(stars_cost(quantity))}")
    check("на пустой баланс ничего не обещаем", affordable_stars(0) == 0)

    await runtime.set_value(conn, "round_prices", "up1")

    # ------------------------------------------------------- кнопки магазина
    labels = [b.text for row in keyboards.stars_entry().inline_keyboard for b in row]
    check("на кнопке ровная сумма", "⭐️ 100 — 17.00 с." in labels, str(labels[:3]))
    check("копеек на кнопках не осталось",
          not any(".02 " in t or ".04 " in t for t in labels), str(labels))

    # --------------------------------------------------------- экран панели
    call = call_of("pn:round")
    await panel.cb_round(call)
    check("экран открывается", "Округление цен" in call.last)
    for title in ("без округления", "вверх до сомони", "до сомони", "вверх до 5 сомони"):
        check(f"есть способ «{title}»", title in call.last)
    check("пример показан на настоящей цене", "16.04 с." in call.last, call.last)
    check("текущий способ отмечен", "🔘 <b>вверх до сомони</b>" in call.last)
    check("выбранный способ отмечен и на кнопке",
          "✅ Вверх до сомони" in texts_of(call.markup), str(texts_of(call.markup)))

    call = call_of("pn:round:near1")
    await panel.cb_round_set(call, conn)
    check("способ переключается", runtime.get("round_prices") == "near1")
    check("экран перерисован", "🔘 <b>до сомони</b>" in call.last)
    check("цены пересчитались сразу", stars_cost(100) == 1600, str(stars_cost(100)))

    call = call_of("pn:round:чепуха")
    await panel.cb_round_set(call, conn)
    check("чужой способ отклонён", runtime.get("round_prices") == "near1")
    check("и об этом сказано", any("нет" in a for a in call.alerts), str(call.alerts))

    check("режим виден в разделе цен",
          any("Округление цен" in b.text
              for r in panel.prices_kb().inline_keyboard for b in r))

    await runtime.set_value(conn, "round_prices", "up1")
    check("по умолчанию округляем вверх до сомони",
          runtime.DEFAULTS["round_prices"] == "up1")


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
