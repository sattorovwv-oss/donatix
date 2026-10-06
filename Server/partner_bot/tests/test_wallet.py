"""Присмотр за кошельком: счётчики расхода и автостоп продажи."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, keyboards, runtime
from app.handlers import panel
from app.services import delivery
from app.services.fragment import DeliveryError, DeliveryProvider, DeliveryResult

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


class TogglePress:
    """Нажатие переключателя в панели: коду нужны только data и ответы."""

    class Screen:
        async def edit_text(self, text, **kw):
            return self

    def __init__(self, data: str):
        self.data = data
        self.message = self.Screen()

    async def answer(self, text: str = "", **kw) -> None:
        pass


class OkProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        return DeliveryResult(order_id="ok", raw={})


class BrokenProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        raise DeliveryError("Недостаточно средств на кошельке")


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


async def buy(bot, conn, provider, uid=7000, price=1000, qty=100):
    return await delivery.purchase(
        bot, conn, provider, user_id=uid, product_type="stars",
        quantity=qty, recipient="target", price=price,
    )


async def run(conn) -> None:
    bot = FakeBot()
    await db.upsert_user(conn, 7000, "buyer", "Покупатель")
    await db.credit(conn, 7000, 1_000_000)

    # ----------------------------------------------- расход считается
    check("сначала расход нулевой", runtime.get_int("stars_since_topup") == 0)
    await buy(bot, conn, OkProvider(), qty=100)
    await buy(bot, conn, OkProvider(), qty=250)
    check("выданные звёзды суммируются",
          runtime.get_int("stars_since_topup") == 350,
          str(runtime.get_int("stars_since_topup")))

    # -------------------------------------------- неудачи копятся
    await runtime.set_value(conn, "autostop_after", "3")
    broken = BrokenProvider()

    await buy(bot, conn, broken)
    check("первая неудача не гасит продажу",
          runtime.get_int("fail_streak") == 1 and runtime.get_bool("stars_enabled"))

    await buy(bot, conn, broken)
    check("вторая тоже не гасит",
          runtime.get_int("fail_streak") == 2 and runtime.get_bool("stars_enabled"))

    before = len(bot.messages)
    await buy(bot, conn, broken)
    check("на третьей продажа гаснет",
          not runtime.get_bool("stars_enabled") and not runtime.get_bool("premium_enabled"),
          f"streak={runtime.get_int('fail_streak')}")
    check("бот пометил, что выключил сам", runtime.get_bool("autostopped"))

    alerts = [text for _, text in bot.messages[before:] if "выключена автоматически" in text]
    check("админу ушла тревога", len(alerts) >= 1)
    check("в тревоге есть причина",
          alerts and "Недостаточно средств" in alerts[0], alerts[0][:80] if alerts else "—")

    # ------------------------------- повторная тревога не спамит
    before = len(bot.messages)
    await buy(bot, conn, broken)
    repeats = [t for _, t in bot.messages[before:] if "выключена автоматически" in t]
    check("тревога не повторяется на каждый заказ", not repeats, str(len(repeats)))

    # ------------------------------ выключенные разделы ушли из меню
    labels = [b.text for row in keyboards.main_menu().inline_keyboard for b in row]
    check("клиент не видит выключенные разделы",
          not any("звезды" in t or "Premium" in t for t in labels), str(labels))

    # ------------------------------------ деньги клиенту вернулись
    user = await db.get_user(conn, 7000)
    refunded = await db.list_orders(conn, user_id=7000, status=db.ORDER_REFUNDED)
    check("за все упавшие заказы деньги вернулись", len(refunded) == 4, str(len(refunded)))
    check("баланс клиента не пострадал",
          user.balance == 1_000_000 - 2000, str(user.balance))

    # --------------------------------------- отметка о пополнении
    await runtime.mark_topup(conn, "26.08.2026 13:00 UTC")
    check("после пополнения продажа включилась",
          runtime.get_bool("stars_enabled") and runtime.get_bool("premium_enabled"))
    check("счётчик неудач обнулён", runtime.get_int("fail_streak") == 0)
    check("расход обнулён", runtime.get_int("stars_since_topup") == 0)
    check("флаг автостопа снят", not runtime.get_bool("autostopped"))

    # ------------------------------------ успех обнуляет серию неудач
    await buy(bot, conn, broken)
    check("неудача снова считается", runtime.get_int("fail_streak") == 1)
    await buy(bot, conn, OkProvider())
    check("успешная выдача обнуляет серию", runtime.get_int("fail_streak") == 0)

    # ------------------------------------------------- экран кошелька
    await runtime.set_value(conn, "star_cost_diram", "18")
    text = panel.wallet_text()
    check("экран кошелька собирается", "Кошелёк Fragment" in text)
    check("показывает дату пополнения", "26.08.2026" in text, text[:120])
    check("показывает себестоимость выданного", "18.00" in text or "себестоимость" in text)
    check("настройки кошелька есть в панели",
          "autostop_after" in str(panel.wallet_kb().inline_keyboard))

    # ---------------- включил продажу руками — тревога снова живая
    # У владельца на сервере флаг автостопа пережил включение продажи
    # руками. Из-за этого следующая поломка выключила бы продажу молча:
    # защита решила бы, что уже предупреждала.
    from app.handlers import panel as pnl

    await runtime.set_value(conn, "stars_enabled", "0")
    await runtime.set_value(conn, "autostopped", "1")
    await pnl.cb_toggle(TogglePress("pn:toggle:stars_enabled"), conn)
    check("панель включила звёзды", runtime.get_bool("stars_enabled"))
    check("и сняла флаг автостопа", not runtime.get_bool("autostopped"))

    # А теперь главное: тревога о новой поломке должна дойти.
    await runtime.set_value(conn, "autostopped", "1")   # флаг «завис»
    await runtime.set_value(conn, "premium_enabled", "1")
    await runtime.set_value(conn, "fail_streak", "0")
    before = len(bot.messages)
    for _ in range(3):
        await buy(bot, conn, broken)
    fresh = [t for _, t in bot.messages[before:] if "выключена автоматически" in t]
    check("при живой продаже тревога приходит, даже если флаг завис",
          len(fresh) >= 1, f"сообщений: {len(fresh)}")


asyncio.run(main())
