"""Проверка денежной логики: списания, возвраты, гонки, сходимость кассы."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from app import db
from app.money import fmt
from app.services import delivery
from app.services.fragment import (
    DeliveryError, DeliveryProvider, DeliveryResult, DeliveryUncertain, Recipient,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    """Заглушка бота: молча глотает уведомления."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


class OkProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        return DeliveryResult(order_id="ok-1", raw={})

    async def deliver_premium(self, username, months):
        return DeliveryResult(order_id="ok-2", raw={})

    async def resolve_recipient(self, username):
        return Recipient(username=username, name=f"{username} Test", verified=True)


class RejectProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        raise DeliveryError("Insufficient Fragment balance")

    async def deliver_premium(self, username, months):
        raise DeliveryError("Insufficient Fragment balance")


class TimeoutProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        raise DeliveryUncertain("Нет ответа от Fragment: timeout")

    async def deliver_premium(self, username, months):
        raise DeliveryUncertain("Нет ответа от Fragment: timeout")


class BuggyProvider(DeliveryProvider):
    async def deliver_stars(self, username, amount):
        raise ValueError("что-то сломалось в коде")


async def main() -> None:
    db_file = db.settings.db_file
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)

    conn = await db.connect()
    await db.init(conn)
    bot = FakeBot()

    # ---------------------------------------------------------- регистрация
    await db.upsert_user(conn, 100, "inviter", "Пригласитель")
    is_new = await db.upsert_user(conn, 200, "buyer", "Покупатель", referrer_id=100)
    check("новый пользователь создаётся", is_new)
    inviter = await db.get_user(conn, 100)
    check("реферал засчитан пригласителю", inviter.ref_count == 1)

    await db.upsert_user(conn, 300, "self", "Сам себя", referrer_id=300)
    selfref = await db.get_user(conn, 300)
    check("нельзя пригласить самого себя", selfref.referrer_id is None)

    await db.upsert_user(conn, 400, "ghost", "Призрак", referrer_id=999999)
    ghost = await db.get_user(conn, 400)
    check("несуществующий пригласитель игнорируется", ghost.referrer_id is None)

    # ------------------------------------------------------------ пополнение
    dep = await db.create_deposit(
        conn, user_id=200, amount=10000, method="Карта", receipt_file_id="f1"
    )
    ok1 = await db.resolve_deposit(conn, dep.id, approved=True, admin_id=111)
    ok2 = await db.resolve_deposit(conn, dep.id, approved=True, admin_id=222)
    check("пополнение подтверждается один раз", ok1 and not ok2,
          "второй админ получил отказ")

    await db.credit(conn, 200, dep.amount, as_deposit=True)
    buyer = await db.get_user(conn, 200)
    check("баланс пополнен", buyer.balance == 10000, fmt(buyer.balance))
    check("общий депозит учтён", buyer.total_deposit == 10000)

    # ------------------------------------------------------- покупка успешна
    order = await delivery.purchase(
        bot, conn, OkProvider(), user_id=200, product_type="stars",
        quantity=100, recipient="target", price=2000,
    )
    buyer = await db.get_user(conn, 200)
    check("успешная покупка списывает деньги", buyer.balance == 8000, fmt(buyer.balance))
    check("заказ помечен выполненным", order.status == db.ORDER_DELIVERED, order.status)

    # ------------------------------------------------- нехватка средств
    try:
        await delivery.purchase(
            bot, conn, OkProvider(), user_id=200, product_type="stars",
            quantity=100000, recipient="target", price=999999,
        )
        raised = False
    except delivery.NotEnoughFunds:
        raised = True
    buyer = await db.get_user(conn, 200)
    check("покупка сверх баланса отклоняется", raised and buyer.balance == 8000,
          fmt(buyer.balance))

    # ------------------------------------------- отказ Fragment → возврат
    before = (await db.get_user(conn, 200)).balance
    order = await delivery.purchase(
        bot, conn, RejectProvider(), user_id=200, product_type="stars",
        quantity=100, recipient="target", price=2000,
    )
    after = (await db.get_user(conn, 200)).balance
    check("явный отказ Fragment возвращает деньги", after == before,
          f"{fmt(before)} -> {fmt(after)}")
    check("заказ помечен возвращённым", order.status == db.ORDER_REFUNDED, order.status)

    # ------------------------------ таймаут → деньги придержаны, ждём админа
    before = (await db.get_user(conn, 200)).balance
    order = await delivery.purchase(
        bot, conn, TimeoutProvider(), user_id=200, product_type="stars",
        quantity=100, recipient="target", price=2000,
    )
    after = (await db.get_user(conn, 200)).balance
    check("при таймауте деньги НЕ возвращаются автоматом",
          after == before - 2000 and order.status == db.ORDER_FAILED,
          f"{order.status}, {fmt(after)}")

    # админ решает: заказ всё-таки дошёл
    done = await delivery.manual_complete(bot, conn, order)
    fresh = await db.get_order(conn, order.id)
    check("/done закрывает зависший заказ без возврата",
          done and fresh.status == db.ORDER_DELIVERED
          and (await db.get_user(conn, 200)).balance == after)

    # второй зависший — админ возвращает
    order2 = await delivery.purchase(
        bot, conn, TimeoutProvider(), user_id=200, product_type="stars",
        quantity=100, recipient="target", price=2000,
    )
    held = (await db.get_user(conn, 200)).balance
    refunded = await delivery.manual_refund(bot, conn, order2)
    check("/refund возвращает деньги за зависший заказ",
          refunded and (await db.get_user(conn, 200)).balance == held + 2000)
    check("повторный /refund не начисляет дважды",
          not await delivery.manual_refund(bot, conn, order2),
          fmt((await db.get_user(conn, 200)).balance))

    # ------------------------------------ баг в коде тоже возвращает деньги
    before = (await db.get_user(conn, 200)).balance
    order = await delivery.purchase(
        bot, conn, BuggyProvider(), user_id=200, product_type="stars",
        quantity=100, recipient="target", price=2000,
    )
    check("непредвиденное исключение возвращает деньги",
          (await db.get_user(conn, 200)).balance == before
          and order.status == db.ORDER_REFUNDED, order.status)

    # ------------------------------------------- гонка: двойное списание
    await db.upsert_user(conn, 500, "racer", "Гонщик")
    await db.credit(conn, 500, 3000)
    results = await asyncio.gather(*[
        db.charge(conn, 500, 2000) for _ in range(2)
    ])
    racer = await db.get_user(conn, 500)
    check("два одновременных списания не уводят баланс в минус",
          sum(results) == 1 and racer.balance == 1000,
          f"успешных: {sum(results)}, баланс: {fmt(racer.balance)}")

    # ------------------------------------------------------------ промокоды
    await db.create_promo(conn, "SALE", 500, max_uses=1)
    first = await db.redeem_promo(conn, "sale", 500)
    again = await db.redeem_promo(conn, "SALE", 500)
    other = await db.redeem_promo(conn, "SALE", 100)
    check("промокод начисляет сумму", first == 500, str(first))
    check("повторная активация тем же юзером запрещена", again == "already_used")
    check("лимит активаций соблюдается", other == "exhausted")
    check("несуществующий промокод отклоняется",
          await db.redeem_promo(conn, "NOPE", 100) == "not_found")

    # ------------------------------------------------- сходимость кассы
    async with conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users") as cur:
        balances = (await cur.fetchone())["s"]
    async with conn.execute(
        "SELECT COALESCE(SUM(price),0) AS s FROM orders WHERE status = ?",
        (db.ORDER_DELIVERED,),
    ) as cur:
        spent = (await cur.fetchone())["s"]

    # Приход: пополнение 10000 + ручное начисление гонщику 3000 + промокод 500.
    incoming = 10000 + 3000 + 500
    # Расход: выполненные заказы + 2000, списанные в гоночной проверке напрямую,
    # без создания заказа (это делает сам тест, а не бот).
    raced_away = 2000
    outgoing = balances + spent + raced_away
    check("деньги сходятся: приход = остатки + потрачено",
          outgoing == incoming,
          f"остатки {fmt(balances)} + заказы {fmt(spent)} + гонка {fmt(raced_away)} "
          f"= {fmt(outgoing)}, приход {fmt(incoming)}")

    await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
