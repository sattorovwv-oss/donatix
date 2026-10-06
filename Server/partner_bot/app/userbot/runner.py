"""Подключение к Telegram и очередь уведомлений.

Юзербот входит в Telegram как человек — под вашим номером. Отсюда три
правила, которые здесь соблюдаются:

  1. Разбираются сообщения ровно от одного источника, заданного в
     BANK_BOT. Всё остальное — чужая переписка, и юзербот её не трогает.

  2. Только новые сообщения. История не читается: там лежат уже
     оплаченные заявки, и один проход по ней зачислил бы всё заново.

  3. Обработка строго по одному. Два уведомления одновременно могли бы
     занять одну и ту же заявку, поэтому они выстраиваются в очередь.

Падать юзербот не должен вообще: он крутится сутками, и интернет за это
время пропадает не раз. Поэтому обрыв связи — не ошибка, а ожидаемое
событие, после которого он ждёт и подключается снова.
"""
from __future__ import annotations

import asyncio
import contextlib

from app import db
from app.config import settings
from app.userbot import processor
from app.userbot.log import get, setup

log = get()

#: Пауза перед новой попыткой подключения. Растёт, чтобы не долбить
#: Telegram, когда интернета нет совсем.
RETRY_MIN = 5
RETRY_MAX = 300

#: После скольких неудач подряд зовём владельца.
#:
#: Юзербот переподключается сам, и обрыв связи на минуту — обычное дело,
#: беспокоить из-за него незачем. Но сессию могут и отозвать («Завершить
#: все сеансы» в Telegram), и тогда он будет молча стучаться в закрытую
#: дверь сутками, пока владелец не заметит, что оплаты перестали
#: подтверждаться. Пять неудач подряд — это уже не связь.
ALERT_AFTER = 5

#: Сколько уведомлений держим в очереди. Больше — значит что-то совсем
#: не так, и лишние лучше потерять, чем съесть всю память.
QUEUE = 100


def source_name() -> str:
    """Кого слушаем. Юзернейм без собачки или числовой id."""
    return (settings.bank_bot or "").strip().lstrip("@")


def _from_source(message, wanted_id: int, wanted_name: str) -> bool:
    """Это точно наш банковский бот?

    Сверяем и по id, и по юзернейму: id надёжнее (юзернейм можно
    перехватить, если банк его освободит), но задать в настройках проще
    юзернейм. Совпасть должно хоть что-то, и ничего не должно
    противоречить.
    """
    sender_id = getattr(message, "sender_id", None)
    if wanted_id and sender_id == wanted_id:
        return True
    if wanted_name:
        sender = getattr(message, "sender", None)
        name = (getattr(sender, "username", "") or "").lower()
        if name and name == wanted_name.lower():
            return True
    return False


async def _drain(queue: asyncio.Queue, bot) -> None:
    """Разбирает очередь по одному уведомлению за раз.

    Своё соединение с базой: задача живёт всё время работы юзербота,
    а чужое соединение может закрыться под ней.
    """
    conn = await db.connect()
    try:
        while True:
            message_id, text = await queue.get()
            try:
                await processor.handle(
                    conn, bot, source=source_name(),
                    message_id=message_id, text=text,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — одно кривое
                # уведомление не должно останавливать остальные
                log.exception("[USERBOT] Обработка сорвалась: %s", exc)
            finally:
                # Соединение живёт часами. Транзакция, зависшая после
                # упавшей вставки, держала бы замок до следующей оплаты —
                # и всё это время бот не мог бы писать в базу.
                if await db.release(conn):
                    log.warning("[USERBOT] уведомление %s оставило "
                                "открытую транзакцию — снята", message_id)
                queue.task_done()
    finally:
        await conn.close()


async def _tell(bot, text: str) -> None:
    """Написать владельцам. Сообщение о поломке не должно ломать то, что
    ещё работает, — поэтому ошибки здесь глотаются."""
    from app.services.delivery import notify_admins

    try:
        await notify_admins(bot, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("[USERBOT] не смог написать владельцу: %s", exc)


async def run(bot=None) -> None:
    """Запустить юзербота. Возвращается только по отмене задачи.

    bot — чем писать владельцу. Свой заводится сам; передают его снаружи
    только проверки, которым настоящий Telegram не нужен.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from telethon import TelegramClient, events

    setup(settings.log_level)

    if not settings.userbot_ready:
        log.error(
            "[USERBOT] Не хватает настроек. Нужны TG_API_ID, TG_API_HASH "
            "и BANK_BOT в .env. api_id и api_hash берутся на my.telegram.org."
        )
        return

    wanted_name = source_name()
    wanted_id = int(wanted_name) if wanted_name.isdigit() else 0
    log.info("[USERBOT] Слушаю уведомления от: %s", wanted_name)

    settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    own_bot = bot is None
    if own_bot:
        bot = Bot(settings.bot_token,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE)
    worker = asyncio.create_task(_drain(queue, bot))
    pause = RETRY_MIN
    misses = 0          # неудач подряд
    told = False        # владельцу уже сообщили о поломке

    try:
        while True:
            client = TelegramClient(
                str(settings.session_file), settings.tg_api_id,
                settings.tg_api_hash,
            )

            @client.on(events.NewMessage(incoming=True))
            async def on_message(event) -> None:      # noqa: ANN001
                # Всё, что не от банка, не читаем и не пишем никуда:
                # это чужая личная переписка.
                if not _from_source(event.message, wanted_id, wanted_name):
                    return
                log.info("[USERBOT] New bank message")
                try:
                    queue.put_nowait((event.message.id, event.message.message or ""))
                except asyncio.QueueFull:
                    log.error("[USERBOT] Очередь переполнена, уведомление "
                              "%s пропущено", event.message.id)

            try:
                await client.start()
                me = await client.get_me()
                log.info("[USERBOT] Подключён как @%s", me.username or me.id)
                if told:
                    await _tell(bot, "✅ <b>Юзербот снова на связи</b>\n\n"
                                     "<i>Оплаты опять подтверждаются сами.</i>")
                pause, misses, told = RETRY_MIN, 0, False
                await client.run_until_disconnected()
                log.warning("[USERBOT] Связь с Telegram пропала")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — обрыв связи не ошибка
                log.warning("[USERBOT] Подключение сорвалось: %s", exc)
                misses += 1
                if misses >= ALERT_AFTER and not told:
                    told = True
                    await _tell(
                        bot,
                        "🔌 <b>Юзербот не может подключиться к Telegram</b>\n"
                        f"├ Попыток подряд: <b>{misses}</b>\n"
                        f"└ <code>{str(exc)[:200]}</code>\n\n"
                        "<blockquote>Оплаты сейчас <b>не подтверждаются "
                        "сами</b> — проверяйте заявки руками: "
                        "/panel → 📥 Заявки.\n\n"
                        "Частая причина — сеанс завершён в Telegram. "
                        "Тогда нужен новый вход: "
                        "<code>stars-bot userbot login</code></blockquote>",
                    )
            finally:
                with contextlib.suppress(Exception):
                    await client.disconnect()

            log.info("[USERBOT] Повтор через %s с", pause)
            await asyncio.sleep(pause)
            pause = min(pause * 2, RETRY_MAX)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        if own_bot:
            with contextlib.suppress(Exception):
                await bot.session.close()
