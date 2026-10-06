"""Темп к поставщику: сто человек разом не должны пробить лимит."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app.services.ratelimit import WINDOW, Busy, Rate

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Clock:
    """Поддельные часы: проверка не должна идти настоящую минуту."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def nap(self, seconds: float) -> None:
        # Спим «мгновенно», но время двигаем — как в жизни.
        self.now += seconds
        await asyncio.sleep(0)


async def hundred_at_once() -> None:
    """Сто заказов в одну секунду при лимите 60."""
    clock = Clock()
    rate = Rate(60, clock=clock, nap=clock.nap)
    sent: list[float] = []

    async def buy(_):
        await rate.take(urgent=True)
        sent.append(clock.now)

    await asyncio.gather(*(buy(i) for i in range(100)))

    check("все сто запросов ушли, никого не потеряли", len(sent) == 100, str(len(sent)))

    # В любом окне длиной в минуту — не больше лимита.
    worst = max(sum(1 for t in sent if start <= t < start + WINDOW)
                for start in sent)
    check("лимит не пробит ни в одном окне", worst <= 60, f"худшее окно: {worst}")

    first_minute = sum(1 for t in sent if t < sent[0] + WINDOW)
    check("первая минута забита под завязку", first_minute == 60, str(first_minute))
    check("остальные ушли следующей минутой", len(sent) - first_minute == 40,
          str(len(sent) - first_minute))


async def orders_go_first() -> None:
    """Заказ клиента не ждёт, пока пройдут фоновые опросы статуса.

    Фоновых берём больше, чем освободится мест: иначе все пройдут одной
    пачкой в один и тот же миг, и очерёдность проверка не покажет.
    """
    clock = Clock()
    rate = Rate(20, clock=clock, nap=clock.nap)
    order: list[float] = []
    polls: list[float] = []

    for _ in range(20):          # окно забито под завязку
        await rate.take()
    clock.now += 1               # чтобы последняя пачка уложилась в предел

    async def poll():
        await rate.take()
        polls.append(clock.now)

    async def buy():
        await rate.take(urgent=True)
        order.append(clock.now)

    tasks = [asyncio.create_task(poll()) for _ in range(25)]
    await asyncio.sleep(0)       # фоновые уже стоят в очереди
    tasks.append(asyncio.create_task(buy()))
    await asyncio.gather(*tasks)

    check("заказ прошёл в первой же освободившейся пачке",
          order and order[0] == min(polls + order),
          f"заказ в {order[0] - 1000:.0f}, самый ранний опрос в {min(polls) - 1000:.0f}")
    check("часть фоновых при этом ушла в следующее окно",
          max(polls) > order[0],
          f"последний опрос в {max(polls) - 1000:.0f}")
    check("никого не потеряли", len(polls) == 25 and len(order) == 1,
          f"{len(polls)} опросов, {len(order)} заказов")


async def queue_has_a_bottom() -> None:
    """Если очередь не расходится, ждать вечно нельзя."""
    clock = Clock()
    rate = Rate(1, clock=clock, nap=clock.nap)
    await rate.take()

    # Занимаем место заново каждую минуту, чтобы очередь не рассасывалась.
    async def hog():
        for _ in range(10):
            clock.now += WINDOW
            await rate.take()
            await asyncio.sleep(0)

    asyncio.create_task(hog())
    refused = None
    try:
        await rate.take()
    except Busy as exc:
        refused = exc
    check("бесконечного ожидания нет", refused is not None, repr(refused))
    check("причина названа понятно",
          refused and "Очередь к поставщику" in str(refused), str(refused))


async def limit_follows_settings() -> None:
    """Лимит меняется из панели без перезапуска."""
    from app import db, runtime
    from app.services import ratelimit

    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)

    ratelimit.forget()
    check("по умолчанию берём из настроек сервера",
          ratelimit.for_key("kluch").limit == db.settings.supplier_rate_per_min,
          str(ratelimit.for_key("kluch").limit))

    await runtime.set_value(conn, "supplier_rate_per_min", "40")
    check("панель важнее и меняет на ходу",
          ratelimit.for_key("kluch").limit == 40,
          str(ratelimit.for_key("kluch").limit))

    check("у каждого ключа свой счёт",
          ratelimit.for_key("kluch") is not ratelimit.for_key("drugoi"))

    # Ноль или мусор не должны обнулять лимит: это остановило бы продажи.
    await runtime.set_value(conn, "supplier_rate_per_min", "0")
    check("нулём лимит не убить", ratelimit.for_key("kluch").limit >= 1,
          str(ratelimit.for_key("kluch").limit))
    await conn.close()


async def polling_scales_down() -> None:
    """Опрос статусов не должен съедать лимит при наплыве заказов.

    Опрос раз в три секунды — двадцать запросов в минуту на заказ. При
    лимите в шестьдесят три заказа разом забирали бы всё, и покупки
    вставали бы в очередь за проверками чужих заказов.
    """
    from app import db, runtime
    from app.services import games, ratelimit

    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await runtime.set_value(conn, "supplier_rate_per_min", "55")
    ratelimit.forget()

    was = games._watching
    try:
        loads = {}
        for count in (1, 3, 10, 50, 120):
            games._watching = count
            step = games.poll_every()
            loads[count] = count * 60 / step

        games._watching = 1
        check("один заказ опрашивается часто",
              games.poll_every() == games.FAST_EVERY, f"{games.poll_every()} с")

        budget = 55 * games.BACKGROUND_SHARE
        over = {n: round(v, 1) for n, v in loads.items() if v > budget + 3}
        check("фон не выходит за свою долю ни при каком наплыве",
              not over, f"доля {budget:.0f}/мин, превышения: {over}")

        games._watching = 120
        check("при сотне заказов шаг разрежается, но не бесконечно",
              games.FAST_EVERY < games.poll_every() <= games.FAST_SLOWEST,
              f"{games.poll_every():.0f} с")

        check("клиентам остаётся больше половины лимита",
              55 - budget > 55 / 2, f"{55 - budget:.0f} из 55")
    finally:
        games._watching = was
        await conn.close()


async def every_call_counted() -> None:
    """Ограничитель стоит на единственном общем месте.

    Появится второй путь к поставщику мимо него — лимит начнёт
    пробиваться молча, и узнаем мы об этом от поставщика.
    """
    body = io.open("app/services/fazer.py", encoding="utf-8").read()
    check("запросы проходят через ограничитель",
          "await for_key(self.api_key).take(urgent=urgent)" in body)
    check("отказ очереди не оставляет заказ в тумане",
          "raise DeliveryError(str(exc)) from exc" in body)

    # Все обращения к сети — внутри _request, кроме диагностики панели.
    sends = body.count("session.request(") + body.count("session.get(")
    check("сетевых вызовов ровно столько, сколько мы знаем",
          sends == 3, f"нашлось {sends}: _request и два диагностических")


async def main() -> None:
    await hundred_at_once()
    await orders_go_first()
    await queue_has_a_bottom()
    await limit_follows_settings()
    await polling_scales_down()
    await every_call_counted()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
