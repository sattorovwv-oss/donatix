"""Транзакция не должна переживать обработчик: замок иначе висит часами."""
from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime
from app.middlewares.txn_guard import TxnGuard

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


async def duplicates_leave_no_lock(conn) -> None:
    """Пять мест, где упавшая вставка держала замок на запись."""
    await db.upsert_user(conn, 5, "a", "А")
    order = await db.create_order(conn, user_id=5, product_type="stars",
                                  quantity=1, recipient="x", price=100)

    await db.create_review(conn, order_id=order.id, user_id=5, rating=5)
    again = await db.create_review(conn, order_id=order.id, user_id=5, rating=1)
    check("повторный отзыв отклонён", again is None)
    check("и не оставил транзакцию", not conn.in_transaction)

    await db.create_link(conn, "reklama")
    check("повторная ссылка отклонена", await db.create_link(conn, "reklama") is None)
    check("и не оставила транзакцию", not conn.in_transaction)

    await db.create_promo(conn, "KOD", amount=100, max_uses=1)
    check("повторный промокод отклонён",
          await db.create_promo(conn, "KOD", amount=100, max_uses=1) is False)
    check("и не оставил транзакцию", not conn.in_transaction)

    key = await db.create_api_key(conn, 5, "тест") if hasattr(db, "create_api_key") else None
    key_id = key.id if key is not None and hasattr(key, "id") else 1
    await db.reserve_idem(conn, key_id, "idem-1")
    await db.reserve_idem(conn, key_id, "idem-1")
    check("повторный ключ идемпотентности не оставил транзакцию",
          not conn.in_transaction)

    await db.claim_bank_payment(conn, source="bank", message_id=1, amount=100)
    twice = await db.claim_bank_payment(conn, source="bank", message_id=1, amount=100)
    check("повторное уведомление банка отклонено", twice is None)
    check("и не оставило транзакцию", not conn.in_transaction)


async def guard_cleans_up(conn) -> None:
    """Обработчик, забывший commit, не должен унести замок с собой."""
    gate = TxnGuard()

    class Press:
        data = "m:test"

    async def forgetful(event, data):
        # Запись без commit — ровно та ошибка, которую ловим.
        await conn.execute("UPDATE users SET first_name = 'Б' WHERE id = 5")
        assert conn.in_transaction

    await gate(forgetful, Press(), {"conn": conn})
    check("после обработчика транзакция снята", not conn.in_transaction)

    async def falling(event, data):
        await conn.execute("UPDATE users SET first_name = 'В' WHERE id = 5")
        raise RuntimeError("упал после записи")

    escaped = None
    try:
        await gate(falling, Press(), {"conn": conn})
    except RuntimeError as exc:
        escaped = exc
    check("после падения транзакция тоже снята", not conn.in_transaction)
    check("ошибка при этом не проглочена", escaped is not None)

    # Второе соединение — как API или юзербот — должно писать сразу.
    other = await db.connect()
    await other.execute("PRAGMA busy_timeout=500")
    import time
    t = time.perf_counter()
    await other.execute("UPDATE users SET first_name = 'Г' WHERE id = 5")
    await other.commit()
    check("другое соединение не ждёт замка",
          time.perf_counter() - t < 0.4, f"{time.perf_counter() - t:.2f} с")
    await other.close()

    # Предохранитель обязан стоять самым внешним: иначе транзакцию,
    # оставленную более внешней прослойкой, он не увидит. У aiogram
    # самой внешней становится ПЕРВАЯ подключённая.
    body = io.open("app/main.py", encoding="utf-8").read()
    outer = re.findall(r"dp\.callback_query\.outer_middleware\((\w+)\(\)\)", body)
    check("предохранитель подключён первым, то есть самым внешним",
          outer and outer[0] == "TxnGuard", str(outer))

    for path in ("app/api/server.py", "app/userbot/runner.py"):
        text = io.open(path, encoding="utf-8").read()
        check(f"{path.split('/')[-1]} снимает зависшую транзакцию",
              "db.release(conn)" in text)


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await duplicates_leave_no_lock(conn)
        await guard_cleans_up(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
