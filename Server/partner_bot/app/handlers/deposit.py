"""Пополнение баланса переводом на карту с ручной проверкой чека."""
from __future__ import annotations

import logging

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app.services import dcpay, paymethods
from app import runtime
from app.config import settings
from app.money import fmt, parse
from app.states import Deposit

log = logging.getLogger(__name__)
router = Router(name="deposit")


@router.callback_query(F.data == "m:deposit")
async def cb_methods(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        texts.DEPOSIT_METHODS, reply_markup=keyboards.deposit_methods()
    )
    await call.answer()


async def _blocked_by_open(call, conn, state) -> bool:
    """Показать открытую заявку вместо новой. True — дальше не идём.

    Одна заявка на человека — условие, на котором держится весь
    автоматический приём. Платёж узнаётся по сумме с копейками, и две
    открытые заявки дают две суммы, к которым пришедшие деньги подходят
    обе. Выбрать наугад нельзя — платёж уходит владельцу разбирать
    руками, ровно то, от чего мы уходили.
    """
    open_one = await db.open_deposit_of(conn, call.from_user.id)
    if open_one is None:
        return False

    await state.clear()
    await call.message.edit_text(
        texts.DEPOSIT_ALREADY.format(
            amount=fmt(open_one.amount),
            method=texts.method_title(open_one.method),
            when=open_one.created_at[:16].replace("T", " "),
        ),
        reply_markup=keyboards.deposit_open(open_one.id),
    )
    await call.answer()
    return True


@router.callback_query(F.data.startswith("dep:drop:"))
async def cb_drop(call: CallbackQuery, state: FSMContext,
                  conn: aiosqlite.Connection) -> None:
    """Отменить свою заявку — чтобы можно было завести новую."""
    deposit_id = int(call.data.rsplit(":", 1)[1])
    if not await db.cancel_deposit(conn, deposit_id, call.from_user.id):
        await call.answer("Ин дархост аллакай пӯшида шуд.", show_alert=True)
    await state.clear()
    await call.message.edit_text(
        texts.DEPOSIT_CANCELLED.format(support=texts.support()),
        reply_markup=keyboards.deposit_methods(),
    )
    await call.answer()


@router.callback_query(F.data == "dep:soon")
async def cb_soon(call: CallbackQuery) -> None:
    await call.answer(texts.DEPOSIT_SOON, show_alert=True)


# ─────────────────────────────────────────── оплата из России
#
# Порядок здесь обратный обычному. При переводе на таджикскую карту сумму
# назначаем мы: просим 10.04, и по этим копейкам узнаём платёж. Из России
# так нельзя — сколько сомони дойдёт, решает банк при пересчёте рублей, и
# узнаём мы это только из чека клиента.
#
# Зато пересчёт сам делает то, что нам нужно: сумма выходит с копейками,
# и такой второй прямо сейчас ни у кого нет. Поэтому клиент называет её
# из чека, а мы ищем перевод по ней — среди уже пришедших уведомлений
# банка и среди тех, что придут потом.


@router.callback_query(F.data == "dep:ru")
async def cb_ru(call: CallbackQuery, state: FSMContext,
                conn: aiosqlite.Connection) -> None:
    if await _blocked_by_open(call, conn, state):
        return
    await state.clear()
    number = runtime.get("ru_pay_number")
    if not number:
        await call.answer(texts.DEPOSIT_SOON, show_alert=True)
        return
    await call.message.edit_text(
        texts.DEPOSIT_RU_HOW.format(number=number),
        reply_markup=keyboards.deposit_ru(number),
    )
    await call.answer()


@router.callback_query(F.data == "dep:ru_sent")
async def cb_ru_sent(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Deposit.from_receipt)

    # Картинка с примером чека, если владелец её задал. Показать, где
    # искать сумму, короче любого объяснения словами — а ошибиться там
    # легко: рядом стоят рубли, комиссия и зачисленные сомони.
    example = runtime.get("ru_example_photo")
    if example:
        try:
            shown = await call.message.answer_photo(
                example, caption=texts.DEPOSIT_RU_ASK,
                reply_markup=keyboards.cancel(),
            )
            await call.message.edit_reply_markup(reply_markup=None)
            await state.update_data(**_where(shown))
            await call.answer()
            return
        except TelegramAPIError:
            pass      # картинка не отправилась — обойдёмся текстом

    await call.message.edit_text(texts.DEPOSIT_RU_ASK,
                                 reply_markup=keyboards.cancel())
    # Запоминаем этот экран: если деньги уже пришли, ответ встанет прямо
    # на его место — вместе с кнопкой «Отмена», которой там больше не
    # место.
    await state.update_data(**_where(call.message))
    await call.answer()


@router.message(Deposit.from_receipt, F.text)
async def on_receipt_amount(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    amount = parse(message.text or "")
    if amount is None or amount <= 0:
        await message.answer(texts.DEPOSIT_BAD_AMOUNT)
        return
    if amount < runtime.min_deposit():
        await message.answer(
            texts.DEPOSIT_TOO_SMALL.format(min_amount=fmt(runtime.min_deposit()))
        )
        return

    # Ровная сумма — почти наверняка описка: в чеке пересчёта копейки
    # есть всегда. Один раз переспрашиваем, второй — принимаем как есть:
    # спорить с человеком, который смотрит в свой чек, мы не вправе.
    data = await state.get_data()
    if amount % 100 == 0 and data.get("round_asked") != amount:
        await state.update_data(round_asked=amount)
        await message.answer(texts.DEPOSIT_RU_ROUND)
        return

    deposit = await db.create_deposit(
        conn, user_id=message.from_user.id, amount=amount,
        method=texts.RU_METHOD, receipt_file_id="",
        reference=dcpay.make_reference(),
    )
    await state.update_data(amount=amount, deposit_id=deposit.id,
                            reference=deposit.reference)
    await state.set_state(Deposit.receipt)

    # Экран с вопросом о сумме — тот самый, что заменится на «оплата
    # получена», если деньги уже ждут. Привязываем его к заявке ДО
    # поиска: иначе ответ уйдёт отдельным сообщением, а вопрос с красной
    # кнопкой останется висеть выше.
    if data.get("ask_chat") and data.get("ask_msg"):
        await db.set_deposit_screen(conn, deposit.id,
                                    data["ask_chat"], data["ask_msg"])

    # Деньги могли прийти раньше клиента: он перевёл из Сбербанка, дождался
    # зачисления и только потом открыл бота. Тогда уведомление банка уже
    # лежит непривязанным — ищем его сразу, не заставляя ждать впустую.
    from app.userbot.processor import claim_for_deposit

    done = await claim_for_deposit(conn, bot, deposit)
    if done is not None:
        await state.clear()
        return

    # Денег пока нет — ждём. Кнопки с вопроса убираем: отвечать на него
    # больше нечего, а «Отмена» под ним теперь только сбивает.
    await _drop_buttons(bot, data)
    shown = await message.answer(
        texts.DEPOSIT_RU_WAIT.format(amount=fmt(amount)),
        reply_markup=keyboards.deposit_receipt(),
    )
    await _remember_screen(conn, deposit.id, shown)


@router.callback_query(F.data.startswith("dep:pm:"))
async def cb_pick_method(call: CallbackQuery, state: FSMContext,
                         conn: aiosqlite.Connection) -> None:
    """Покупатель выбрал банк из тех, что владелец добавил в «Реквизитах»."""
    from app.services import paymethods
    if await _blocked_by_open(call, conn, state):
        return
    method = paymethods.get(call.data.split(":", 2)[2])
    if method is None or not method.get("enabled", True):
        await state.clear()
        await call.answer(texts.DEPOSIT_SOON, show_alert=True)
        return
    await state.set_state(Deposit.amount)
    await state.update_data(pm=method["id"])
    await call.message.edit_text(
        f"🏦 <b>{method['bank']}</b>\n\n"
        + texts.DEPOSIT_ASK_AMOUNT.format(min_amount=fmt(runtime.min_deposit())),
        reply_markup=keyboards.cancel(),
    )
    await state.update_data(**_where(call.message))
    await call.answer()


@router.callback_query(F.data == "dep:card")
async def cb_card(call: CallbackQuery, state: FSMContext,
                  conn: aiosqlite.Connection) -> None:
    if await _blocked_by_open(call, conn, state):
        return
    # Бот мог быть запущен без реквизитов: владелец ввёл токен, ID и ключ
    # и нажал «активировать». Пускать клиента дальше незачем — он
    # переведёт деньги в никуда. Пусть лучше увидит «скоро».
    if not runtime.get("pay_card_number"):
        await state.clear()
        await call.answer(texts.DEPOSIT_SOON, show_alert=True)
        return
    await state.set_state(Deposit.amount)
    await call.message.edit_text(
        texts.DEPOSIT_ASK_AMOUNT.format(min_amount=fmt(runtime.min_deposit())),
        reply_markup=keyboards.cancel(),
    )
    await state.update_data(**_where(call.message))
    await call.answer()


@router.message(Deposit.amount, F.text)
async def on_amount(
    message: Message, state: FSMContext, conn: aiosqlite.Connection,
    bot: Bot | None = None,
) -> None:
    amount = parse(message.text or "")
    if amount is None or amount <= 0:
        await message.answer(texts.DEPOSIT_BAD_AMOUNT)
        return
    if amount < runtime.min_deposit():
        await message.answer(
            texts.DEPOSIT_TOO_SMALL.format(min_amount=fmt(runtime.min_deposit()))
        )
        return

    from app.handlers import donatix
    await donatix.sync_rate(conn, donatix.RATE_PAYMENT)  # сумма в USDT — по свежему курсу

    # Код платежа нужен, чтобы найти перевод в выписке, даже если чек
    # придёт позже или не придёт вовсе.
    reference = dcpay.make_reference()

    # К сумме добавляются копейки — не для красоты, а чтобы платёж стал
    # узнаваемым. Клиент просит 10 сомони, платит 10.04, и такая сумма
    # прямо сейчас не ждёт больше никого. Тогда уведомление банка
    # опознаётся одной цифрой, без чеков и без гадания, чей это перевод.
    # Но копейки нужны только там, где платежи узнаёт юзербот по уведомлению
    # банка. Где оплату проверяет владелец по чеку (боты из конструктора),
    # «10.04 вместо 10» только путает клиента — там сумма остаётся ровной.
    if settings.userbot_ready:
        amount = await db.free_amount(
            conn, amount, hours=runtime.get_int("deposit_match_hours", 6)
        )

    # Заявку заводим ПРЯМО СЕЙЧАС, до оплаты и до всякого чека.
    #
    # Раньше она появлялась только после присланного чека — и это ломало
    # автоматику: клиент платил, банк присылал уведомление, а сопоставлять
    # его было не с чем, заявки ещё не существовало. Каждый такой платёж
    # приходилось подтверждать руками, ровно то, от чего мы уходим.
    #
    # Теперь порядок обратный: заявка ждёт денег, а не деньги ждут заявку.
    from app.services import paymethods
    chosen = paymethods.get((await state.get_data()).get("pm") or "")
    deposit = await db.create_deposit(
        conn, user_id=message.from_user.id, amount=amount,
        method=f"Перевод · {chosen['bank']}" if chosen else "Перевод на карту",
        receipt_file_id="", reference=reference,
    )
    await state.update_data(amount=amount, reference=reference,
                            deposit_id=deposit.id)
    await state.set_state(Deposit.receipt)

    # Сумма названа — вопрос о ней отвечен, и «Отмена» под ним больше не
    # нужна: нажатая позже, она отменяет уже идущую оплату.
    if bot is not None:
        await _drop_buttons(bot, await state.get_data())

    body, markup = _requisites(amount, reference, chosen)
    shown = await message.answer(body, reply_markup=markup)

    # Запоминаем, где показаны реквизиты: когда деньги придут, номер
    # карты надо будет убрать с экрана — платить по нему больше нечего.
    await _remember_screen(conn, deposit.id, shown)


def _where(message) -> dict:
    """Координаты сообщения: чтобы потом заменить его или убрать кнопки."""
    chat = getattr(getattr(message, "chat", None), "id", None)
    msg_id = getattr(message, "message_id", None)
    return {"ask_chat": chat, "ask_msg": msg_id} if chat and msg_id else {}


async def _drop_buttons(bot, data: dict) -> None:
    """Убрать кнопки с экрана, на который уже ответили.

    Красная «Отмена» под вопросом о сумме остаётся висеть и после того,
    как сумма названа. Человек возвращается к ней и нажимает — отменяя
    оплату, которая, может быть, уже прошла.
    """
    chat, msg_id = data.get("ask_chat"), data.get("ask_msg")
    if not (chat and msg_id):
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat, message_id=msg_id,
                                            reply_markup=None)
    except TelegramAPIError:
        pass          # сообщение удалили или оно уже без кнопок


async def _remember_screen(conn, deposit_id: int, shown) -> None:
    chat = getattr(getattr(shown, "chat", None), "id", None)
    msg_id = getattr(shown, "message_id", None)
    if chat and msg_id:
        await db.set_deposit_screen(conn, deposit_id, chat, msg_id)


def _requisites(amount: int, reference: str, method: dict | None = None) -> tuple[str, object]:
    """Экран реквизитов: один, короткий, с кнопками под рукой.

    Раньше на нём же просили прислать чек. Клиент этого ещё не сделал —
    и просьба тонула в длинном тексте. Теперь про чек говорим отдельным
    шагом, после нажатия «Я оплатил».
    """
    card = runtime.get("pay_card_number") or "— реквизиты не заданы —"
    holder = runtime.get("pay_card_holder")
    bank = runtime.get("pay_card_bank")
    city = runtime.get("pay_city")
    note = runtime.get("pay_extra")
    crypto = None
    if method:  # способ, выбранный покупателем (бот из конструктора)
        card, holder, bank, city, note = method["number"], method.get("holder"), method["bank"], "", ""
        crypto = paymethods.crypto(bank)
        if crypto:
            bank = f"{bank} · шабака / сеть <b>{crypto[1]}</b>"
            rate = runtime.usd_rate()
            usdt = f"≈ <b>{amount / rate:.2f} USDT</b>\n" if rate > 0 else ""
            note = (usdt + f"⚠️ Танҳо дар шабакаи <b>{crypto[0]}</b>! / Только в сети <b>{crypto[0]}</b> — "
                    "в другой сети деньги не дойдут.")

    where = " · ".join(part for part in (bank, city) if part)
    body = texts.DEPOSIT_REQUISITES.format(
        amount=fmt(amount),
        card=card,
        holder=f"👤 <b>{holder}</b>\n" if holder else "",
        bank=f"🏦 {where}\n" if where else "",
        extra=f"\n{note}\n" if note else "\n",
        dc_block=texts.DEPOSIT_DC_BLOCK.format(reference=reference),
    )

    link = ""
    if dcpay.is_ready():
        link = dcpay.build_link(
            dcpay.account(), amount,
            dcpay.build_comment(dcpay.comment_prefix(), reference),
            dcpay.service(),
        )
    # Копировать даём только настоящий номер: владелец мог записать
    # реквизиты словами, и кнопка «скопировать» скопировала бы фразу.
    digits = "".join(ch for ch in card if ch.isdigit())
    if crypto:
        return body, keyboards.deposit_pay(link, card)
    return body, keyboards.deposit_pay(link, digits if len(digits) >= 8 else "")


async def _restore(call: CallbackQuery, state: FSMContext, conn) -> dict | None:
    """Данные заявки для шага чека.

    Обычно они в состоянии. Но экран «У вас уже есть заявка» состояние
    очищает, да и после перезапуска бота его может не быть — а кнопка
    «Чек фиристодан» на экране остаётся. Тогда берём открытую заявку из базы,
    иначе нажатие упиралось в «экран устарел».
    """
    data = await state.get_data()
    if data.get("amount"):
        return data
    open_one = await db.open_deposit_of(conn, call.from_user.id)
    if open_one is None:
        await state.clear()
        await call.answer("Дархост гум шуд, аз нав сар кунед.", show_alert=True)
        return None
    pm = next((m["id"] for m in paymethods.all_methods()
               if open_one.method == f"Перевод · {m['bank']}"), "")
    await state.set_state(Deposit.receipt)
    await state.update_data(amount=open_one.amount, reference=open_one.reference or "",
                            deposit_id=open_one.id, pm=pm)
    return await state.get_data()


@router.callback_query(F.data == "dep:paid")
async def cb_paid(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """«Я оплатил» / «Чек фиристодан».

    Постоянному клиенту чек не нужен вовсе: его плательщика мы уже знаем
    по прошлым переводам и узнаем этот. Просить у него скриншот каждый
    раз — работа без пользы.
    """
    data = await _restore(call, state, conn)
    if data is None:
        return

    await call.message.edit_text(
        texts.DEPOSIT_WAITING.format(amount=fmt(data["amount"])),
        reply_markup=keyboards.deposit_receipt(),
    )
    await call.answer()


@router.callback_query(F.data == "dep:back")
async def cb_back_to_requisites(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Вернуться к реквизитам: клиент мог закрыть банк, не заплатив."""
    data = await _restore(call, state, conn)
    if data is None:
        return
    # Реквизиты — того банка, который выбрал покупатель, а не первые попавшиеся
    body, markup = _requisites(data["amount"], data.get("reference", ""),
                               paymethods.get(data.get("pm") or ""))
    await call.message.edit_text(body, reply_markup=markup)
    if data.get("deposit_id"):
        await _remember_screen(conn, data["deposit_id"], call.message)
    await call.answer()


@router.message(Deposit.receipt, F.photo | F.document)
async def on_receipt(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    data = await state.get_data()
    amount = data.get("amount")
    if not amount:
        await state.clear()
        await message.answer("Дархост гум шуд. Аз нав сар кунед: /menu")
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id

    # Тот же чек второй раз. Шлют его и от беспокойства, и затем, чтобы
    # получить второе зачисление за один перевод. Отпечаток файла у
    # Telegram переживает пересылку, поэтому сравниваем по нему.
    if await db.receipt_seen(conn, message.from_user.id, file_id):
        await message.answer(texts.DEPOSIT_RECEIPT_OLD)
        return

    # Чек по этой заявке уже есть — повтор ничего не ускоряет, а
    # владельцу добавляет одинаковых картинок на разбор.
    already = await db.get_deposit(conn, data.get("deposit_id") or 0)
    if already is not None and already.receipt_file_id:
        await message.answer(
            texts.DEPOSIT_RECEIPT_TWICE.format(amount=fmt(already.amount))
        )
        return

    # Заявка уже создана на шаге суммы — чек к ней прикрепляется.
    # Заводить вторую нельзя: две заявки на одну сумму сделали бы платёж
    # спорным, и юзербот отказался бы зачислять его сам.
    deposit = await db.get_deposit(conn, data.get("deposit_id") or 0)
    if deposit is None or deposit.user_id != message.from_user.id:
        deposit = await db.create_deposit(
            conn, user_id=message.from_user.id, amount=amount,
            method="Перевод на карту", receipt_file_id=file_id,
            reference=data.get("reference"),
        )
    else:
        await db.attach_receipt(conn, deposit.id, file_id)

    await state.clear()

    # Чек — вторая половина условия. Деньги от банка могли прийти раньше
    # и ждать именно этого: теперь обе части на месте, можно зачислять.
    credited = await _credit_if_paid(conn, bot, deposit)

    fresh = await db.get_deposit(conn, deposit.id)
    paid = fresh is not None and fresh.status == db.DEP_APPROVED

    # Про пополнение клиенту уже написали при зачислении — второй раз
    # то же самое слать нельзя: выглядит как два разных пополнения.
    if not credited:
        if paid:
            user = await db.get_user(conn, message.from_user.id)
            await message.answer(
                texts.DEPOSIT_APPROVED.format(
                    amount=fmt(amount), balance=fmt(user.balance if user else 0)
                ),
                reply_markup=keyboards.back(),
            )
        else:
            await message.answer(
                texts.DEPOSIT_SENT.format(deposit_id=deposit.id,
                                          amount=fmt(amount)),
                reply_markup=keyboards.back(),
            )

    await _send_receipt(message, conn, deposit, amount, paid)


async def _credit_if_paid(conn, bot, deposit) -> bool:
    """Зачислить, если перевод уже пришёл и ждал чека.

    True — деньги зачислены прямо сейчас, и клиенту об этом написали.

    Порядок «сначала чек, потом деньги» выбран владельцем нарочно:
    совпадения одной суммы мало, чтобы решить, чей это перевод. Зато
    когда обе половины на месте, подтверждать вручную уже нечего.
    """
    from app.handlers.admin import _resolve_deposit
    from app.userbot.processor import ROBOT

    held = await db.held_bank_payment(conn, deposit.id)
    if held is None:
        return False

    await _resolve_deposit(conn, bot, deposit.id, ROBOT, approved=True)
    fresh = await db.get_deposit(conn, deposit.id)
    if fresh is None or fresh.status != db.DEP_APPROVED:
        return False

    await db.close_bank_payment(
        conn, held.id, status=db.BANK_MATCHED, deposit_id=deposit.id,
        note="зачислено после чека",
    )
    # Плательщик теперь знаком: в следующий раз чек у этого клиента
    # спрашивать не будем — деньги зачислятся сразу.
    await db.bind_sender(conn, held.sender, deposit.user_id)
    log.info("Заявка %s закрыта: пришёл чек к уже полученному переводу",
             deposit.id)
    return True


async def _send_receipt(message, conn, deposit, amount: int, paid: bool) -> None:
    """Переслать чек владельцу.

    Чек уходит и тогда, когда деньги уже зачислены автоматически: сверять
    нечего, но при споре с клиентом он понадобится, а искать его потом
    будет негде.

    Разница в подписи и кнопках. У зачисленной заявки кнопок «зачислить»
    и «отклонить» нет: нажимать их некуда, а вид рабочей кнопки, которая
    ничего не делает, сбивает с толку в самый неподходящий момент.
    """
    buyer = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or "без имени"
    )

    if paid:
        bank = await db.bank_payment_for_deposit(conn, deposit.id)
        caption = texts.ADMIN_DEPOSIT_RECEIPT.format(
            deposit_id=deposit.id, amount=fmt(amount),
            paid=fmt(bank.amount) if bank else fmt(amount),
            code=(bank.op_code if bank and bank.op_code else "—"),
            sender=(bank.sender if bank and bank.sender else "—"),
            buyer=buyer, user_id=message.from_user.id,
        )
        markup = None
    else:
        caption = texts.ADMIN_NEW_DEPOSIT.format(
            deposit_id=deposit.id, amount=fmt(amount), method=deposit.method,
            buyer=buyer, user_id=message.from_user.id,
            reference=deposit.reference or "—",
        )
        markup = keyboards.admin_deposit(deposit.id)

    from app.services import access

    targets = list(access.admins())
    if settings.orders_chat_id:
        targets.append(settings.orders_chat_id)
    for chat_id in targets:
        try:
            await message.copy_to(chat_id, caption=caption, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("Заявка %s не ушла в чат %s: %s", deposit.id, chat_id, exc)


@router.message(Deposit.receipt)
async def on_receipt_wrong(message: Message) -> None:
    await message.answer(texts.DEPOSIT_NEED_PHOTO)

