"""Пакеты: сначала алмазы/UC от дешёвых, потом ваучеры, потом прокачки."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app.services.games import sort_packs  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def packs(*pairs):
    return [{"name": n, "supplier_name": n, "price": p} for n, p in pairs]


ff = sort_packs(packs(("Level Up Pass", 500), ("Weekly Membership", 1800), ("520 Diamonds", 5000),
                      ("100 Diamonds", 1000), ("Monthly Membership", 9000), ("Booyah Pass", 7000),
                      ("310 Diamonds", 3000), ("Evo Access 7 Days", 2500)))
names = [o["name"] for o in ff]
check("Free Fire: алмазы от дешёвых", names[:3] == ["100 Diamonds", "310 Diamonds", "520 Diamonds"], str(names))
check("потом ваучеры", names[3:6] == ["Weekly Membership", "Booyah Pass", "Monthly Membership"], str(names))
check("потом прокачки", names[6:] == ["Level Up Pass", "Evo Access 7 Days"], str(names))

pubg = [o["name"] for o in sort_packs(packs(("Royale Pass", 9000), ("325 UC", 4500), ("60 UC", 900),
                                            ("Prime", 1000), ("660 UC", 9000)))]
check("PUBG: UC от дешёвых, потом пропуски", pubg == ["60 UC", "325 UC", "660 UC", "Prime", "Royale Pass"], str(pubg))

print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
