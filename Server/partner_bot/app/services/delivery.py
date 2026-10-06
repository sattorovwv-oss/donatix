"""Оплата с баланса, выдача через Fragment и возврат при неудаче.

Порядок операций выбран так, чтобы при любом сбое пользователь не остался
без денег и без товара:

  1. списываем деньги (атомарно, с проверкой баланса в том же UPDATE);
  2. создаём заказ в статусе delivering — он фиксирует, что деньги списаны;
  3. зовём Fragment;
  4. успех  → delivered;
     явный отказ Fragment → возвращаем деньги, refunded;
     нет ответа (таймаут/5xx) → failed, деньги held, разбирается админ.

Шаг 4 разделён намеренно: при таймауте неизвестно, ушли звёзды или нет, и
автовозврат означал бы раздачу товара бесплатно.
"""
from __future__ import annotations

import logging

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app import db, keyboards, runtime, texts
from app.config import settings
from app.money import fmt
from app.services.fragment import DeliveryError, DeliveryProvider, DeliveryUncertain

log = logging.getLogger(__name__)


async def notify(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Отправить сообщение, не роняя вызывающий код, если чат недоступен."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as exc:
        log.warning("Не смог написать в чат %s: %s", chat_id, exc)


async def notify_admins(bot: Bot, text: str, **kwargs) -> None:
    """Всем, у кого есть доступ, а не только владельцам из .env.

    Админа, добавленного в панели, нет в настройках сервера. Пока здесь
    стоял settings.admin_ids, он открывал панель, но не получал ни
    заявок, ни чеков, ни тревог — то есть был админом только на словах.
    """
    from app.services import access

    for admin_id in access.admins():
        await notify(bot, admin_id, text, **kwargs)
    if settings.orders_chat_id:
        await notify(bot, settings.orders_chat_id, text, **kwargs)


#: Статусы, ради которых разработчика стоит будить в любом случае:
#: на них он теряет деньги или клиента.
TROUBLE = (db.ORDER_REFUNDED, db.ORDER_FAILED)


async def tell_buyer(
    bot: Bot, conn: aiosqlite.Connection, order: db.Order, text: str, **kwargs
) -> None:
    """Написать покупателю. Человеку — всегда, разработчику — как просил.

    Заказ, пришедший по API, сделал чужой бот, и его владельцу о статусе
    сообщает вебхук. Писать ему в Telegram про каждый заказ значит
    завалить личку на первой же сотне и упереться в лимиты Telegram —
    поэтому по умолчанию он получает только проблемы: возврат и
    зависший заказ. Полный поток и полную тишину он включает сам.

    Проверяем по базе, а не по флагу: статус заказа меняют пять разных
    путей, и забыть передать флаг в один из них — значит начать спамить.
    """
    if await db.api_order_of(conn, order.id) is None:
        await notify(bot, order.user_id, text, **kwargs)
        return

    mode = (await db.api_prefs(conn, order.user_id))["notify"]
    if mode == "off":
        return
    if mode == "problems":
        # Статус берём из базы: сюда приходят и объекты, собранные до
        # смены статуса, и по ним «проблема» не опозналась бы.
        fresh = await db.get_order(conn, order.id)
        if (fresh.status if fresh else order.status) not in TROUBLE:
            return
    await notify(bot, order.user_id, text, **kwargs)


class NotEnoughFunds(Exception):
    pass


async def purchase(
    bot: Bot,
    conn: aiosqlite.Connection,
    provider: DeliveryProvider,
    *,
    user_id: int,
    product_type: str,
    quantity: int,
    recipient: str,
    price: int,
    promo: str | None = None,
    discount: int = 0,
) -> db.Order:
    """Списать деньги, создать заказ и выдать товар.

    price — уже со скидкой; promo и discount едут в заказ, чтобы активация
    промокода списалась при выдаче, а в отчётах была видна причина скидки.

    Бросает NotEnoughFunds, если баланса не хватило (деньги не тронуты).
    """
    if not await db.charge(conn, user_id, price):
        raise NotEnoughFunds

    # Себестоимость фиксируем в заказе: курс меняется, и без этого прибыль
    # за прошлые дни пересчитывалась бы задним числом.
    # Деньги уже списаны: если заказ не заведётся, вернуть их надо тут же.
    # Иначе покупатель остался бы с пустым балансом и без единого следа
    # о том, куда делись деньги.
    try:
        order = await db.create_order(
            conn, user_id=user_id, product_type=product_type, quantity=quantity,
            recipient=recipient, price=price,
            cost=runtime.cost_of(product_type, quantity),
            promo=promo, discount=discount,
        )
    except Exception:
        await db.credit(conn, user_id, price)
        raise
    log.info("Заказ %s: списано %s с пользователя %s", order.id, fmt(price), user_id)
    await _run_delivery(bot, conn, provider, order)
    refreshed = await db.get_order(conn, order.id)
    return refreshed or order


async def _run_delivery(
    bot: Bot, conn: aiosqlite.Connection, provider: DeliveryProvider, order: db.Order
) -> None:
    try:
        if order.product_type == "stars":
            result = await provider.deliver_stars(order.recipient, order.quantity)
        elif order.product_type == "steam":
            result = await provider.deliver_steam(order.recipient, order.quantity)
        else:
            result = await provider.deliver_premium(order.recipient, order.quantity)

    except DeliveryError as exc:
        # Шлюз ответил отказом — выдачи точно не было, возвращаем деньги.
        await _refund(bot, conn, order, str(exc))
        await _watch_failures(bot, conn, str(exc))

    except DeliveryUncertain as exc:
        # Ответа нет. Деньги придерживаем, зовём админа разобраться вручную.
        await db.transition_order(
            conn, order.id, expected=db.ORDER_DELIVERING, new=db.ORDER_FAILED,
            error=str(exc)[:1000],
        )
        await tell_buyer(
            bot, conn, order,
            f"⏳ <b>Заказ №{order.id} проверяется.</b>\n\n"
            f"Fragment не ответил вовремя. Проверяю вручную — напишу в течение "
            f"нескольких минут. Деньги в безопасности.",
        )
        await notify_admins(
            bot,
            texts.ADMIN_ORDER_FAILED.format(
                order_id=order.id, title=order.title, recipient=order.recipient,
                user_id=order.user_id, error=str(exc)[:400],
            )
            + "\n\n❗️ Проверьте в кабинете Fragment, дошёл ли заказ:\n"
            f"• дошёл → <code>/done {order.id}</code>\n"
            f"• не дошёл → <code>/refund {order.id}</code>",
        )
        log.error("Заказ %s: неопределённый исход — %s", order.id, exc)

    except Exception as exc:  # noqa: BLE001 — баг в коде не должен съесть деньги
        log.exception("Заказ %s: непредвиденная ошибка", order.id)
        await _refund(bot, conn, order, f"{type(exc).__name__}: {exc}")

    else:
        await db.transition_order(
            conn, order.id, expected=db.ORDER_DELIVERING, new=db.ORDER_DELIVERED,
            fragment_order_id=result.order_id, error=None,
        )
        await runtime.note_delivery_ok(conn, order.product_type, order.quantity)
        await tell_buyer(
            bot, conn, order, _done_text(order), reply_markup=keyboards.back()
        )
        await notify_admins(
            bot,
            texts.ADMIN_ORDER_DONE.format(
                order_id=order.id, title=order.title, recipient=order.recipient,
                price=fmt(order.price), user_id=order.user_id,
                external=result.order_id or "—",
            ),
        )
        log.info("Заказ %s выдан, fragment_id=%s", order.id, result.order_id)
        await _ask_review(bot, conn, order)


async def _give_back(conn: aiosqlite.Connection, order: db.Order) -> None:
    """Вернуть деньги за заказ.

    Заказу по API возврат пишется ещё и в журнал транзакций: разработчик
    должен видеть в истории, за что ему вернули деньги, а не гадать,
    почему баланс вырос сам.
    """
    api = await db.api_order_of(conn, order.id)
    if api is None:
        await db.credit(conn, order.user_id, order.price)
        return
    await db.credit_logged(
        conn, order.user_id, order.price, kind="refund",
        order_ref=api["ref"], note=(order.error or "заказ не прошёл")[:190],
    )


async def _refund(
    bot: Bot, conn: aiosqlite.Connection, order: db.Order, reason: str
) -> None:
    """Вернуть деньги за заказ. Переход статуса делается первым, поэтому
    повторный вызов по тому же заказу не начислит деньги дважды."""
    moved = await db.transition_order(
        conn, order.id, expected=db.ORDER_DELIVERING, new=db.ORDER_REFUNDED,
        error=reason[:1000],
    )
    if not moved:
        log.warning("Заказ %s: возврат пропущен, статус уже изменён", order.id)
        return

    await _give_back(conn, order)
    await tell_buyer(
        bot, conn, order,
        texts.REFUNDED.format(
            order_id=order.id, price=fmt(order.price), support=texts.support()
        ),
        reply_markup=keyboards.back(),
    )
    await notify_admins(
        bot,
        texts.ADMIN_ORDER_FAILED.format(
            order_id=order.id, title=order.title, recipient=order.recipient,
            user_id=order.user_id, error=reason[:400],
        ),
    )
    log.warning("Заказ %s: возвращено %s — %s", order.id, fmt(order.price), reason)


async def retry_failed(
    bot: Bot, conn: aiosqlite.Connection, provider: DeliveryProvider, order: db.Order
) -> bool:
    """Повторить выдачу зависшего заказа. Деньги уже списаны, повторно не берём."""
    if not await db.transition_order(
        conn, order.id, expected=db.ORDER_FAILED, new=db.ORDER_DELIVERING
    ):
        return False
    await _run_delivery(bot, conn, provider, order)
    return True


async def manual_refund(bot: Bot, conn: aiosqlite.Connection, order: db.Order) -> bool:
    """Возврат по решению админа для заказа, зависшего в failed."""
    if not await db.transition_order(
        conn, order.id, expected=db.ORDER_FAILED, new=db.ORDER_REFUNDED
    ):
        return False
    await _give_back(conn, order)
    await tell_buyer(
        bot, conn, order,
        texts.REFUNDED.format(
            order_id=order.id, price=fmt(order.price), support=texts.support()
        ),
    )
    return True


async def manual_complete(bot: Bot, conn: aiosqlite.Connection, order: db.Order) -> bool:
    """Админ подтвердил, что заказ всё-таки дошёл."""
    if not await db.transition_order(
        conn, order.id, expected=db.ORDER_FAILED, new=db.ORDER_DELIVERED
    ):
        return False
    await tell_buyer(bot, conn, order, _done_text(order))
    await _ask_review(bot, conn, order)
    return True


def _done_text(order: db.Order) -> str:
    """Сообщение о выполненном заказе. У Steam свой текст: получатель там —
    логин, а не юзернейм Telegram, и путать их нельзя."""
    if order.product_type.startswith("game:"):
        return texts.GAME_DELIVERED.format(
            order_id=order.id, pack=db.product_title(order.product_type),
            player=order.recipient, price=fmt(order.price),
        )
    if order.product_type == "steam":
        return texts.STEAM_DELIVERED.format(
            order_id=order.id, login=order.recipient,
            amount=order.quantity, currency=runtime.steam_currency(),
            price=fmt(order.price),
        )
    return texts.DELIVERED.format(
        order_id=order.id, title=order.title,
        recipient=order.recipient, price=fmt(order.price),
    )


async def _ask_review(bot: Bot, conn: aiosqlite.Connection, order: db.Order) -> None:
    """Спросить отзыв. Импорт внутри: сервис отзывов сам зовёт базу и тексты,
    а на верхнем уровне это замкнуло бы модули друг на друга.

    У заказа по API отзыва не просим: покупал его чужой бот, и просить
    оценку у программы — значит писать её владельцу на каждый заказ.
    """
    from app.services import reviews

    if await db.api_order_of(conn, order.id) is not None:
        return

    try:
        await reviews.offer(bot, conn, order)
    except Exception as exc:  # noqa: BLE001 — отзыв не должен ломать выдачу
        log.info("Заказ %s: не спросил отзыв — %s", order.id, exc)


async def _watch_failures(bot: Bot, conn: aiosqlite.Connection, reason: str) -> None:
    """Считать неудачи подряд и гасить продажу, если выдача сломалась.

    Без этого при пустом кошельке бот продолжал бы принимать заказы: клиенты
    платили бы и получали возврат, а владелец узнавал бы об этом от них.
    """
    streak = await runtime.note_delivery_fail(conn)
    limit = runtime.autostop_after()
    if streak < limit:
        return

    # Молчим только если продажа и правда стояла выключенной — тогда
    # владелец уже предупреждён. Один поднятый флаг основанием не
    # считаем: он переживает включение продажи руками, и тревога о
    # новой поломке ушла бы в никуда.
    already_off = (runtime.get_bool("autostopped")
                   and not runtime.get_bool("stars_enabled")
                   and not runtime.get_bool("premium_enabled"))
    await runtime.autostop(conn)
    if already_off:
        return  # не повторяем тревогу на каждый следующий заказ

    await notify_admins(
        bot,
        "🛑 <b>Продажа выключена автоматически</b>\n\n"
        f"Подряд не прошло заказов: <b>{streak}</b>.\n"
        "Скорее всего кончились деньги на кошельке Fragment "
        "или сломался шлюз.\n\n"
        f"Последняя ошибка:\n<code>{reason[:300]}</code>\n\n"
        "Деньги клиентам возвращены. Пополните кошелёк и отметьте это "
        "в /panel → 💼 Кошелёк — продажа включится обратно.",
    )


async def run(
    bot: Bot, conn: aiosqlite.Connection, provider: DeliveryProvider,
    order: db.Order,
) -> db.Order:
    """Выдать уже созданный и уже оплаченный заказ.

    Нужно API: там деньги списываются и заказ заводится отдельно, чтобы
    успеть связать его с ключом разработчика ДО первой выдачи. Иначе
    выдача считала бы заказ человеческим и написала бы роботу в Telegram.
    """
    await _run_delivery(bot, conn, provider, order)
    return await db.get_order(conn, order.id) or order
