"""Покупка звёзд и Premium: количество → получатель → подтверждение → выдача."""
from __future__ import annotations

import logging
import re

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app import runtime
from app.handlers.menu import main_markup, menu_text
from app.money import (
    affordable_stars, discount_of, fmt, fmt4, stars_cost, steam_cost,
)
from app.services import delivery
from app.services.fragment import DeliveryProvider, Recipient
from app.states import Buy, Steam

log = logging.getLogger(__name__)
router = Router(name="shop")

USERNAME_RE = re.compile(r"^@?([a-zA-Z][a-zA-Z0-9_]{4,31})$")


def parse_username(raw: str) -> str | None:
    """'@name', 'name', 'https://t.me/name' -> 'name'."""
    text = raw.strip().rstrip("/").rsplit("/", 1)[-1]
    match = USERNAME_RE.match(text)
    return match.group(1) if match else None


def title_of(product_type: str, quantity: int) -> str:
    return (f"⭐️ {quantity} ситора" if product_type == "stars"
            else f"👑 Premium {quantity} моҳ")


# ------------------------------------------------------------------ звёзды


@router.callback_query(F.data == "m:stars")
async def cb_stars(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        texts.STARS_ENTRY.format(rate=fmt4(runtime.star_price_e4())),
        reply_markup=keyboards.stars_entry(),
    )
    await call.answer()


@router.callback_query(F.data == "stars:buy")
async def cb_stars_buy(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    user = await db.get_user(conn, call.from_user.id)
    balance = user.balance if user else 0
    await state.set_state(Buy.quantity)
    await state.update_data(product_type="stars")
    await call.message.edit_text(
        texts.STARS_ASK_QUANTITY.format(
            rate=fmt4(runtime.star_price_e4()),
            min_stars=runtime.min_stars(),
            max_stars=f"{runtime.max_stars():,}".replace(",", " "),
            affordable=affordable_stars(balance),
            balance=fmt(balance),
        ),
        reply_markup=keyboards.cancel(),
    )
    await call.answer()


@router.message(Buy.quantity, F.text)
async def on_quantity(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        await message.answer(texts.STARS_BAD_QUANTITY.format(
            min_stars=runtime.min_stars(), max_stars=runtime.max_stars()
        ))
        return

    quantity = int(raw)
    if not runtime.min_stars() <= quantity <= runtime.max_stars():
        await message.answer(texts.STARS_BAD_QUANTITY.format(
            min_stars=runtime.min_stars(), max_stars=runtime.max_stars()
        ))
        return

    await _start_stars_order(message, state, conn, quantity)


async def _start_stars_order(
    target: Message | CallbackQuery, state: FSMContext,
    conn: aiosqlite.Connection, quantity: int,
) -> bool:
    """Посчитать цену, проверить баланс и спросить получателя.

    Одна дорога и для кнопки-набора, и для введённого вручную числа —
    иначе проверка баланса разъехалась бы между ними.
    """
    price = stars_cost(quantity)
    user = await db.get_user(conn, target.from_user.id)
    balance = user.balance if user else 0

    if balance < price:
        text = texts.STARS_NOT_ENOUGH.format(
            need=fmt(price), balance=fmt(balance), missing=fmt(price - balance),
        )
        markup = keyboards.deposit_methods()
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        await state.clear()
        return False

    await state.update_data(product_type="stars", quantity=quantity, price=price)
    await _ask_recipient(target, state, "stars", quantity, price)
    return True


@router.callback_query(F.data.startswith("stars:pack:"))
async def cb_stars_pack(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Готовый набор: количество уже известно, спрашивать нечего."""
    quantity = int(call.data.rsplit(":", 1)[1])
    if quantity not in runtime.star_packs():
        await call.answer("Ин маҷмӯа дигар нест.", show_alert=True)
        await call.message.edit_text(
            texts.STARS_ENTRY.format(rate=fmt4(runtime.star_price_e4())),
            reply_markup=keyboards.stars_entry(),
        )
        return
    await state.clear()
    await _start_stars_order(call, state, conn, quantity)
    await call.answer()


# ----------------------------------------------------------------- premium


@router.callback_query(F.data == "m:premium")
async def cb_premium(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(texts.PREMIUM_ENTRY, reply_markup=keyboards.premium_menu())
    await call.answer()


@router.callback_query(F.data.startswith("premium:"))
async def cb_premium_plan(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    months = int(call.data.split(":")[1])
    plan = runtime.find_premium(months)
    if plan is None:
        await call.answer("Ин таъриф дигар нест.", show_alert=True)
        return

    price = int(plan["price"])
    user = await db.get_user(conn, call.from_user.id)
    balance = user.balance if user else 0
    if balance < price:
        await call.message.edit_text(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(price), balance=fmt(balance), missing=fmt(price - balance)
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        await call.answer()
        return

    await state.update_data(product_type="premium", quantity=months, price=price)
    await _ask_recipient(call, state, "premium", months, price)
    await call.answer()


# -------------------------------------------------------------- получатель


async def _ask_recipient(
    target: Message | CallbackQuery, state: FSMContext,
    product_type: str, quantity: int, price: int,
) -> None:
    await state.set_state(Buy.recipient)
    text = texts.ASK_RECIPIENT.format(
        title=title_of(product_type, quantity), price=fmt(price)
    )
    markup = keyboards.ask_recipient(has_username=bool(target.from_user.username))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "order:again")
async def cb_change_recipient(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("product_type"):
        await call.answer("Фармоишро аз нав сар кунед: /menu", show_alert=True)
        return
    await _ask_recipient(
        call, state, data["product_type"], data["quantity"], data["price"]
    )
    await call.answer()


@router.callback_query(Buy.recipient, F.data == "order:self")
async def cb_buy_for_self(
    call: CallbackQuery, state: FSMContext, provider: DeliveryProvider
) -> None:
    """Покупка себе: юзернейм берём из аккаунта, вручную вводить не надо."""
    username = call.from_user.username
    if not username:
        await call.message.edit_text(
            texts.NO_OWN_USERNAME, reply_markup=keyboards.back()
        )
        await call.answer()
        return
    await call.answer("Ҳисоби шуморо месанҷам…")
    await _check_and_confirm(call.message, state, provider, username,
                             buyer_username=username, edit=True)


@router.message(Buy.recipient, F.text)
async def on_recipient(
    message: Message, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider,
) -> None:
    username = parse_username(message.text or "")
    if not username:
        await message.answer(texts.BAD_USERNAME)
        return
    notice = await message.answer(texts.CHECKING_RECIPIENT.format(username=username))
    await _check_and_confirm(
        notice, state, provider, username,
        buyer_username=message.from_user.username, edit=True,
    )


async def _check_and_confirm(
    target: Message, state: FSMContext, provider: DeliveryProvider,
    username: str, *, buyer_username: str | None, edit: bool = False,
) -> None:
    """Спросить Fragment об аккаунте и показать его имя на подтверждение."""
    recipient: Recipient | None = await provider.resolve_recipient(username)

    async def show(text: str, markup) -> None:
        if edit:
            await target.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)

    if recipient is None:
        await show(texts.UNKNOWN_RECIPIENT.format(username=username),
                   keyboards.cancel("‹ В меню"))
        return

    is_self = bool(buyer_username) and buyer_username.lower() == username.lower()
    data = await state.get_data()
    await state.update_data(recipient=username, recipient_name=recipient.display)
    await state.set_state(Buy.check_recipient)

    who = texts.RECIPIENT_IS_YOU if is_self else texts.RECIPIENT_IS_OTHER
    title = title_of(data["product_type"], data["quantity"])

    if recipient.verified:
        text = texts.CONFIRM_RECIPIENT.format(
            name=recipient.display, username=username, who=who, title=title,
        )
    else:
        # Сервис выдачи не подтверждает имя — не делаем вид, что подтвердил.
        text = texts.CONFIRM_RECIPIENT_UNVERIFIED.format(
            username=username, who=who, title=title,
        )
    await show(text, keyboards.confirm_recipient())


def totals(data: dict) -> tuple[int, int, int]:
    """(полная цена, скидка, к списанию) для текущего заказа."""
    price = int(data.get("price") or 0)
    discount = discount_of(price, int(data.get("promo_percent") or 0))
    return price, discount, price - discount


async def show_confirm(
    target: Message, state: FSMContext, conn: aiosqlite.Connection, user_id: int
) -> None:
    """Итоговая сводка заказа — с учётом промокода, если он введён."""
    data = await state.get_data()
    user = await db.get_user(conn, user_id)
    balance = user.balance if user else 0
    price, discount, total = totals(data)

    block = ""
    if discount:
        block = texts.CONFIRM_DISCOUNT.format(
            full=fmt(price), code=data["promo"],
            percent=data["promo_percent"], saved=fmt(discount),
        )

    await state.set_state(Buy.confirm)
    await target.edit_text(
        texts.CONFIRM.format(
            title=title_of(data["product_type"], data["quantity"]),
            name=data.get("recipient_name") or f"@{data['recipient']}",
            recipient=data["recipient"],
            discount=block,
            price=fmt(total),
            rest=fmt(max(balance - total, 0)),
        ),
        reply_markup=keyboards.confirm(has_promo=bool(discount)),
    )


@router.callback_query(Buy.check_recipient, F.data == "order:recipient_ok")
async def cb_recipient_ok(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Получатель подтверждён — показываем итоговую сводку заказа."""
    await show_confirm(call.message, state, conn, call.from_user.id)
    await call.answer()


# ------------------------------------------------------------- промокод


@router.callback_query(Buy.confirm, F.data == "order:promo")
async def cb_order_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Buy.promo)
    await call.message.edit_text(
        texts.ORDER_PROMO_ASK,
        reply_markup=keyboards.back("order:promo_off", "‹ Назад к заказу"),
    )
    await call.answer()


@router.message(Buy.promo, F.text)
async def on_order_promo(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    promo = await db.check_discount(conn, message.text or "", message.from_user.id)
    if isinstance(promo, str):
        await message.answer(texts.PROMO_ERRORS.get(promo, "❌ Промокод не принят."))
        return

    await state.update_data(promo=promo["code"], promo_percent=promo["percent"])
    data = await state.get_data()
    _, discount, _ = totals(data)
    notice = await message.answer(texts.ORDER_PROMO_OK.format(
        code=promo["code"], percent=promo["percent"], saved=fmt(discount),
    ))
    await show_confirm(notice, state, conn, message.from_user.id)


@router.callback_query(StateFilter(Buy.confirm, Buy.promo),
                       F.data == "order:promo_off")
async def cb_order_promo_off(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    had = bool((await state.get_data()).get("promo_percent"))
    await state.update_data(promo=None, promo_percent=0)
    await show_confirm(call.message, state, conn, call.from_user.id)
    await call.answer("Промокод гирифта шуд" if had else "")


# ------------------------------------------------------------------ оплата


@router.callback_query(Buy.confirm, F.data == "order:go")
async def cb_pay(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider, bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()

    title = title_of(data["product_type"], data["quantity"])
    waiting = texts.PROCESSING if provider.instant else texts.PROCESSING_SLOW
    await call.message.edit_text(
        waiting.format(title=title, recipient=data["recipient"])
    )
    await call.answer()

    price, discount, total = totals(data)
    try:
        await delivery.purchase(
            bot, conn, provider,
            user_id=call.from_user.id,
            product_type=data["product_type"],
            quantity=data["quantity"],
            recipient=data["recipient"],
            price=total,
            promo=data.get("promo") if discount else None,
            discount=discount,
        )
    except delivery.NotEnoughFunds:
        user = await db.get_user(conn, call.from_user.id)
        balance = user.balance if user else 0
        await call.message.answer(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(total), balance=fmt(balance),
                missing=fmt(total - balance),
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        return

    # Итоговое сообщение (успех, возврат или «проверяю») отправляет
    # delivery-сервис — он единственный знает, чем всё кончилось.
    # Здесь только возвращаем пользователя в меню со свежим балансом.
    await call.message.answer(
        await menu_text(conn, call.from_user.id),
        reply_markup=await main_markup(conn),
    )


# ------------------------------------------------------------------ Steam


@router.callback_query(F.data == "m:steam")
async def cb_steam(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not runtime.steam_on():
        await call.answer("Бахш муваққатан баста аст.", show_alert=True)
        return
    await call.message.edit_text(
        texts.STEAM_ENTRY, reply_markup=keyboards.steam_menu()
    )
    await call.answer()


@router.callback_query(F.data.startswith("steam:"), F.data.regexp(r"^steam:\d+$"))
async def cb_steam_amount(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    amount = int(call.data.split(":")[1])
    if amount not in runtime.steam_packs():
        await call.answer("Ин маблағ дигар нест.", show_alert=True)
        await call.message.edit_text(
            texts.STEAM_ENTRY, reply_markup=keyboards.steam_menu()
        )
        return

    price = steam_cost(amount)
    user = await db.get_user(conn, call.from_user.id)
    balance = user.balance if user else 0
    if balance < price:
        await call.message.edit_text(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(price), balance=fmt(balance), missing=fmt(price - balance)
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        await call.answer()
        return

    await state.set_state(Steam.login)
    await state.update_data(product_type="steam", quantity=amount, price=price)
    await call.message.edit_text(
        texts.STEAM_ASK_LOGIN.format(
            amount=amount, currency=runtime.steam_currency(), price=fmt(price),
        ),
        reply_markup=keyboards.cancel(),
    )
    await call.answer()


@router.message(Steam.login, F.text)
async def on_steam_login(
    message: Message, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider,
) -> None:
    login = (message.text or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", login):
        await message.answer(texts.STEAM_BAD_LOGIN.format(login=login[:64] or "—"))
        return

    notice = await message.answer(texts.STEAM_CHECKING.format(login=login))
    await _confirm_steam(notice, state, conn, provider, login, message.from_user.id)


@router.callback_query(Steam.confirm, F.data == "steam:again")
async def cb_steam_again(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(Steam.login)
    await call.message.edit_text(
        texts.STEAM_ASK_LOGIN.format(
            amount=data.get("quantity", 0), currency=runtime.steam_currency(),
            price=fmt(data.get("price", 0)),
        ),
        reply_markup=keyboards.cancel(),
    )
    await call.answer()


async def _confirm_steam(
    target: Message, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider, login: str, user_id: int,
) -> None:
    """Проверить логин у сервиса и показать аккаунт на подтверждение.

    Без подтверждённого логина заказ не создаётся: деньги на чужой аккаунт
    Steam не вернуть, и предупреждение тут не спасёт.
    """
    account = await provider.check_steam_login(login)
    if account is None:
        await target.edit_text(texts.STEAM_NO_CHECK, reply_markup=keyboards.back())
        await state.clear()
        return
    if not account.exists:
        await target.edit_text(texts.STEAM_BAD_LOGIN.format(login=login))
        await state.set_state(Steam.login)
        return

    data = await state.get_data()
    user = await db.get_user(conn, user_id)
    balance = user.balance if user else 0
    price = data["price"]

    await state.update_data(recipient=login, recipient_name=account.display)
    await state.set_state(Steam.confirm)
    await target.edit_text(
        texts.STEAM_CONFIRM.format(
            name=account.display, login=login,
            amount=data["quantity"], currency=runtime.steam_currency(),
            price=fmt(price), rest=fmt(max(balance - price, 0)),
        ),
        reply_markup=keyboards.confirm_steam(),
    )


@router.callback_query(Steam.confirm, F.data == "steam:ok")
async def cb_steam_pay(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection,
    provider: DeliveryProvider, bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()

    await call.message.edit_text(
        texts.PROCESSING_SLOW.format(
            title=f"{data['quantity']} {runtime.steam_currency()} на Steam",
            recipient=data["recipient"],
        )
    )
    await call.answer()

    try:
        await delivery.purchase(
            bot, conn, provider,
            user_id=call.from_user.id,
            product_type="steam",
            quantity=data["quantity"],
            recipient=data["recipient"],
            price=data["price"],
        )
    except delivery.NotEnoughFunds:
        user = await db.get_user(conn, call.from_user.id)
        balance = user.balance if user else 0
        await call.message.answer(
            texts.STARS_NOT_ENOUGH.format(
                need=fmt(data["price"]), balance=fmt(balance),
                missing=fmt(data["price"] - balance),
            ),
            reply_markup=keyboards.deposit_methods(),
        )
        return

    await call.message.answer(
        await menu_text(conn, call.from_user.id),
        reply_markup=await main_markup(conn),
    )
