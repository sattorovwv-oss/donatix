"""Запуск юзербота: python -m app.userbot

Отдельным процессом от основного бота нарочно. Юзербот входит в Telegram
как человек, и если его уронит обрыв связи или отзыв сессии, продажи в
боте продолжатся — просто оплату придётся подтверждать руками.
"""
from __future__ import annotations

import asyncio
import contextlib

from app.userbot.runner import run

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
