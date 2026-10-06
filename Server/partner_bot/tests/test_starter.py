"""Бот из конструктора: сразу готовы только Free Fire (СНГ, Индонезия) и PUBG."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime  # noqa: E402
from app.services import autogames  # noqa: E402
from app.services import regions  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


CATALOG = [
    {"category_id": "free_fire_id", "name": "Free Fire (Indonesia)", "fields": ["user_id"]},
    {"category_id": "free_fire_cis", "name": "Free Fire (CIS)", "fields": ["user_id"]},
    {"category_id": "free_fire_br", "name": "Free Fire (Brazil)", "fields": ["user_id"]},
    {"category_id": "freefire_max_id", "name": "Free Fire MAX Indonesia", "fields": ["user_id"]},
    {"category_id": "pubg_mobile", "name": "PUBG Mobile", "fields": ["user_id"]},
    {"category_id": "pubg_new_state", "name": "PUBG New State", "fields": ["user_id"]},
    {"category_id": "mobile_legends", "name": "Mobile Legends", "fields": ["user_id", "zone_id"]},
    {"category_id": "genshin", "name": "Genshin Impact", "fields": ["uid"]},
]


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await runtime.set_value(conn, "usd_rate_diram", "1090")

    added, enabled = await autogames.import_starter(conn, CATALOG)
    games = {g.category_id: g for g in await db.list_games(conn)}
    check("добавлены только FF СНГ/Индонезия и PUBG", set(games) == {"free_fire_id", "free_fire_cis", "pubg_mobile"},
          str(sorted(games)))
    check("и сразу в меню", enabled == 3 and all(g.enabled for g in games.values()))
    ff = [g for g in games.values() if regions.family_of(g) == "free_fire"]
    check("Free Fire — одна кнопка с двумя регионами", len(ff) == 2, str([g.category_id for g in ff]))

    # Старый бот, которому раньше добавились все игры
    await autogames.import_all(conn, CATALOG)
    for g in await db.list_games(conn):
        await db.update_game(conn, g.category_id, enabled=1)
    hidden = await autogames.keep_only_starter(conn)
    on = sorted(g.category_id for g in await db.list_games(conn, only_enabled=True))
    check("у старого бота лишние игры скрыты", on == ["free_fire_cis", "free_fire_id", "pubg_mobile"], str(on))
    check("а не удалены", len(await db.list_games(conn)) == len(CATALOG) and hidden == 5, str(hidden))
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
