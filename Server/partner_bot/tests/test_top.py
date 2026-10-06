"""Топ клиентов: считаем по сумме покупок, а не по пополнениям."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime, texts
from app.handlers import menu, panel

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


async def buy(conn, user_id: int, price: int, status: str = db.ORDER_DELIVERED) -> None:
    order = await db.create_order(
        conn, user_id=user_id, product_type="stars", quantity=50,
        recipient="kto_to", price=price,
    )
    if status != db.ORDER_DELIVERING:
        await db.update_order(conn, order.id, status=status)


def names(rows) -> list[str]:
    return [client.username for client, _ in rows]


def amounts(rows) -> list[int]:
    return [amount for _, amount in rows]


async def run(conn) -> None:
    # Щедрый на пополнения, но почти не покупает.
    await db.upsert_user(conn, 1, "kopilka", "Копилка")
    await db.credit(conn, 1, 100_00, as_deposit=True)
    await buy(conn, 1, 10_00)

    # Пополняет меньше, но тратит всё.
    await db.upsert_user(conn, 2, "pokupatel", "Покупатель")
    await db.credit(conn, 2, 60_00, as_deposit=True)
    await buy(conn, 2, 30_00)
    await buy(conn, 2, 25_00)

    # Заказы были, но все отменены — в топ покупателей не попадает.
    await db.upsert_user(conn, 3, "nevezuchiy", "Невезучий")
    await db.credit(conn, 3, 80_00, as_deposit=True)
    await buy(conn, 3, 70_00, db.ORDER_REFUNDED)
    await buy(conn, 3, 5_00, db.ORDER_DELIVERING)

    # Ни одного заказа.
    await db.upsert_user(conn, 4, "molchun", "Молчун")

    # ------------------------------------------------------ по покупкам
    top = await db.top_clients(conn, by="purchases")
    check("первым идёт тот, кто больше купил", names(top)[0] == "pokupatel", str(names(top)))
    check("суммы — это покупки, а не пополнения", amounts(top) == [5500, 1000], str(amounts(top)))
    check("возвращённый заказ в сумму не идёт", "nevezuchiy" not in names(top))
    check("незавершённый заказ в сумму не идёт", "nevezuchiy" not in names(top))
    check("клиент без заказов в топ не попадает", "molchun" not in names(top))

    # ---------------------------------------------------- по пополнениям
    top = await db.top_clients(conn, by="deposits")
    check("по пополнениям первым идёт другой", names(top)[0] == "kopilka", str(names(top)))
    check("суммы — это пополнения", amounts(top) == [10000, 8000, 6000], str(amounts(top)))
    check("клиент без пополнений не попадает", "molchun" not in names(top))

    check("лимит соблюдается", len(await db.top_clients(conn, limit=1)) == 1)

    # ------------------------------------------------------------ текст
    rows = await db.top_clients(conn)
    body = texts.TOP_CLIENTS.format(items=menu.top_lines(rows),
                                    basis=menu.top_basis("purchases"))
    check("в тексте написано, что рейтинг по покупкам", "аз рӯи маблағи харидҳо" in body, body[-90:])
    check("медали проставлены", "🥇" in body and "🥈" in body)
    check("сумма показана в сомони", "55.00" in body, body)

    body = texts.TOP_CLIENTS.format(items="", basis=menu.top_basis("deposits"))
    check("для пополнений подпись другая", "аз рӯи маблағи пуркуниҳо" in body)

    # ------------------------------------------------- переключатель в панели
    check("по умолчанию считаем по покупкам", runtime.get("top_by") == "purchases")
    check("в панели видно текущий режим",
          "по сумме покупок" in panel.toggles_text(), panel.toggles_text()[-60:])
    check("кнопка предлагает второй режим",
          any("Топ по пополнениям" in b.text
              for r in panel.toggles_kb().inline_keyboard for b in r))

    await runtime.set_value(conn, "top_by", "deposits")
    check("после переключения в панели другой режим",
          "по сумме пополнений" in panel.toggles_text())
    check("кнопка предлагает вернуться к покупкам",
          any("Топ по покупкам" in b.text
              for r in panel.toggles_kb().inline_keyboard for b in r))
    await runtime.set_value(conn, "top_by", "purchases")


async def cached_top(conn) -> None:
    """Топ считается не на каждое нажатие.

    Это публичная кнопка, а запрос складывает все выданные заказы разом.
    Соединение с базой одно на весь бот, поэтому десяток нажатий в
    секунду остановил бы бота для всех — не только для нажавших.
    """
    db.forget_top()
    first = await db.top_clients(conn, limit=3)

    # Новая покупка в память не попадёт — и это верно: рейтинг за пять
    # минут не портится, а нагрузка от него исчезает.
    await db.upsert_user(conn, 90_001, "новый", "Новый")
    order = await db.create_order(
        conn, user_id=90_001, product_type="stars", quantity=1,
        recipient="@x", price=999_999, cost=1,
    )
    await db.transition_order(conn, order.id, expected=db.ORDER_DELIVERING,
                              new=db.ORDER_DELIVERED)

    again = await db.top_clients(conn, limit=3)
    check("второй раз топ берётся из памяти",
          [u.id for u, _ in again] == [u.id for u, _ in first],
          str([u.id for u, _ in again]))

    fresh = await db.top_clients(conn, limit=3, cached=False)
    check("а если попросить заново — виден новый покупатель",
          any(user.id == 90_001 for user, _ in fresh),
          str([u.id for u, _ in fresh]))

    db.forget_top()
    after = await db.top_clients(conn, limit=3)
    check("сброс памяти тоже показывает свежее",
          any(user.id == 90_001 for user, _ in after),
          str([u.id for u, _ in after]))


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
        await cached_top(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
