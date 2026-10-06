"""Контакт поддержки — свой у каждого бота, задаётся в /panel."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime, texts  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Msg:
    def __init__(self, text):
        self.text = text
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class State:
    def __init__(self):
        self.data = {"field": "support_username"}

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data = {}


async def main() -> None:
    from app.handlers.panel import on_field_value
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await runtime.set_value(conn, "support_username", "")
    check("без своего — из настроек запуска", texts.support() == "@support")

    m = Msg("https://t.me/Partner_Shop")
    await on_field_value(m, State(), conn)
    check("ссылка t.me принимается", runtime.get("support_username") == "Partner_Shop", str(m.sent))
    check("покупатели видят контакт партнёра", texts.support() == "@Partner_Shop")

    m = Msg("@ab")
    await on_field_value(m, State(), conn)
    check("короткий юзернейм не принимаем", "❌" in m.sent[0] and runtime.get("support_username") == "Partner_Shop")
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
