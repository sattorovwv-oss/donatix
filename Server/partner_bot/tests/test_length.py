"""Длинные экраны не падают: текст укорачивается, теги закрыты; экран игр компактный."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime  # noqa: E402
from app.middlewares.length_guard import TAG_RE, _visible_len, shorten  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def balanced(html: str) -> bool:
    stack = []
    for closing, name, selfclose in TAG_RE.findall(html):
        if selfclose:
            continue
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


async def main() -> None:
    long = "<b>Игры</b>\n<blockquote>" + "\n".join(f"✅ <b>Игра {i}</b> · <code>code_{i}</code> &amp; ещё"
                                                  for i in range(400)) + "</blockquote>"
    short = shorten(long, 4096)
    check("укорочено до лимита (как считает Telegram)", _visible_len(short) < 4096, str(_visible_len(short)))
    check("теги закрыты", balanced(short))
    check("видно, что обрезано", "…" in short)
    check("короткий текст не трогаем", shorten("<b>ok</b>", 4096) == "<b>ok</b>")
    one_line = "<b>" + "я" * 6000 + "</b>"
    s2 = shorten(one_line, 4096)
    check("одна огромная строка тоже", len(s2) < 4096 and balanced(s2), str(len(s2)))

    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    for i in range(200):
        await db.add_game(conn, category_id=f"game_{i}", title=f"Очень длинное название игры номер {i}",
                          field="user_id", region="")
        await db.update_game(conn, f"game_{i}", enabled=1 if i % 5 == 0 else 0)
    from app.handlers.panel import games_kb, games_text
    text = await games_text(conn)
    kb = await games_kb(conn)
    buttons = sum(len(r) for r in kb.inline_keyboard)
    check("экран игр с 200 играми влезает в сообщение", len(text) < 3500, str(len(text)))
    check("и кнопок не больше 60", buttons <= 60, str(buttons))
    check("скрытые — отдельной кнопкой", any("Скрытые игры · 160" in b.text for r in kb.inline_keyboard for b in r))
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
