"""Шаги диалогов переживают перезапуск бота."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.fsm.storage.base import StorageKey  # noqa: E402

from app.fsm_storage import SqliteStorage  # noqa: E402
from app.handlers.paymethods import PayWizard  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


async def main() -> None:
    path = Path(tempfile.mkdtemp()) / "fsm.sqlite3"
    key = StorageKey(bot_id=1, chat_id=111, user_id=111)
    s = SqliteStorage(path)
    await s.set_state(key, PayWizard.holder)
    await s.set_data(key, {"bank": "Алиф", "number": "777030211"})
    check("шаг в работе", await s.get_state(key) == PayWizard.holder.state)
    await s.close()

    s2 = SqliteStorage(path)  # «перезапуск бота»
    check("шаг пережил перезапуск", await s2.get_state(key) == PayWizard.holder.state)
    check("данные шага тоже", (await s2.get_data(key)) == {"bank": "Алиф", "number": "777030211"})
    await s2.set_state(key, None)
    await s2.set_data(key, {})
    await s2.close()
    s3 = SqliteStorage(path)
    check("очищенный шаг не возвращается", await s3.get_state(key) is None and await s3.get_data(key) == {})
    await s3.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
