"""Главное меню, информация, калькулятор, топ клиентов."""
from __future__ import annotations

import aiosqlite
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, keyboards, texts
from app import runtime
from app.money import fmt4, affordable_stars, fmt, parse, stars_cost
from app.states import Calc

router = Router(name="menu")


async def menu_text(conn: aiosqlite.Connection, user_id: int) -> str:
    user = await db.get_user(conn, user_id)
    return texts.MENU.format(balance=fmt(user.balance if user else 0))


async def main_markup(conn: aiosqlite.Connection):
    """Клавиатура меню. Раздел игр показываем, только если игры открыты."""
    has_games = bool(runtime.get_bool("games_enabled")
                     and await db.list_games(conn, only_enabled=True))
    return keyboards.main_menu(games=has_games)


async def render_menu(target: Message | CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Показать меню. У Message берём отправителя, у CallbackQuery — нажавшего.

    Правка текста годится не для всякого сообщения: у фотографии текста
    нет, и Telegram такую правку отвергает. Для человека это выглядит
    мёртвой кнопкой — нажал «Отмена» под картинкой, и ничего. Поэтому
    сообщение без текста убираем и присылаем меню новым.
    """
    text = await menu_text(conn, target.from_user.id)
    markup = await main_markup(conn)

    # Различаем по тому, что у объекта есть, а не по его классу: у
    # нажатия кнопки внутри лежит сообщение, у обычного сообщения —
    # нет. Так же это работает и в проверках, где Telegram подставной.
    message = getattr(target, "message", None)
    if message is None:
        await target.answer(text, reply_markup=markup)
        return

    if getattr(message, "text", None) is None:
        try:
            await message.delete()
        except TelegramAPIError:
            pass          # удалить нельзя — не беда, меню всё равно придёт
        await message.answer(text, reply_markup=markup)
        return
    await message.edit_text(text, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await render_menu(message, conn)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await render_menu(message, conn)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await render_menu(message, conn)


@router.callback_query(F.data == "m:main")
async def cb_main(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await render_menu(call, conn)
    await call.answer()


@router.callback_query(F.data == "sub:check")
async def cb_sub_check(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """«Я подписался». Проверяем заново, не заглядывая в память.

    Память нужна, чтобы не ходить в Telegram на каждое нажатие, но
    здесь она вредна: человек только что подписался, и старый ответ
    «не подписан» держался бы ещё несколько минут.
    """
    from app.services import sponsor

    sponsor.forget(call.from_user.id)
    left = await sponsor.missing(call.bot, call.from_user.id)
    if left:
        await call.answer(texts.SPONSOR_NOT_YET, show_alert=True)
        return

    await call.answer(texts.SPONSOR_OK)
    await state.clear()
    await render_menu(call, conn)


@router.callback_query(F.data == "m:info")
async def cb_info(call: CallbackQuery) -> None:
    await call.message.edit_text(
        texts.INFO.format(support=texts.support()), reply_markup=keyboards.back()
    )
    await call.answer()


MEDALS = ["🥇", "🥈", "🥉"]


def top_basis(by: str) -> str:
    """По чему считается топ — подпись для клиента."""
    return "харидҳо" if by != "deposits" else "пуркуниҳо"


def top_basis_ru(by: str) -> str:
    """То же самое для админ-панели: она остаётся на русском."""
    return "покупок" if by != "deposits" else "пополнений"


def top_lines(clients: list[tuple]) -> str:
    lines = []
    for index, (client, amount) in enumerate(clients):
        badge = MEDALS[index] if index < len(MEDALS) else f"{index + 1}."
        name = f"@{client.username}" if client.username else (client.first_name or "Аноним")
        lines.append(f"{badge} {name} — <b>{fmt(amount)}</b>")
    return "\n".join(lines)


@router.callback_query(F.data == "m:top")
async def cb_top(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    by = runtime.get("top_by") or "purchases"
    clients = await db.top_clients(conn, limit=10, by=by)
    if not clients:
        await call.message.edit_text(texts.TOP_EMPTY, reply_markup=keyboards.back())
        await call.answer()
        return
    await call.message.edit_text(
        texts.TOP_CLIENTS.format(items=top_lines(clients), basis=top_basis(by)),
        reply_markup=keyboards.back(),
    )
    await call.answer()


@router.callback_query(F.data == "m:calc")
async def cb_calc(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Calc.query)
    await call.message.edit_text(
        texts.CALC_ASK.format(rate=fmt4(runtime.star_price_e4())),
        reply_markup=keyboards.cancel("‹ В меню"),
    )
    await call.answer()


@router.message(Calc.query, F.text)
async def on_calc(message: Message) -> None:
    raw = (message.text or "").strip().lower()

    # «100с» / «100 сомони» — считаем, сколько звёзд выйдет за эту сумму.
    money_part = raw.rstrip(".")
    for suffix in ("сомони", "смн", "с", "tjs"):
        if money_part.endswith(suffix):
            amount = parse(money_part[: -len(suffix)])
            if amount is not None and amount > 0:
                await message.answer(texts.CALC_MONEY.format(
                    money=fmt(amount), stars=affordable_stars(amount)
                ))
                return
            break

    # Голое число — считаем стоимость такого количества звёзд.
    if raw.isdigit() and int(raw) > 0:
        quantity = int(raw)
        await message.answer(texts.CALC_STARS.format(
            stars=quantity, price=fmt(stars_cost(quantity))
        ))
        return

    await message.answer(texts.CALC_BAD)
