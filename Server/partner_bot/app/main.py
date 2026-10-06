"""Точка входа. Запуск: python -m app.main (из папки stars_bot)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeChat

from app import db, runtime, texts
from app.config import settings
from app.handlers import fallback
from app.handlers import (
    api_cab,
    games, reviews,
    admin, broadcast, deposit, donatix, menu, panel, paymethods, profile, shop, support,
)
from app.middlewares.emoji_guard import CustomEmojiGuard
from app.middlewares.escape import CommandEscapeMiddleware
from app.middlewares.same_screen import SameScreenGuard
from app.middlewares.sponsor import SponsorGate
from app.middlewares.stale_screen import StaleScreenGuard
from app.middlewares.txn_guard import TxnGuard
from app.middlewares.guard import UserGuardMiddleware
from app.services.billing import make_sender
from app.services.fragment import (
    DELIVERY_MODES, MYSTARS_MODES, build_provider, mode_now,
)
from app.services.games import watch_loop as games_watch
from app.services.pricing import auto_price_loop

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="api", description="API для разработчиков"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="panel", description="Админ-панель"),
    BotCommand(command="donatix", description="Счёт у поставщика Donatix"),
]


def readiness() -> tuple[list[str], list[str]]:
    """Что мешает работать (blockers) и что стоит доделать (warnings)."""
    blockers, warnings = [], []

    if not settings.admin_ids:
        blockers.append(
            "ADMIN_IDS пуст — некому подтверждать пополнения, "
            "деньги будут зависать. Свой ID узнайте у @userinfobot."
        )
    # Без реквизитов бот всё равно запускается: владелец задаёт их прямо
    # в /panel, а до тех пор пополнение картой отвечает клиенту «скоро».
    # Раньше это был отказ запускаться — и бот, только что «активированный»
    # по токену, ID и ключу, молча не работал.
    if not runtime.get("pay_card_number"):
        warnings.append(
            "Не заданы реквизиты карты — пополнение картой пока выключено. "
            "Задайте их в /panel → Реквизиты."
        )
    if runtime.star_price() <= 0:
        blockers.append("Цена звезды должна быть больше нуля (/panel → Цены).")

    # Рабочих режимов выдачи несколько, и проверять надо не «стоит ли
    # api», а «не стоит ли пустышка». Иначе бот при живых продажах через
    # FazerCards уверяет владельца, что звёзды не отправляются, и зовёт
    # его на старый шлюз, хранящий сид-фразу у себя.
    if mode_now() not in DELIVERY_MODES:
        warnings.append(
            f"FRAGMENT_MODE={settings.fragment_mode!r} — бот работает, но "
            "звёзды и Premium НЕ отправляются. Рабочие режимы: "
            + ", ".join(sorted(DELIVERY_MODES)) + "."
        )
    if not texts.support_username():
        warnings.append("Контакт поддержки не задан — владелец задаст его в /panel → 📞 Мой контакт.")

    return blockers, warnings


def print_readiness() -> bool:
    """Печатает отчёт о готовности. False — запускаться нельзя."""
    blockers, warnings = readiness()
    for item in warnings:
        log.warning("⚠️  %s", item)
    for item in blockers:
        log.error("❌ %s", item)
    if blockers:
        log.error(
            "Бот не запущен: сначала исправьте пункты выше. "
            "Проще всего — запустить `python setup.py`."
        )
        return False
    return True


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Чистка секретов — до первой строки журнала. Через бота и API ходят
    # ключи, пропуски и номера карт, и одной случайной строки с токеном
    # хватит, чтобы он навсегда осел в логах сервера.
    from app.userbot.log import guard_root

    guard_root()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Если премиум-эмодзи перестанут приниматься, бот не должен замолчать.
    bot.session.middleware(CustomEmojiGuard())
    # Слишком длинный экран не должен падать ошибкой «text is too long»
    from app.middlewares.length_guard import LengthGuard

    bot.session.middleware(LengthGuard())
    conn = await db.connect()
    await db.init(conn)
    # Журнал WAL сворачивается в базу сам, но только когда её никто не
    # читает. У работающего бота такой тишины не бывает, и журнал
    # растёт месяцами — а чем он длиннее, тем дольше каждая запись.
    # Перезапуск — единственный надёжный момент, когда база пуста.
    await db.compact(conn)
    # Настройки из панели грузим до проверки готовности: реквизиты могли
    # быть заданы через бота, а не в .env.
    await runtime.load(conn)
    await db.load_game_titles(conn)

    if not print_readiness():
        await conn.close()
        await bot.session.close()
        raise SystemExit(1)

    # ManualPayer нужен только режиму mystars: он присылает владельцу
    # ссылку на оплату вместо того, чтобы подписывать перевод самому.
    payer = None
    if mode_now() in MYSTARS_MODES:
        from app.services.mystars import ManualPayer

        payer = ManualPayer(make_sender(bot))
    provider = build_provider(payer)

    # Шаги диалогов — на диске: перезапуск бота не обрывает мастер на полпути
    from pathlib import Path

    from app.fsm_storage import SqliteStorage
    storage = SqliteStorage(Path(settings.db_path).with_name("fsm.sqlite3"))
    dp = Dispatcher(storage=storage, conn=conn, provider=provider)
    # Внешняя мидлварь — до фильтров: команда должна пробиваться
    # сквозь любой незакрытый шаг диалога.
    dp.message.outer_middleware(CommandEscapeMiddleware())
    dp.message.middleware(UserGuardMiddleware())
    dp.callback_query.middleware(UserGuardMiddleware())
    # Снаружи всех: ловит «экран не изменился» из любого обработчика,
    # даже если тот правит сообщение напрямую, без safe_edit.
    # Самым внешним — первым по порядку: aiogram оборачивает прослойки
    # в порядке подключения, и первая оказывается снаружи всех. Любая
    # транзакция, пережившая обработчик или другую прослойку, снимается
    # здесь. Без этого один ранний return после записи вешает замок на
    # общее соединение, и все остальные получают «database is locked».
    dp.message.outer_middleware(TxnGuard())
    dp.callback_query.outer_middleware(TxnGuard())
    dp.callback_query.outer_middleware(SameScreenGuard())
    # Кнопка на сообщении старше двух суток: Telegram доставляет нажатие,
    # но само сообщение заменяет заглушкой без правки. Без этой проверки
    # обработчик падает молча — снаружи это «кнопка не работает».
    dp.callback_query.outer_middleware(StaleScreenGuard())
    # Обязательная подписка — снаружи всего: до неё клиент не должен
    # попадать ни в один экран. Владельца и админов не трогает.
    dp.message.outer_middleware(SponsorGate())
    dp.callback_query.outer_middleware(SponsorGate())

    # Админские роутеры первыми: их фильтр отсекает чужие апдейты
    # и пропускает их дальше по цепочке.
    dp.include_router(donatix.router)
    dp.include_router(paymethods.router)
    dp.include_router(panel.router)
    dp.include_router(broadcast.router)
    dp.include_router(admin.router)
    dp.include_router(menu.router)
    dp.include_router(shop.router)
    dp.include_router(deposit.router)
    dp.include_router(profile.router)
    dp.include_router(support.router)
    dp.include_router(reviews.router)
    dp.include_router(games.router)
    if not donatix.enabled():
        # Раздел «API для разработчиков» — только у самостоятельного бота:
        # у бота из конструктора API даёт сам Donatix.
        dp.include_router(api_cab.router)
    # Последним: ловит нажатия, которые не взял никто, — чаще всего это
    # кнопка из диалога, чьё состояние стёр перезапуск бота. Без него
    # такое нажатие пропадает молча, и клиент видит сломанную кнопку.
    dp.include_router(fallback.router)

    async def on_error(event) -> bool:
        """Поломка в обработчике не должна выглядеть мёртвой кнопкой.

        Без этого любое исключение оставляет у клиента крутящиеся часики
        до упора, а владелец узнаёт о поломке в лучшем случае из логов.
        Отвечаем коротко, подробности — в журнал.
        """
        log.exception("Обработчик упал: %s", event.exception)
        from app.services import access

        upd = event.update
        press = getattr(upd, "callback_query", None)
        message = getattr(upd, "message", None)
        who = (press or message).from_user if (press or message) else None
        admin = who is not None and access.is_admin(who.id)
        # Владельцу — по-русски и с причиной: по ней видно, что чинить. Покупателю — коротко.
        reason = f"{type(event.exception).__name__}: {event.exception}"[:150]
        text = (f"⚠️ Не получилось: {reason}\n\nОткройте /panel и повторите. Если повторится — "
                "перешлите это сообщение в поддержку Donatix." if admin else texts.SOMETHING_BROKE)
        with suppress(Exception):
            if press is not None:
                await press.answer(text[:190], show_alert=True)
            elif message is not None and admin:
                await message.answer(text)
        return True

    dp.errors.register(on_error)

    try:
        me = await bot.me()
        await bot.set_my_commands(
            [c for c in USER_COMMANDS if not (donatix.enabled() and c.command == "api")])
        # У админов в меню команд появляется /panel. Список берём общий:
        # добавленный в панели админ — такой же админ.
        from app.services import access

        for admin_id in access.admins():
            try:
                await bot.set_my_commands(
                    [c for c in ADMIN_COMMANDS if not (donatix.enabled() and c.command == "api")]
                    if donatix.enabled() else [c for c in ADMIN_COMMANDS if c.command != "donatix"],
                    scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except TelegramAPIError:
                log.debug("Не смог поставить команды админу %s", admin_id)
    except TelegramUnauthorizedError:
        log.error(
            "❌ Telegram отверг токен. Проверьте BOT_TOKEN в .env — возможно, "
            "он отозван. Новый берётся у @BotFather: /mybots → бот → API Token."
        )
        await _shutdown(bot, conn, provider)
        raise SystemExit(1) from None
    except TelegramNetworkError as exc:
        log.error(
            "❌ Нет связи с Telegram: %s\n"
            "Проверьте интернет. Если Telegram блокируется провайдером — "
            "запускайте бота на сервере за границей.", exc,
        )
        await _shutdown(bot, conn, provider)
        raise SystemExit(1) from None
    except TelegramAPIError as exc:
        log.error("❌ Telegram вернул ошибку при старте: %s", exc)
        await _shutdown(bot, conn, provider)
        raise SystemExit(1) from None

    log.info("✅ Запущен @%s. Режим Fragment: %s", me.username, settings.fragment_mode)
    log.info("   Админы: %s", ", ".join(map(str, settings.admin_ids)))
    if donatix.enabled():
        # Бот из конструктора Donatix: курс и игры — сразу, без ручной настройки
        asyncio.create_task(donatix.bootstrap(conn, provider))

    # Установили по токену, ID и ключу — пусть бот сам скажет владельцу,
    # что работает и что ещё задать в /panel.
    from app.services import welcome

    with suppress(Exception):
        await welcome.greet_once(bot, conn, me.username, access.admins())

    # Автоцены держат наценку постоянной, пока курс гуляет.
    pricing_task = asyncio.create_task(auto_price_loop(provider, bot))
    # Игровые заказы почти всегда уходят «в обработку»: без присмотра они
    # зависли бы навсегда, а клиент остался бы и без денег, и без товара.
    games_task = asyncio.create_task(games_watch(provider, bot))

    # Поставщик умеет сам сообщать о выдаче. Это не отменяет присмотра
    # выше — отчёт может не дойти, — но с ним клиент узнаёт за секунду,
    # а не через несколько минут опроса.
    from app.services import webhook as webhook_service

    hook = None
    try:
        hook = await webhook_service.serve(bot, provider)
    except OSError as exc:
        log.error("Вебхук не поднялся (%s). Бот работает без него: "
                  "заказы закроются опросом.", exc)

    # Рассылка вебхуков разработчикам, купившим через API. Отдельная
    # задача, а не вызов из каждого места смены статуса: статус меняют
    # пять разных путей, и забыть один — значит молча не сообщить.
    from app.api import hooks as api_hooks

    api_task = asyncio.create_task(api_hooks.loop(conn))

    # Сводка за сутки тем, кто её включил.
    from app.api import digest as api_digest

    digest_task = asyncio.create_task(api_digest.loop(bot))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in (pricing_task, games_task, api_task, digest_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if hook is not None:
            await hook.cleanup()
        await _shutdown(bot, conn, provider)


async def _shutdown(bot: Bot, conn, provider) -> None:
    """Аккуратно закрыть всё: незакрытое соединение с базой держит процесс."""
    from app.services import suppliers

    await suppliers.close_all()
    await provider.close()
    await conn.close()
    await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено")
    except SystemExit as exc:
        # Код выхода должен дойти до systemd, иначе упавший бот
        # будет выглядеть как штатно завершённый и не перезапустится.
        raise SystemExit(exc.code) from None
