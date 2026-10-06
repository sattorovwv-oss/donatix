"""Админ-панель: пополнения, тикеты, заказы, промокоды, рассылка."""
from __future__ import annotations

import asyncio
import logging

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app import runtime
from app.config import settings
from app.services import access
from app.emoji import substitute
from app.money import fmt, parse
from app.services import delivery
from app.services.fragment import DeliveryProvider

log = logging.getLogger(__name__)
router = Router(name="admin")

# Весь роутер доступен только админам.
router.message.filter(F.from_user.func(lambda u: access.is_admin(u.id)))
router.callback_query.filter(F.from_user.func(lambda u: access.is_admin(u.id)))


@router.message(Command("admin", "help"))
async def cmd_admin(message: Message) -> None:
    await message.answer(texts.ADMIN_HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message, conn: aiosqlite.Connection) -> None:
    await message.answer(texts.money_stats(await db.global_stats(conn)))


# ------------------------------------------------------------- пополнения


@router.message(Command("pending"))
async def cmd_pending(message: Message, conn: aiosqlite.Connection) -> None:
    deposits = await db.list_deposits(conn, status=db.DEP_PENDING, limit=15)
    if not deposits:
        await message.answer("Пополнений на проверке нет.")
        return
    lines = [
        f"<b>№{d.id}</b> · {fmt(d.amount)} · <code>{d.user_id}</code>\n"
        f"Зачислить: <code>/dep_ok {d.id}</code> · Отклонить: <code>/dep_no {d.id}</code>"
        for d in deposits
    ]
    await message.answer("🔍 <b>Пополнения на проверке</b>\n\n" + "\n\n".join(lines))


NO_RECEIPT = ("У заявки №{deposit_id} нет чека — зачислить нельзя. "
              "Дождитесь скриншота от клиента или отклоните заявку.")


async def _resolve_deposit(
    conn: aiosqlite.Connection, bot: Bot, deposit_id: int, admin_id: int, approved: bool
) -> str:
    deposit = await db.get_deposit(conn, deposit_id)
    if deposit is None:
        return "Пополнение не найдено."
    # Без чека вручную не зачисляем: сверять не с чем. Юзербот (ROBOT = 0) зачисляет по
    # уведомлению банка — там деньги подтвердил сам банк.
    if approved and admin_id != 0 and not deposit.receipt_file_id:
        return NO_RECEIPT.format(deposit_id=deposit.id)
    if not await db.resolve_deposit(conn, deposit_id, approved=approved, admin_id=admin_id):
        return texts.ADMIN_ALREADY_HANDLED

    if not approved:
        await delivery.notify(bot, deposit.user_id, texts.DEPOSIT_REJECTED.format(
            deposit_id=deposit.id, amount=fmt(deposit.amount), support=texts.support()
        ))
        return texts.ADMIN_DEPOSIT_NO.format(deposit_id=deposit.id)

    await db.credit(conn, deposit.user_id, deposit.amount, as_deposit=True)
    user = await db.get_user(conn, deposit.user_id)
    await delivery.notify(
        bot, deposit.user_id,
        texts.DEPOSIT_APPROVED.format(
            amount=fmt(deposit.amount), balance=fmt(user.balance if user else 0)
        ),
        reply_markup=keyboards.back(),
    )
    await _pay_referral(conn, bot, user, deposit.amount)
    return texts.ADMIN_DEPOSIT_OK.format(deposit_id=deposit.id, amount=fmt(deposit.amount))


async def _pay_referral(
    conn: aiosqlite.Connection, bot: Bot, user: db.User | None, amount: int
) -> None:
    """Начислить пригласившему процент с пополнения."""
    if user is None or not user.referrer_id or runtime.referral_percent() <= 0:
        return
    bonus = amount * runtime.referral_percent() // 100
    if bonus <= 0:
        return
    await db.add_ref_earning(conn, user.referrer_id, bonus)
    referrer = await db.get_user(conn, user.referrer_id)
    await delivery.notify(bot, user.referrer_id, texts.REFERRAL_BONUS.format(
        amount=fmt(bonus), balance=fmt(referrer.balance if referrer else 0)
    ))


@router.callback_query(F.data.startswith("a:dep_ok:"))
async def cb_dep_ok(call: CallbackQuery, conn: aiosqlite.Connection, bot: Bot) -> None:
    deposit_id = int(call.data.rsplit(":", 1)[1])
    report = await _resolve_deposit(conn, bot, deposit_id, call.from_user.id, approved=True)
    if report == NO_RECEIPT.format(deposit_id=deposit_id):
        await call.answer(report[:190], show_alert=True)   # кнопки оставляем: можно отклонить
        return
    await call.answer(report[:190])
    await _strip(call)
    await call.message.reply(report)


@router.callback_query(F.data.startswith("a:dep_no:"))
async def cb_dep_no(call: CallbackQuery, conn: aiosqlite.Connection, bot: Bot) -> None:
    report = await _resolve_deposit(
        conn, bot, int(call.data.rsplit(":", 1)[1]), call.from_user.id, approved=False
    )
    await call.answer(report[:190])
    await _strip(call)
    await call.message.reply(report)


@router.message(Command("dep_ok"))
async def cmd_dep_ok(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    deposit_id = _one_int(command)
    if deposit_id is None:
        await message.answer("Использование: <code>/dep_ok 12</code>")
        return
    await message.answer(await _resolve_deposit(
        conn, bot, deposit_id, message.from_user.id, approved=True
    ))


@router.message(Command("dep_no"))
async def cmd_dep_no(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    deposit_id = _one_int(command)
    if deposit_id is None:
        await message.answer("Использование: <code>/dep_no 12</code>")
        return
    await message.answer(await _resolve_deposit(
        conn, bot, deposit_id, message.from_user.id, approved=False
    ))


# ----------------------------------------------------------------- заказы


@router.message(Command("order"))
async def cmd_order(
    message: Message, command: CommandObject, conn: aiosqlite.Connection
) -> None:
    """Карточка заказа с обоими номерами — своим и на стороне сервиса.

    Ищем и по нашему номеру, и по номеру платформы: когда что-то пошло не
    так, под рукой обычно только один из них.
    """
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Использование: <code>/order 109</code>\n"
            "Работает и с номером на стороне сервиса выдачи."
        )
        return

    order = None
    if query.isdigit():
        order = await db.get_order(conn, int(query))
    if order is None:
        order = await db.find_order_by_external(conn, query)
    if order is None:
        await message.answer(f"Заказ <code>{query}</code> не найден ни по одному номеру.")
        return

    user = await db.get_user(conn, order.user_id)
    buyer = f"@{user.username}" if user and user.username else (
        user.first_name if user else "—"
    )
    external = (
        f"<code>{order.fragment_order_id}</code>"
        if order.fragment_order_id else "<i>не присвоен</i>"
    )
    profit = (f"\n└ Прибыль: <b>{fmt(order.profit)}</b>"
              if order.cost else "\n└ <i>себестоимость не сохранялась</i>")

    await message.answer(substitute(
        f"🧾 <b>Заказ №{order.id}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Статус: {order.status_title}\n"
        f"├ Товар: <b>{order.title}</b>\n"
        f"├ Получатель: <code>@{order.recipient}</code>\n"
        f"└ Создан: {order.created_at}\n\n"
        f"🔗 <b>Номера</b>\n"
        f"├ У нас: <code>{order.id}</code>\n"
        f"└ На платформе: {external}\n\n"
        f"[[money]] <b>Деньги</b>\n"
        f"├ Заплатил клиент: <b>{fmt(order.price)}</b>\n"
        + (f"├ Промокод <code>{order.promo}</code>: <b>−{fmt(order.discount)}</b>\n"
           if order.promo else "")
        + f"├ Себестоимость: <b>{fmt(order.cost)}</b>{profit}\n\n"
        f"👤 Покупатель: {buyer} (<code>{order.user_id}</code>)"
        + (f"\n\n<blockquote expandable>{order.error}</blockquote>"
           if order.error else "")
    ))


@router.message(Command("orders"))
async def cmd_orders(message: Message, conn: aiosqlite.Connection) -> None:
    orders = await db.list_orders(conn, limit=15)
    if not orders:
        await message.answer("Заказов ещё нет.")
        return
    lines = []
    for o in orders:
        external = f" · платформа <code>{o.fragment_order_id}</code>" if o.fragment_order_id else ""
        lines.append(
            f"<b>№{o.id}</b> · {o.status_title}\n"
            f"{o.title} → <code>@{o.recipient}</code>\n"
            f"{fmt(o.price)}{external}"
        )
    await message.answer(
        "📦 <b>Последние заказы</b>\n\n" + "\n\n".join(lines)
        + "\n\n<i>Подробнее:</i> <code>/order 109</code>"
    )


@router.message(Command("retry"))
async def cmd_retry(
    message: Message, command: CommandObject, conn: aiosqlite.Connection,
    provider: DeliveryProvider, bot: Bot,
) -> None:
    order = await _order_from(command, conn)
    if order is None:
        await message.answer("Использование: <code>/retry 42</code> (заказ в статусе ⚠️)")
        return
    if not await delivery.retry_failed(bot, conn, provider, order):
        await message.answer(f"Заказ №{order.id} не в статусе ⚠️ (сейчас: {order.status_title}).")
        return
    fresh = await db.get_order(conn, order.id)
    await message.answer(f"Заказ №{order.id}: {fresh.status_title if fresh else '—'}")


@router.message(Command("refund"))
async def cmd_refund(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    order = await _order_from(command, conn)
    if order is None:
        await message.answer("Использование: <code>/refund 42</code>")
        return
    if not await delivery.manual_refund(bot, conn, order):
        await message.answer(f"Возврат невозможен: статус {order.status_title}.")
        return
    await message.answer(f"↩️ Заказ №{order.id}: {fmt(order.price)} возвращены покупателю.")


@router.message(Command("done"))
async def cmd_done(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    order = await _order_from(command, conn)
    if order is None:
        await message.answer("Использование: <code>/done 42</code>")
        return
    if not await delivery.manual_complete(bot, conn, order):
        await message.answer(f"Нельзя закрыть: статус {order.status_title}.")
        return
    await message.answer(f"✅ Заказ №{order.id} отмечен выполненным.")


@router.callback_query(F.data.startswith("a:retry:"))
async def cb_retry(
    call: CallbackQuery, conn: aiosqlite.Connection, provider: DeliveryProvider, bot: Bot
) -> None:
    order = await db.get_order(conn, int(call.data.rsplit(":", 1)[1]))
    if order is None:
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if not await delivery.retry_failed(bot, conn, provider, order):
        await call.answer(texts.ADMIN_ALREADY_HANDLED, show_alert=True)
        return
    await call.answer("Повторяю выдачу…")
    await _strip(call)


@router.message(Command("balance"))
async def cmd_balance(message: Message, provider: DeliveryProvider) -> None:
    try:
        value = await provider.get_balance()
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"⚠️ Баланс недоступен:\n<code>{exc}</code>")
        return
    await message.answer(f"💼 Баланс Fragment: <b>{value}</b>")


# ----------------------------------------------------------------- тикеты


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, conn: aiosqlite.Connection) -> None:
    tickets = await db.list_tickets(conn, status=db.TICKET_OPEN, limit=15)
    if not tickets:
        await message.answer("Открытых тикетов нет.")
        return
    lines = [
        f"<b>№{t.id}</b> от <code>{t.user_id}</code>\n{t.subject[:200]}\n"
        f"<code>/answer {t.id} текст</code> · <code>/close {t.id}</code>"
        for t in tickets
    ]
    await message.answer("📞 <b>Открытые тикеты</b>\n\n" + "\n\n".join(lines))


@router.message(Command("answer"))
async def cmd_answer(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Использование: <code>/answer 5 Здравствуйте, проверяю…</code>")
        return

    ticket_id, text = int(parts[0]), parts[1]
    ticket = await db.get_ticket(conn, ticket_id)
    if ticket is None:
        await message.answer("Тикет не найден.")
        return

    await db.add_ticket_message(conn, ticket.id, message.from_user.id, is_admin=True, text=text)
    await delivery.notify(
        bot, ticket.user_id,
        texts.TICKET_ADMIN_ANSWER.format(ticket_id=ticket.id, text=text),
        reply_markup=keyboards.back(),
    )
    await message.answer(f"✅ Ответ отправлен в тикет №{ticket.id}.")


@router.message(Command("close"))
async def cmd_close(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    ticket_id = _one_int(command)
    if ticket_id is None:
        await message.answer("Использование: <code>/close 5</code>")
        return
    ticket = await db.get_ticket(conn, ticket_id)
    if ticket is None or not await db.close_ticket(conn, ticket_id):
        await message.answer("Тикет не найден или уже закрыт.")
        return
    await delivery.notify(bot, ticket.user_id, texts.TICKET_CLOSED_USER.format(ticket_id=ticket_id))
    await message.answer(f"✅ Тикет №{ticket_id} закрыт.")


@router.message(Command("notice"))
async def cmd_notice(
    message: Message, command: CommandObject, conn: aiosqlite.Connection
) -> None:
    if not command.args:
        await message.answer(
            "Использование: <code>/notice текст объявления</code>\n"
            "Показывается в разделе «Поддержка»."
        )
        return
    await runtime.set_value(conn, "support_notice", command.args.strip())
    await message.answer("✅ Объявление в поддержке обновлено.")


# ------------------------------------------------------- баланс и юзеры


@router.message(Command("give"))
async def cmd_give(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    await _adjust_balance(message, command, conn, bot, sign=1)


@router.message(Command("take"))
async def cmd_take(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    await _adjust_balance(message, command, conn, bot, sign=-1)


async def _adjust_balance(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot, *, sign: int
) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        verb = "give" if sign > 0 else "take"
        await message.answer(f"Использование: <code>/{verb} 123456789 50</code>")
        return

    user_id, amount = int(parts[0]), parse(parts[1])
    if amount is None or amount <= 0:
        await message.answer("Сумма должна быть положительным числом, например <code>50</code>.")
        return

    user = await db.get_user(conn, user_id)
    if user is None:
        await message.answer("Пользователь не найден.")
        return

    if sign > 0:
        await db.credit(conn, user_id, amount)
        await delivery.notify(
            bot, user_id, f"💰 Вам начислено <b>{fmt(amount)}</b>."
        )
    else:
        if not await db.charge(conn, user_id, amount):
            await message.answer(f"Недостаточно средств: на балансе {fmt(user.balance)}.")
            return
        await delivery.notify(bot, user_id, f"➖ С баланса списано <b>{fmt(amount)}</b>.")

    fresh = await db.get_user(conn, user_id)
    await message.answer(f"✅ Готово. Баланс: <b>{fmt(fresh.balance if fresh else 0)}</b>")


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, conn: aiosqlite.Connection) -> None:
    user_id = _one_int(command)
    if user_id is None:
        await message.answer("Использование: <code>/user 123456789</code>")
        return
    user = await db.get_user(conn, user_id)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    stats = await db.user_order_stats(conn, user_id)
    await message.answer(
        f"👤 <b>{user.first_name or '—'}</b> "
        f"({'@' + user.username if user.username else 'без юзернейма'})\n"
        f"ID: <code>{user.id}</code>\n\n"
        f"💰 Баланс: <b>{fmt(user.balance)}</b>\n"
        f"📥 Депозит: <b>{fmt(user.total_deposit)}</b>\n"
        f"📦 Заказов: <b>{stats['total']}</b> (выполнено {stats['done']})\n"
        f"⭐️ Куплено звёзд: <b>{stats['stars']}</b>\n"
        f"👥 Рефералов: <b>{user.ref_count}</b>, заработано {fmt(user.ref_earned)}\n"
        f"🚫 Забанен: <b>{'да' if user.is_banned else 'нет'}</b>"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, conn: aiosqlite.Connection) -> None:
    await _set_ban(message, command, conn, banned=True)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, conn: aiosqlite.Connection) -> None:
    await _set_ban(message, command, conn, banned=False)


async def _set_ban(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, *, banned: bool
) -> None:
    user_id = _one_int(command)
    if user_id is None:
        await message.answer("Использование: <code>/ban 123456789</code>")
        return
    await db.set_banned(conn, user_id, banned)
    await message.answer(("🚫 Забанен " if banned else "✅ Разбанен ") + f"<code>{user_id}</code>")


# ------------------------------------------------------ промокоды, рассылка


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, conn: aiosqlite.Connection) -> None:
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer(
            "Использование: <code>/promo SALE10 25 100</code>\n"
            "код · сумма в сомони · сколько раз можно активировать"
        )
        return
    amount = parse(parts[1])
    if amount is None or amount <= 0:
        await message.answer("Сумма должна быть положительной, например <code>25</code>.")
        return
    if not await db.create_promo(conn, parts[0], amount, int(parts[2])):
        await message.answer("Такой промокод уже существует.")
        return
    await message.answer(
        f"✅ Промокод <code>{parts[0].upper()}</code> на {fmt(amount)}, "
        f"активаций: {parts[2]}"
    )


@router.message(Command("promos"))
async def cmd_promos(message: Message, conn: aiosqlite.Connection) -> None:
    promos = await db.list_promos(conn)
    if not promos:
        await message.answer("Промокодов нет.")
        return
    lines = [
        f"<code>{p['code']}</code> — "
        + (f"скидка {p['percent']}%" if p["kind"] == "discount" else fmt(p["amount"]))
        + f" · {p['used_count']}/{p['max_uses']}"
        for p in promos
    ]
    await message.answer("🎟 <b>Промокоды</b>\n\n" + "\n".join(lines))


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, bot: Bot
) -> None:
    if not command.args:
        await message.answer("Использование: <code>/broadcast текст сообщения</code>")
        return

    user_ids = await db.all_user_ids(conn)
    status = await message.answer(f"📣 Рассылка на {len(user_ids)} пользователей…")
    sent = failed = 0
    for index, user_id in enumerate(user_ids, start=1):
        try:
            await bot.send_message(user_id, command.args)
            sent += 1
        except TelegramAPIError:
            failed += 1
        # ~20 сообщений в секунду — предел Telegram для рассылок.
        await asyncio.sleep(0.05)
        if index % 50 == 0:
            try:
                await status.edit_text(f"📣 Отправлено {index}/{len(user_ids)}…")
            except TelegramAPIError:
                pass
    await status.edit_text(f"📣 Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Не дошло: {failed}")


# ------------------------------------------------------------------ утилиты


def _one_int(command: CommandObject) -> int | None:
    args = (command.args or "").strip()
    return int(args) if args.isdigit() else None


async def _order_from(command: CommandObject, conn: aiosqlite.Connection) -> db.Order | None:
    order_id = _one_int(command)
    return await db.get_order(conn, order_id) if order_id is not None else None


async def _strip(call: CallbackQuery) -> None:
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError as exc:
        log.debug("Кнопки уже убраны: %s", exc)


#: Признаки того, что до сервиса вообще не дозвонились. Источники
#: сообщают о таком по-разному: одни бросают исключение, другие ловят
#: его сами и возвращают текст, — а лечится это всё одинаково.
OFFLINE_MARKS = (
    "сбой", "timeout", "oserror", "cannot connect", "clientconnector",
    "serverdisconnected", "connectionreset", "нет связи", "ssl",
)


def _offline(row: dict) -> bool:
    text = f"{row.get('verdict', '')} {row.get('error', '')}".lower()
    return any(mark in text for mark in OFFLINE_MARKS)


@router.message(Command("nick"))
async def cmd_nick(message: Message, command: CommandObject) -> None:
    """Спросить ник у всех источников сразу и показать, кто что ответил.

    Сервисы ников бесплатные и живут на чужих хостингах: сегодня отвечает,
    завтра молчит, а послезавтра отдаёт чужой ник. Гадать не надо — вот
    команда, которая проверяет их все на настоящем ID.
    """
    uid = (command.args or "").strip()
    if not uid.isdigit():
        await message.answer(
            "Использование: <code>/nick 1724367212</code>\n\n"
            "<i>Спрошу ник у всех источников и покажу, кто что ответил.</i>"
        )
        return

    from app.services import nicknames

    notice = await message.answer(f"🔎 Спрашиваю ник <code>{uid}</code>…")
    nicknames.forget_all()          # ответ из кэша тут ничего не проверит
    rows = await nicknames.probe(
        uid,
        key=runtime.get("gameskinbo_key") or settings.gameskinbo_key,
        community_key=runtime.get("ff_community_key")
        or settings.ff_community_key,
    )

    lines, names = [], []
    offline = 0        # сколько источников вообще не дозвонились
    for row in rows:
        if row["name"]:
            names.append(row["name"])
            lines.append(f"✅ <b>{row['name']}</b>\n"
                         f"   <i>{row['source']} · {row['seconds']} сек</i>")
            continue

        why = f" — {row['error']}" if row["error"] else ""
        if row["verdict"] == "bad":
            lines.append(f"⛔️ <i>{row['source']}</i> — нет такого игрока{why}")
        elif row["verdict"] == "нет ключа":
            lines.append(f"➖ <i>{row['source']}</i> — ключ не задан")
        else:
            if _offline(row):
                offline += 1
            lines.append(f"❌ <i>{row['source']}</i>{why or ' — пусто'}"
                         f"  <code>{row['seconds']}с</code>")

    unique = set(names)
    asked = [r for r in rows if r["verdict"] != "нет ключа"]
    if not unique and offline >= max(len(asked), 1):
        verdict = (
            "🌐 <b>Ни один сервис не отозвался</b>\n\n"
            "<blockquote>Похоже, дело не в ID: до сервисов не достучался "
            "никто. Так бывает, когда у сервера закрыт выход в интернет "
            "или провайдер режет эти адреса.\n\n"
            "Проверить прямо на сервере:\n"
            "<code>curl -m 10 -s -o /dev/null -w '%{http_code}\\n' "
            "https://glob-info2.vercel.app/info?uid=1</code></blockquote>"
        )
    elif not unique:
        verdict = (
            "😔 <b>Ник не узнал никто</b>\n\n"
            "<blockquote>Связь есть — сервисы ответили, но ника не дали. "
            "Причина у каждого написана выше.\n\n"
            "Скорее всего ID не существует: проверьте каждую цифру "
            "в профиле игры.</blockquote>"
        )
    elif len(unique) == 1:
        verdict = (f"✅ <b>Ник: {names[0]}</b>\n\n"
                   f"<i>Согласны источников: {len(names)}. "
                   "Значит ник верный.</i>")
    else:
        verdict = ("⚠️ <b>Источники не согласны между собой</b>\n\n"
                   "<blockquote>Разные ники на один ID — так быть не должно. "
                   "Пришлите этот экран разработчику: источник, который "
                   "врёт, надо убрать.</blockquote>")

    await notice.edit_text(
        f"🔎 <b>Проверка ника</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"ID: <code>{uid}</code>\n\n"
        + "\n".join(lines) + f"\n\n{verdict}"
    )


@router.message(Command("gorder"))
async def cmd_game_order(
    message: Message, command: CommandObject, conn: aiosqlite.Connection, provider
) -> None:
    """Что поставщик говорит про игровой заказ прямо сейчас.

    Нужна, когда заказ «не дошёл»: показывает сырой ответ, а не наше
    толкование — по нему видно, зависло у поставщика или бот не понял статус.
    """
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Использование: <code>/gorder 26</code> — по нашему номеру\n"
            "или <code>/gorder ord-1296254</code> — по номеру поставщика."
        )
        return

    order = None
    external = query
    if query.isdigit():
        order = await db.get_order(conn, int(query))
        if order is None:
            await message.answer(f"Заказа №{query} нет.")
            return
        if not order.fragment_order_id:
            await message.answer(
                f"У заказа №{order.id} нет номера у поставщика — "
                "спросить его статус нечем."
            )
            return
        external = order.fragment_order_id

    from app.services import games as gsvc
    from app.services import suppliers

    client = suppliers.for_games(provider)
    remote = await client.order_status(external)
    if remote is None:
        await message.answer(
            f"❌ Поставщик не ответил про <code>{external}</code>.\n\n"
            "<i>Проверьте ключ для игр и номер заказа.</i>"
        )
        return

    status = gsvc.status_of(remote)
    verdict = "🟢 выполнен" if status in gsvc.DONE else (
        "🔴 отклонён" if status in gsvc.FAILED else "⏳ ещё в работе"
    )
    ours = ""
    if order is not None:
        ours = (f"├ У нас: №{order.id} — {order.status_title}\n"
                f"├ Списано: <b>{fmt(order.price)}</b>\n")

    await message.answer(
        f"🎮 <b>Заказ у поставщика</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{ours}"
        f"├ Номер: <code>{external}</code>\n"
        f"└ Статус: <b>{status or '—'}</b> {verdict}\n\n"
        f"<blockquote expandable>{str(remote)[:900]}</blockquote>"
    )
