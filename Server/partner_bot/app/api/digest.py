"""Сводка за сутки для разработчика.

Отдельная задача, а не часть уведомлений о заказах: те приходят по
событию, а сводка — по времени, и у неё своя причина существовать.
Разработчик, выключивший поток сообщений, всё равно хочет знать раз в
день, сколько прошло заказов и сколько из них вернулось.

Пустую сводку не шлём: «за сутки 0 заказов» каждое утро — это способ
приучить человека не читать наши сообщения вовсе.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app import db
from app.api import catalog

log = logging.getLogger(__name__)

#: Когда шлём, по UTC. 4 утра здесь — девять утра в Душанбе.
HOUR = 4
#: Как часто проверяем, не пора ли. Получасовой шаг надёжнее часового:
#: если процесс перезапустили ровно в свой час, сводка не потеряется.
EVERY = 30 * 60


def _day_bounds(now: datetime) -> tuple[str, str]:
    """Прошедшие сутки: [позавчерашняя полночь, вчерашняя полночь)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight - timedelta(days=1)
    return (since.isoformat(timespec="seconds"),
            midnight.isoformat(timespec="seconds"))


def text_of(data: dict) -> str:
    lines = [
        "📊 <b>Сводка за сутки</b>",
        f"├ Заказов: <b>{data['orders']}</b>",
        f"├ Выполнено: <b>{data['done']}</b>",
    ]
    if data["working"]:
        lines.append(f"├ Ещё в работе: <b>{data['working']}</b>")
    if data["refunded"]:
        lines.append(f"├ Возвратов: <b>{data['refunded']}</b> "
                     f"на {catalog.money(data['returned'])['amount_text']}")
    lines.append(f"└ Потрачено: <b>{catalog.money(data['spent'])['amount_text']}"
                 f" {catalog.money(0)['currency']}</b>")
    return "\n".join(lines)


async def run_once(bot: Bot, conn, now: datetime | None = None) -> int:
    """Разослать сводки, кому сегодня ещё не слали. Возвращает сколько ушло."""
    from app.services.delivery import notify

    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    since, until = _day_bounds(now)

    sent = 0
    for user_id in await db.api_digest_targets(conn, today):
        data = await db.api_summary(conn, user_id, since, until)
        # Отметку ставим в любом случае: иначе пустой день заставлял бы
        # пересчитывать сводку каждые полчаса до самой ночи.
        await db.set_api_prefs(conn, user_id, digest_on=today)
        if not data["orders"]:
            continue
        try:
            await notify(bot, user_id, text_of(data))
            sent += 1
        except Exception as exc:  # noqa: BLE001 — один адресат не гасит рассылку
            log.info("Сводка API: %s не получил — %s", user_id, exc)
    return sent


async def loop(bot: Bot) -> None:
    while True:
        try:
            await asyncio.sleep(EVERY)
            if datetime.now(timezone.utc).hour != HOUR:
                continue
            conn = await db.connect()
            try:
                count = await run_once(bot, conn)
            finally:
                await conn.close()
            if count:
                log.info("Сводка API: разослана %s адресатам", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фон не должен умирать
            log.exception("Сводка API: непредвиденная ошибка: %s", exc)
            await asyncio.sleep(60)
