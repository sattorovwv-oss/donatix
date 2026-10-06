"""Точка входа: long polling, без домена и HTTPS."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from . import admin as admin_module
from . import brain as brain_module
from . import db, handlers, reminders, reports, stt
from .access import AccessMiddleware, bootstrap_admins
from .config import Config, load_config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run(config: Config) -> None:
    log = logging.getLogger("bot")
    config.ensure_dirs()

    # Шрифты проверяем на старте, а не когда пользователь попросит отчёт.
    reports.register_fonts(config.font_path, config.font_bold_path)

    conn = db.connect(config.db_path)
    # Владельцы из .env заводятся в базе как админы: при переходе со старой
    # однопользовательской версии владелец не должен потерять свои записи.
    bootstrap_admins(conn, config.allowed_user_ids)

    transcriber = stt.Transcriber(
        stt.make_client(config.openai_api_key), model=config.whisper_model
    )
    brain = brain_module.Brain(brain_module.make_client(config), conn, config)

    bot = Bot(config.telegram_token, default=DefaultBotProperties(parse_mode=None))
    dispatcher = Dispatcher()

    # Доступ проверяется один раз для всех обработчиков сразу — обойти нельзя.
    access = AccessMiddleware(conn)
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)

    dispatcher.include_router(admin_module.router)   # панель — раньше общих обработчиков
    dispatcher.include_router(handlers.router)

    # Зависимости прокидываются в хендлеры как аргументы.
    dispatcher["config"] = config
    dispatcher["conn"] = conn
    dispatcher["brain"] = brain
    dispatcher["stt"] = transcriber

    me = await bot.get_me()
    dispatcher["bot_username"] = me.username
    log.info("Запущен как @%s. База: %s", me.username, config.db_path)
    log.info("Админы: %s. Всего пользователей: %s",
             sorted(config.allowed_user_ids), len(db.list_users(conn)))

    # Планировщик напоминаний живёт рядом с опросом Телеграма.
    scheduler = asyncio.create_task(reminders.run_scheduler(bot, conn, config.tz))

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        await bot.session.close()
        conn.close()


def main() -> int:
    try:
        config = load_config()
    except (RuntimeError, ValueError) as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        print("\nОстановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
