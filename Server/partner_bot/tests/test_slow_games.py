"""Standoff 2 и Clash of Clans выдаются долго: клиенту — срок. По таймауту денег не возвращаем никогда —
ждём ответа поставщика (отменил — возврат, выполнил — выдано)."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from app import db, runtime, texts  # noqa: E402
from app.services import games as svc  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Provider:
    async def order_status(self, order_id):
        return {"order_id": order_id, "status": "processing"}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


async def main() -> None:
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)

    so = svc.wait_text("Standoff 2")
    check("Standoff 2: до 90 минут и часы выдачи", "90 дақиқа" in so and "11:00 то 23:00" in so, so)
    coc = svc.wait_text("⚔️ Clash of Clans")
    check("Clash of Clans: 20–90 минут", "20–90 дақиқа" in coc and "субҳ" in coc, coc)
    check("остальные игры — как раньше", "камтар аз" in svc.wait_text("🔥 Free Fire"))
    msg = texts.GAME_ACCEPTED.format(order_id=7, pack="100 Gold", player="178563910", price="25.95", wait=so)
    check("срок попадает в сообщение «заказ принят»", "90 дақиқа" in msg and "№7" in msg)

    await db.add_game(conn, category_id="vd_1", title="Standoff 2")
    await db.add_game(conn, category_id="free_fire_br", title="🔥 Free Fire")
    bot, provider = FakeBot(), Provider()
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
    orders = {}
    for code in ("vd_1", "free_fire_br"):
        o = await db.create_order(conn, user_id=1, product_type=f"game:{code}", quantity=1,
                                  recipient="178563910", price=2595, cost=2000)
        await db.update_order(conn, o.id, fragment_order_id=f"dx-{o.id}")
        await conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (old, o.id))
        await conn.commit()
        orders[code] = await db.get_order(conn, o.id)
    slow = await svc.check(bot, conn, provider, orders["vd_1"])
    check("Standoff 2 через 45 минут — ждём, деньги не возвращаем", slow == "waiting", slow)
    fast = await svc.check(bot, conn, provider, orders["free_fire_br"])
    check("быстрая игра через 45 минут — тоже ждём, без возврата", fast == "waiting", fast)
    row = await db.get_order(conn, orders["free_fire_br"].id)
    check("деньги не вернули, заказ в обработке", row.status == orders["free_fire_br"].status, row.status)
    check("владельцу одно предупреждение «долго в обработке»",
          bool(bot.sent) and all("долго в обработке" in t and "НЕ возвращали" in t for t in bot.sent), bot.sent)
    told = len(bot.sent)
    again = await svc.check(bot, conn, provider, row)
    check("повторно не предупреждаем", again == "waiting" and len(bot.sent) == told, len(bot.sent))

    class Cancelled:
        async def order_status(self, order_id):
            return {"order_id": order_id, "status": "cancelled"}
    gone = await svc.check(bot, conn, Cancelled(), row)
    after = await db.get_order(conn, row.id)
    check("поставщик отменил — сразу отмена и возврат", gone == "failed" and after.status != row.status,
          f"{gone} {after.status}")

    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
