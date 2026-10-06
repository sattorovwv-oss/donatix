"""Что делать с разобранным уведомлением банка.

Порядок шагов выбран так, чтобы одно зачисление не могло закрыть две
заявки и чтобы одно и то же уведомление не сработало дважды:

  1. Уведомление записывается в bank_payments — ДО всякой работы с
     деньгами. Повтор упирается в уникальный индекс и до денег не
     доходит. Проверять отдельным SELECT нельзя: два сообщения подряд
     успевают проскочить между проверкой и вставкой.

  2. Разбор. Не разобралось — платёж остаётся владельцу на руки, и
     никакие деньги не двигаются.

  3. Поиск заявок ровно на эту сумму. Сравнение целыми числами в
     дирамах — точное.

  4. Ровно одна заявка — зачисляем. Несколько — НЕ выбираем наугад:
     владелец решит сам, кто из двоих платил. Ни одной — платёж
     остаётся неопознанным.

Само зачисление делает та же функция, что и кнопка в панели. Свой
второй путь начисления денег разошёлся бы с первым при первой правке:
кто-то поправил бы реферальный процент в одном месте и забыл в другом.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import db, runtime
from app.money import fmt
from app.userbot import parser
from app.userbot.log import get

log = get()

#: Кем помечаются заявки, закрытые юзерботом. Ноль — не человек.
ROBOT = 0


@dataclass
class Result:
    """Чем кончилась обработка одного уведомления."""
    status: str
    amount: int = 0
    deposit_id: int | None = None
    note: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == db.BANK_MATCHED


async def handle(
    conn, bot, *, source: str, message_id: int, text: str,
) -> Result:
    """Обработать одно уведомление банка. Возвращает, чем кончилось."""
    notice = parser.parse(text)
    body = parser.safe_body(text)

    # Записываем ВСЁ, включая неразобранное: владелец должен видеть, что
    # уведомление приходило, даже если бот его не понял.
    payment = await db.claim_bank_payment(
        conn, source=source, message_id=message_id,
        op_code=notice.op_code, amount=notice.amount, sender=notice.sender,
        card_tail=notice.card_tail, bank_time=notice.bank_time,
        status=db.BANK_FAILED, note=notice.error, body=body,
        comment=notice.comment,
    )
    if payment is None:
        log.info("%s Duplicate ignored — сообщение %s уже обрабатывали",
                 "[USERBOT]", message_id)
        return Result(status="duplicate", note="повтор")

    if not notice.ok:
        log.warning("[USERBOT] Parser error — %s (сообщение %s)",
                    notice.error, message_id)
        await _tell_owner(
            bot,
            "🚫 <b>Уведомление банка не разобрал</b>\n"
            f"<blockquote>{notice.error}</blockquote>\n\n"
            "<i>Деньги не зачислены. Проверьте заявку руками: "
            "/panel → 📥 Заявки.</i>",
        )
        return Result(status=db.BANK_FAILED, note=notice.error)

    log.info("[USERBOT] Payment detected")
    log.info("[USERBOT] Amount: %s", fmt(notice.amount))
    if notice.op_code:
        log.info("[USERBOT] Operation code: %s", notice.op_code)
    log.info("[USERBOT] Matching order...")

    hours = runtime.get_int("deposit_match_hours", 6)
    waiting = await db.pending_deposits_for(conn, notice.amount, hours=hours)

    # Сумма с уникальными копейками — сама себе опознавательный знак.
    # Совпала ровно одна заявка — вопросов нет.
    if len(waiting) == 1:
        return await _confirm(conn, bot, payment, waiting[0], notice)

    if len(waiting) > 1:
        # Столкнуться могли только заявки, заведённые до этой затеи.
        # Разберём по плательщику, если он знаком.
        return await _by_sender(conn, bot, payment, waiting, notice)

    # Точной суммы нет. Скорее всего клиент отправил круглую вместо
    # названной: бот просил 10.04, человек по привычке послал 10.00.
    # Ищем рядом — но закрываем, только если рядом ровно одна.
    near = await db.pending_near(conn, notice.amount, hours=hours)
    if len(near) == 1:
        return await _confirm(conn, bot, payment, near[0], notice, near=True)
    if len(near) > 1:
        return await _by_sender(conn, bot, payment, near, notice)

    return await _unknown(conn, bot, payment, notice)


async def claim_for_deposit(conn, bot, deposit) -> Result | None:
    """Найти уже пришедшие деньги под эту заявку и закрыть её.

    Обычный порядок такой: заявка ждёт денег. При оплате из России он
    обратный — человек сначала переводит из Сбербанка, а в бота приходит
    потом, уже с чеком на руках. Уведомление банка к этому моменту лежит
    со статусом «заявки нет», и найти его надо по сумме из чека.

    None — подходящих денег не нашлось: заявка просто ждёт дальше, как
    обычная. Несколько одинаковых сумм тоже дают None: выбрать наугад,
    чьи это деньги, хуже, чем подождать решения владельца.
    """
    hours = runtime.get_int("deposit_match_hours", 6)
    found = await db.unclaimed_bank_payments(conn, deposit.amount, hours=hours)
    if len(found) != 1:
        return None

    payment = found[0]
    notice = parser.Notice(
        amount=payment.amount, op_code=payment.op_code or "",
        sender=payment.sender, card_tail=payment.card_tail,
        bank_time=payment.bank_time, source_field="из ранее пришедшего",
    )
    return await _confirm(conn, bot, payment, deposit, notice)


#: Что банк пишет в приписке у переводов из России. Буквы там бывают
#: вперемешку — латинская C вместо русской С встречается прямо в чеках,
#: поэтому сравниваем по огрублённой строке, а не по точному совпадению.
RUSSIAN_MARKS = ("сбербанк", "тинькофф", "тинькоф", "sberbank", "tinkoff")

#: Латинские двойники русских букв. В приписке «Cбербанк» первая буква
#: латинская — глазом не отличить, для сравнения это разные строки.
LOOKALIKE = str.maketrans("ACEHKMOPTXYacehkmoptxy",
                          "АСЕНКМОРТХУасенкмортху")


def from_russia(comment: str) -> bool:
    """Похожа ли приписка банка на перевод из России."""
    plain = (comment or "").strip().lower().translate(LOOKALIKE)
    return any(mark in plain for mark in RUSSIAN_MARKS)


async def _by_sender(conn, bot, payment, waiting, notice) -> Result:
    """Заявок несколько. Спасает знакомый плательщик или приписка банка."""
    known = await db.sender_owner(conn, notice.sender)
    if known is not None:
        his = [d for d in waiting if d.user_id == known.user_id]
        if his:
            return await _confirm(conn, bot, payment, his[0], notice,
                                  near=his[0].amount != notice.amount)

    # Банк подписал перевод «Сбербанк» — значит из России. Если на эту
    # сумму ждёт ровно одна заявка, заведённая этим же способом, она и
    # есть: совпадение суммы и способа вдвоём уже не случайность.
    if from_russia(notice.comment):
        from app import texts

        theirs = [d for d in waiting if d.method == texts.RU_METHOD]
        if len(theirs) == 1:
            return await _confirm(conn, bot, payment, theirs[0], notice,
                                  near=theirs[0].amount != notice.amount)

    return await _ambiguous(conn, bot, payment, waiting, notice)


async def _confirm(conn, bot, payment, deposit, notice, near=False) -> Result:
    """Закрыть заявку и зачислить деньги.

    near — пришло не ровно столько, сколько просили. Тогда сумму заявки
    подгоняем под то, что правда пришло: зачислять больше отданного
    нельзя, а меньше — обидно и неверно.
    """
    from app.handlers.admin import _resolve_deposit

    asked = deposit.amount
    if near and notice.amount != asked:
        if not await db.set_deposit_amount(conn, deposit.id, notice.amount):
            # Заявку закрыли между нашей проверкой и правкой.
            return await _ambiguous(conn, bot, payment, [deposit], notice)
        deposit = await db.get_deposit(conn, deposit.id) or deposit

    report = await _resolve_deposit(conn, bot, deposit.id, ROBOT, approved=True)
    fresh = await db.get_deposit(conn, deposit.id)

    if fresh is None or fresh.status != db.DEP_APPROVED:
        # Кто-то успел раньше — владелец руками или второй процесс.
        # Деньги уже зачислены им, второй раз не начисляем.
        log.warning("[USERBOT] Заявку %s закрыли раньше нас — не трогаем",
                    deposit.id)
        await db.close_bank_payment(
            conn, payment.id, status=db.BANK_AMBIGUOUS,
            note="заявку закрыли раньше",
        )
        return Result(status=db.BANK_AMBIGUOUS, amount=notice.amount,
                      note="заявку закрыли раньше")

    await db.close_bank_payment(
        conn, payment.id, status=db.BANK_MATCHED, deposit_id=deposit.id,
        note=("сумма отличалась от заявленной" if near
              else f"из поля {notice.source_field}"),
    )
    # Плательщик закрепляется за клиентом ровно здесь — после удачного
    # зачисления, а не по приходу денег: закреплять того, кому мы ещё
    # ничего не отдали, значит запомнить чужую связь по ошибке.
    await db.bind_sender(conn, notice.sender, deposit.user_id)
    log.info("[USERBOT] Payment confirmed: %s", deposit.id)

    await _close_screen(bot, conn, fresh, notice)
    await _tell_owner(
        bot,
        "💳 <b>Оплата подтверждена автоматически</b>\n"
        f"├ Заявка: <code>№{deposit.id}</code>\n"
        f"├ Сумма: <b>{fmt(notice.amount)}</b>\n"
        + (f"├ Просили: <b>{fmt(asked)}</b> — прислали другое\n"
           if near and asked != notice.amount else "")
        + f"├ Клиент: <code>{deposit.user_id}</code>\n"
        + (f"├ Отправитель: <code>{notice.sender}</code>\n"
           if notice.sender else "")
        + (f"├ Приписка банка: <b>{notice.comment}</b>\n"
           if notice.comment else "")
        + (f"├ Код банка: <code>{notice.op_code}</code>\n"
           if notice.op_code else "")
        + f"└ {notice.bank_time or 'время не указано'}",
    )
    return Result(status=db.BANK_MATCHED, amount=notice.amount,
                  deposit_id=deposit.id, note=report)


async def _close_screen(bot, conn, deposit, notice) -> None:
    """Убрать с экрана клиента реквизиты — платить по ним больше нечего.

    Номер карты, висящий после оплаты, путает: человек возвращается в
    чат, видит «переведите» и не понимает, прошло или нет. Поэтому на
    его месте оказывается ответ.
    """
    if not (deposit.pay_chat and deposit.pay_msg):
        return

    user = await db.get_user(conn, deposit.user_id)
    text = (
        "🎉 <b>Пардохт гирифта шуд</b>\n"
        "<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
        f"💰 <b>{fmt(deposit.amount)}</b> ба баланс гузаронида шуд\n"
        f"└ Баланси ҳозира: <b>{fmt(user.balance if user else 0)}</b>"
    )
    try:
        await bot.edit_message_text(
            chat_id=deposit.pay_chat, message_id=deposit.pay_msg,
            text=text, reply_markup=None,
        )
    except Exception as exc:  # noqa: BLE001 — экран не важнее денег
        log.info("[USERBOT] экран реквизитов не заменился: %s", exc)


async def _ambiguous(conn, bot, payment, waiting, notice) -> Result:
    """Заявок несколько. Угадывать нельзя — деньги чужие."""
    note = f"подходящих заявок: {len(waiting)}"
    await db.close_bank_payment(
        conn, payment.id, status=db.BANK_AMBIGUOUS, note=note,
    )
    log.warning("[USERBOT] Ambiguous payment — %s", note)

    rows = "\n".join(
        f"├ <code>№{d.id}</code> — клиент <code>{d.user_id}</code>, "
        f"<code>/dep_ok {d.id}</code>"
        for d in waiting[:8]
    )
    await _tell_owner(
        bot,
        "⚠️ <b>Пришла оплата, но заявок несколько</b>\n"
        f"├ Сумма: <b>{fmt(notice.amount)}</b>\n"
        + (f"├ Отправитель: <code>{notice.sender}</code>\n"
           if notice.sender else "")
        + (f"├ Приписка банка: <b>{notice.comment}</b>\n"
           if notice.comment else "")
        + f"└ Ждут той же суммы: <b>{len(waiting)}</b>\n\n"
        f"{rows}\n\n"
        "<blockquote>Деньги <b>не зачислены</b>. Выбирать наугад нельзя — "
        "зачислили бы чужой платёж. Сверьте отправителя и подтвердите "
        "нужную заявку сами.</blockquote>",
    )
    return Result(status=db.BANK_AMBIGUOUS, amount=notice.amount, note=note)


async def _unknown(conn, bot, payment, notice) -> Result:
    """Заявки на такую сумму нет — платёж остаётся неопознанным."""
    await db.close_bank_payment(
        conn, payment.id, status=db.BANK_UNKNOWN, note="заявки на эту сумму нет",
    )
    log.info("[USERBOT] Unknown payment — %s", fmt(notice.amount))
    await _tell_owner(
        bot,
        "❔ <b>Пришла оплата без заявки</b>\n"
        f"├ Сумма: <b>{fmt(notice.amount)}</b>\n"
        + (f"├ Отправитель: <code>{notice.sender}</code>\n"
           if notice.sender else "")
        + (f"├ Приписка банка: <b>{notice.comment}</b>\n"
           if notice.comment else "")
        + (f"├ Код банка: <code>{notice.op_code}</code>\n"
           if notice.op_code else "")
        + f"└ {notice.bank_time or 'время не указано'}\n\n"
        "<blockquote>Никто не оставлял заявку на такую сумму. Возможно, "
        "клиент заплатил, не нажав «Пополнить», — тогда начислите ему "
        "вручную: /panel → 👥 Клиенты.</blockquote>",
    )
    return Result(status=db.BANK_UNKNOWN, amount=notice.amount)


async def _tell_owner(bot, text: str) -> None:
    """Написать владельцам. Молча падать здесь нельзя, но и ронять
    обработку платежа из-за недоступного чата — тоже."""
    if bot is None:
        return
    from app.services.delivery import notify_admins

    try:
        await notify_admins(bot, text)
    except Exception as exc:  # noqa: BLE001 — письмо не важнее платежа
        log.warning("[USERBOT] не смог написать владельцу: %s", exc)
