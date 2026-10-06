"""Отчёты за периоды, учёт себестоимости и сверка номеров заказов."""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from decimal import Decimal

from app import db, reports, runtime
from app.handlers import panel
from app.money import fmt
from app.services import pricing

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def utc(day: date, hour: int = 12) -> str:
    return datetime(day.year, day.month, day.day, hour,
                    tzinfo=timezone.utc).isoformat(timespec="seconds")


async def add_order(conn, day, *, price, cost, status=db.ORDER_DELIVERED,
                    user_id=1, qty=100, kind="stars", external=None):
    order = await db.create_order(conn, user_id=user_id, product_type=kind,
                                  quantity=qty, recipient="x", price=price, cost=cost)
    await conn.execute(
        "UPDATE orders SET status = ?, created_at = ?, fragment_order_id = ? WHERE id = ?",
        (status, utc(day), external, order.id),
    )
    await conn.commit()
    return order.id


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


async def run(conn) -> None:
    today = reports.local_today()
    yesterday = today - timedelta(days=1)
    long_ago = today - timedelta(days=20)

    await db.upsert_user(conn, 1, "a", "A")
    await db.upsert_user(conn, 2, "b", "B")

    # Сегодня: два выполненных и один возврат
    await add_order(conn, today, price=2500, cost=2000, external="FZ-109")
    await add_order(conn, today, price=5000, cost=4000, user_id=2)
    await add_order(conn, today, price=1000, cost=800, status=db.ORDER_REFUNDED)
    # Вчера и давно
    await add_order(conn, yesterday, price=3000, cost=2400)
    await add_order(conn, long_ago, price=9000, cost=7000)

    # -------------------------------------------------- отчёт за сегодня
    since, until = reports.bounds(today, today)
    data = await db.report(conn, since, until)

    check("за сегодня учтены только сегодняшние заказы",
          data["orders"] == 3 and data["done"] == 2, str(data["orders"]))
    check("выручка считается по выполненным",
          data["revenue"] == 7500, fmt(data["revenue"]))
    check("себестоимость суммируется", data["cost"] == 6000, fmt(data["cost"]))
    check("прибыль = выручка минус себестоимость",
          data["profit"] == 1500, fmt(data["profit"]))
    check("возвраты считаются отдельно",
          data["refunded"] == 1 and data["refunded_sum"] == 1000, str(data["refunded"]))
    check("возврат не попал в выручку", data["revenue"] == 7500)
    check("покупатели считаются по головам", data["buyers"] == 2, str(data["buyers"]))
    check("звёзды суммируются", data["stars"] == 200, str(data["stars"]))

    # -------------------------------------------------- вчера и период
    since, until = reports.bounds(yesterday, yesterday)
    y = await db.report(conn, since, until)
    check("вчерашний отчёт видит только вчера",
          y["done"] == 1 and y["revenue"] == 3000, str(y["revenue"]))

    since, until = reports.bounds(yesterday, today)
    both = await db.report(conn, since, until)
    check("период из двух дней складывает оба",
          both["done"] == 3 and both["revenue"] == 10500, fmt(both["revenue"]))

    since, until = reports.bounds(long_ago, today)
    month = await db.report(conn, since, until)
    check("месячный отчёт включает старые заказы",
          month["done"] == 4 and month["revenue"] == 19500, fmt(month["revenue"]))

    # ----------------------------------------------------- разбивка по дням
    days = await db.daily_series(conn, *reports.bounds(long_ago, today),
                                 reports.tz_hours())
    check("разбивка по дням отдаёт три дня с продажами",
          len(days) == 3, str(days))
    check("в дне видно выручку и прибыль",
          any(d[2] == 7500 and d[3] == 1500 for d in days), str(days))

    # ------------------------------- заказ без себестоимости не врёт о прибыли
    await add_order(conn, today, price=4000, cost=0)
    data = await db.report(conn, *reports.bounds(today, today))
    order = await db.get_order(conn, 6)
    check("заказ без себестоимости показывает нулевую прибыль",
          order.profit == 0, str(order.profit))

    text = panel.format_report("Тест", data, days)
    check("отчёт собирается", "Прибыль" in text or "не посчитать" in text)
    check("в отчёте видны возвраты", "Возвращено" in text)

    empty = await db.report(conn, *reports.bounds(date(2020, 1, 1), date(2020, 1, 2)))
    check("пустой период не ломает отчёт",
          empty["orders"] == 0 and empty["profit"] == 0)
    panel.format_report("Пусто", empty, [])
    check("пустой отчёт тоже собирается", True)

    # ---------------------------------------- разбивка отчёта по товарам
    db.GAME_TITLES["game:free_fire_cis"] = "🔥 Free Fire"
    db.GAME_TITLES["game:pubg_mobile"] = "🎯 PUBG Mobile"
    await add_order(conn, today, price=3200, cost=2600, qty=1,
                    kind="game:free_fire_cis")
    await add_order(conn, today, price=1400, cost=1022, qty=1,
                    kind="game:pubg_mobile")
    await add_order(conn, today, price=1700, cost=0, qty=1,
                    kind="game:free_fire_cis", status=db.ORDER_REFUNDED)
    await add_order(conn, today, price=5000, cost=4300, qty=1, kind="steam")

    by = await db.report_by_product(conn, *reports.bounds(today, today))
    got = {r["product_type"]: r for r in by}
    check("в разбивке видно каждое направление",
          set(got) == {"stars", "steam", "game:free_fire_cis", "game:pubg_mobile"},
          str(sorted(got)))
    check("выручка игры считается отдельно",
          got["game:free_fire_cis"]["revenue"] == 3200,
          fmt(got["game:free_fire_cis"]["revenue"]))
    check("прибыль по направлению считается",
          got["game:pubg_mobile"]["profit"] == 378,
          fmt(got["game:pubg_mobile"]["profit"]))
    check("возврат виден у своего направления",
          got["game:free_fire_cis"]["refunds"] == 1
          and got["game:free_fire_cis"]["refunded_sum"] == 1700,
          str(got["game:free_fire_cis"]["refunds"]))
    check("возврат не попал в выручку направления",
          got["game:free_fire_cis"]["revenue"] == 3200)
    check("игры помечены как игры",
          got["game:pubg_mobile"]["is_game"] and not got["steam"]["is_game"])
    check("звёзды и Steam не смешались",
          got["steam"]["revenue"] == 5000 and got["stars"]["revenue"] == 11500,
          f"{got['steam']['revenue']} / {got['stars']['revenue']}")
    check("направления идут от крупного к мелкому",
          [r["product_type"] for r in by][0] == "stars",
          str([r["product_type"] for r in by]))
    check("название направления подставлено",
          got["game:free_fire_cis"]["title"] == "🔥 Free Fire",
          got["game:free_fire_cis"]["title"])

    # пустое направление в отчёт не лезет: строка «Premium — 0» ничего
    # не говорит, а места занимает столько же, сколько настоящая продажа
    check("направления без заказов не показываются", "premium" not in got,
          str(sorted(got)))

    text = panel.format_report("Тест", data, days, by_product=by)
    check("в отчёте есть разбивка", "Что продано" in text, text[:60])
    check("в разбивке названа игра", "Free Fire" in text)
    check("игры сведены в одну строку", "🕹 Игры" in text, text[:400])
    check("возвраты видны в разбивке", "возвратов" in text)

    only_stars = [r for r in by if r["product_type"] == "stars"]
    check("одно направление не делает лишнего итога",
          "🕹 Игры" not in panel.product_block(only_stars))
    one_game = [r for r in by if r["product_type"] == "game:pubg_mobile"]
    check("одна игра сама себе итог",
          "🕹 Игры" not in panel.product_block(one_game)
          and "PUBG" in panel.product_block(one_game))
    check("без продаж разбивки нет", panel.product_block([]) == "")
    check("пустой отчёт не спотыкается о разбивку",
          "Что продано" not in panel.format_report("Пусто", empty, []))

    since, until = reports.bounds(date(2020, 1, 1), date(2020, 1, 2))
    check("за пустой период разбивка пустая",
          await db.report_by_product(conn, since, until) == [])

    # ------------------------------- отчёт по каждому поставщику отдельно
    # Звёзды, Premium и Steam идут с одного счёта, игры — с другого.
    # Владельцу надо знать, сколько отправить каждому: это себестоимость.
    main = panel.supplier_report("main", "Сегодня", by, same_key=False)
    check("в отчёте поставщика названа сумма к отправке",
          "Ему за период" in main, main[:80])
    head = main.split("📦")[0]
    check("к отправке идёт себестоимость, а не выручка",
          fmt(6000 + 4300) in head and fmt(11500 + 5000) not in head, head)
    check("выручка показана отдельной строкой",
          f"Продано клиентам на: <b>{fmt(11500 + 5000)}</b>" in main,
          main[:500])
    check("у первого поставщика звёзды и Steam",
          "Звёзды" in main and "Steam" in main)
    check("игры к первому поставщику не попали", "Free Fire" not in main)

    games = panel.supplier_report("games", "Сегодня", by, same_key=False)
    check("у второго поставщика только игры",
          "Free Fire" in games and "PUBG" in games and "Звёзды" not in games)
    check("сумма второму поставщику — себестоимость игр",
          fmt(2600 + 1022) in games, games[:400])
    check("возвраты показаны отдельно и не считаются в сумму",
          "Возвраты" in games and "денег не берёт" in games, games[-300:])

    empty_sup = panel.supplier_report("games", "Сегодня", [], same_key=False)
    check("без продаж отчёт поставщика не врёт",
          "продаж не было" in empty_sup, empty_sup[:200])
    check("без своего ключа предупреждаем про общий счёт",
          "с того же счёта" in panel.supplier_report("games", "Сегодня", by,
                                                     same_key=True))
    check("со своим ключом предупреждения нет",
          "с того же счёта" not in games)

    labels = [b.text for row in panel.report_kb("today").inline_keyboard
              for b in row]
    check("под отчётом есть кнопки обоих поставщиков",
          "⭐ Поставщик 1" in labels and "🕹 Поставщик 2" in labels, str(labels))

    kb = panel.supplier_kb("main", "week")
    data_all = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("период на экране поставщика не теряется",
          "pn:sup:main:today" in data_all and "pn:sup:main:week" in data_all,
          str(data_all))
    check("с экрана поставщика видно второго",
          "pn:sup:games:week" in data_all, str(data_all))
    check("возврат к отчёту сохраняет период",
          "pn:rep:week" in data_all, str(data_all))

    # ------------------------------------------- сверка номеров заказов
    found = await db.find_order_by_external(conn, "FZ-109")
    check("заказ находится по номеру платформы",
          found is not None and found.id == 1, str(found.id if found else None))
    check("несуществующий номер не находится",
          await db.find_order_by_external(conn, "FZ-999") is None)

    # ------------------------------------------------- разбор периодов
    check("границы включают весь последний день",
          reports.bounds(today, today)[1] > reports.bounds(today, today)[0])
    check("часовой пояс сдвигает границы",
          reports.bounds(date(2026, 8, 1), date(2026, 8, 1))[0].startswith("2026-07-31"),
          reports.bounds(date(2026, 8, 1), date(2026, 8, 1))[0])

    # ------------------------------------------------------- автоцены
    class FakeEstimate:
        def __init__(self, per_unit, total):
            self.usd_per_unit = Decimal(per_unit)
            self.usd_total = Decimal(total)
            self.quantity = 1000

    class Provider:
        async def cost_estimate(self, kind, amount):
            if kind == "stars":
                return FakeEstimate("0.0154", "15.40")
            return FakeEstimate("1", {3: "12.20", 6: "16.27", 12: "29.50"}[amount])

    await runtime.set_value(conn, "usd_rate_diram", "1090")
    await runtime.set_value(conn, "margin_percent", "0")
    result = await pricing.refresh_once(conn, Provider())
    check("без наценки автоцены не включаются", not result["ok"], result.get("reason"))

    await runtime.set_value(conn, "margin_percent", "15")
    result = await pricing.refresh_once(conn, Provider())
    check("автоцены отработали", result["ok"], str(result))
    # 0.0154 × 10.90 = 0.16786 сомони -> 1679 в десятитысячных; +15% = 1931
    check("себестоимость посчитана с четырьмя знаками",
          runtime.star_cost_e4() == 1679, str(runtime.star_cost_e4()))
    check("цена продажи держит наценку 15%",
          runtime.star_price_e4() == 1931, str(runtime.star_price_e4()))
    real = (runtime.star_price_e4() - runtime.star_cost_e4()) / runtime.star_cost_e4() * 100
    check("фактическая наценка совпадает с заданной",
          abs(real - 15) < 0.1, f"{real:.2f}%")
    check("себестоимость Premium сохранена",
          runtime.premium_costs().get(3) == 13298, str(runtime.premium_costs()))
    check("цена Premium пересчитана с наценкой",
          runtime.find_premium(3)["price"] == 15293,
          str(runtime.find_premium(3)))

    check("себестоимость заказа берётся из настроек",
          runtime.cost_of("stars", 100) == 1679, str(runtime.cost_of("stars", 100)))
    check("себестоимость Premium берётся по сроку",
          runtime.cost_of("premium", 3) == 13298)
    check("неизвестный срок Premium даёт ноль",
          runtime.cost_of("premium", 24) == 0)

    await runtime.set_value(conn, "usd_rate_diram", "0")
    result = await pricing.refresh_once(conn, Provider())
    check("без курса доллара автоцены отказываются работать",
          not result["ok"] and "курс" in result["reason"], result.get("reason"))

    class NoPrices:
        pass

    result = await pricing.refresh_once(conn, NoPrices())
    check("провайдер без цен не ломает автоцены", not result["ok"])


asyncio.run(main())
