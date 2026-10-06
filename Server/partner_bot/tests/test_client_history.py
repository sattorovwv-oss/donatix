"""Панель: найти клиента по имени/нику/ID и увидеть всю его историю."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await db.upsert_user(conn, 5551, "farrukh_ff", "Фаррух")
    await db.upsert_user(conn, 5552, "farid", "Фарид")
    await db.upsert_user(conn, 7777, None, "Алиджон")

    check("по ID", [u.id for u in await db.search_users(conn, "5551")][:1] == [5551])
    check("по @нику", [u.id for u in await db.search_users(conn, "@farrukh_ff")][:1] == [5551])
    check("по имени", [u.id for u in await db.search_users(conn, "алиджон")] == [7777])
    check("по части — несколько", {u.id for u in await db.search_users(conn, "far")} == {5551, 5552})

    await db.create_deposit(conn, user_id=5551, amount=15000, method="Алиф", receipt_file_id="x")
    await db.create_order(conn, user_id=5551, product_type="game:free_fire_id", quantity=1,
                          recipient="123456789", price=1200)
    await db.add_adjustment(conn, user_id=5551, admin_id=1, amount=-500, reason="штраф")
    rows, total = await db.user_timeline(conn, 5551)
    kinds = [r["kind"] for r in rows]
    check("вся история одной лентой", total == 3 and set(kinds) == {"deposit", "order", "adjust"}, str(kinds))

    from app.handlers.panel import _hist_line
    lines = [_hist_line(r) for r in rows]
    joined = "\n".join(lines)
    check("видно пополнение, покупку и списание",
          "+150.00" in joined and "−12.00" in joined and "123456789" in joined and "штраф" in joined, joined)
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
