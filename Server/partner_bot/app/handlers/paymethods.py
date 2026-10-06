"""Реквизиты для покупателей — пошаговый мастер в панели (бот из конструктора Donatix).

Добавление: банк → номер карты/телефона → владелец → проверка → сохранить.
Способов сколько угодно; каждый можно скрыть, поправить или удалить.
"""
from __future__ import annotations

from html import escape as esc

import aiosqlite
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.handlers import donatix
from app.services import access
from app.services import paymethods as pm

router = Router(name="paymethods")
router.message.filter(F.from_user.func(lambda u: access.is_admin(u.id)))
router.callback_query.filter(F.from_user.func(lambda u: access.is_admin(u.id)))


def _on(_event) -> bool:
    """Мастер работает у бота из конструктора; у самостоятельного — прежний экран."""
    return donatix.enabled()


class PayWizard(StatesGroup):
    bank = State()
    number = State()
    holder = State()
    edit_number = State()
    edit_holder = State()


def _kb(*rows) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return kb.as_markup()


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
        target = target.message
    await target.answer(text, reply_markup=kb)


def _card(m: dict) -> str:
    net = pm.crypto(m["bank"])
    if net:
        return (f"💎 <b>{esc(m['bank'])}</b> · сеть <b>{esc(net[1])}</b>\n"
                f"<code>{esc(m['number'])}</code>\n"
                f"⚠️ <i>{esc(pm.network_warning(m['bank']))}</i>\n")
    return (f"🏦 <b>{esc(m['bank'])}</b>\n"
            f"<code>{esc(m['number'])}</code>\n"
            + (f"👤 {esc(m['holder'])}\n" if m.get("holder") else ""))


# ── Список ───────────────────────────────────────────────────


@router.callback_query(F.data == "pn:pay", _on)
@router.callback_query(F.data == "pm:list", _on)
async def show_list(call: CallbackQuery, state: FSMContext, toast: str = "") -> None:
    await state.clear()
    methods = pm.all_methods()
    if methods:
        body = "\n".join(("✅ " if m.get("enabled", True) else "🚫 ") + f"<b>{esc(m['bank'])}</b> · "
                         f"{esc(pm.masked(m['number']))}" for m in methods)
    else:
        body = "<i>Пока ни одного — покупатели не смогут пополнить баланс.</i>"
    rows = [[(f"{'✅' if m.get('enabled', True) else '🚫'} {m['bank']} · {pm.masked(m['number'])}",
              f"pm:open:{m['id']}")] for m in methods]
    rows.insert(0, [("➕ Добавить способ оплаты", "pm:new")])
    rows.append([("‹ В панель", "pn:home")])
    await _show(call, "💳 <b>Реквизиты для покупателей</b>\n\n" + body +
                "\n\n<blockquote>Покупатель выбирает банк, вводит сумму и видит реквизиты "
                "этого банка. ✅ — показывается, 🚫 — скрыт.</blockquote>", _kb(*rows))
    await call.answer(toast)


# ── Мастер добавления ────────────────────────────────────────


@router.callback_query(F.data == "pm:new", _on)
async def step_bank(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PayWizard.bank)
    banks = [[(b, f"pm:bank:{i}") for i, b in enumerate(pm.BANKS[j:j + 2], start=j)]
             for j in range(0, len(pm.BANKS), 2)]
    await _show(call, "➕ <b>Новый способ оплаты</b> · шаг 1 из 3\n\n"
                      "Выберите банк или USDT-кошелёк, либо напишите название банка сообщением.",
                _kb(*banks, [("‹ Отмена", "pm:list")]))
    await call.answer()


async def _ask_number(target, state: FSMContext, bank: str) -> None:
    await state.update_data(bank=bank)
    await state.set_state(PayWizard.number)
    net = pm.crypto(bank)
    if net:
        await _show(target, f"💎 <b>{esc(bank)}</b> · шаг 2 из 2\n\n"
                            f"Отправьте <b>адрес кошелька в сети {esc(net[1])}</b>.\n"
                            f"<i>Адрес {net[3]}. Проверьте сеть: перевод в другой сети не дойдёт.</i>",
                    _kb([("‹ Назад", "pm:new"), ("Отмена", "pm:list")]))
        return
    await _show(target, f"➕ <b>{esc(bank)}</b> · шаг 2 из 3\n\n"
                        "Отправьте <b>номер карты</b> или <b>телефона</b>, на который переводить.\n"
                        "<i>Например: 5058 2700 1234 5678 или +992 900 00 00 00</i>",
                _kb([("‹ Назад", "pm:new"), ("Отмена", "pm:list")]))


@router.callback_query(PayWizard.bank, F.data.startswith("pm:bank:"))
async def picked_bank(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_number(call, state, pm.BANKS[int(call.data.split(":")[2])])
    await call.answer()


@router.message(PayWizard.bank, F.text)
async def typed_bank(message: Message, state: FSMContext) -> None:
    bank = pm.clean_bank(message.text)
    if not bank:
        await message.answer("Название банка — от 2 до 40 символов. Напишите ещё раз.")
        return
    await _ask_number(message, state, bank)


@router.message(PayWizard.number, F.text)
async def typed_number(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    net = pm.crypto(data.get("bank", ""))
    if net:
        wallet = pm.clean_wallet(data["bank"], message.text)
        if not wallet:
            await message.answer(f"❌ Это не адрес сети {esc(net[1])} — он {net[3]}. Отправьте ещё раз.")
            return
        await state.update_data(number=wallet, holder="")
        await state.set_state(PayWizard.holder)
        await message.answer("👀 <b>Проверьте — так увидит покупатель:</b>\n\n"
                             + _card({"bank": data["bank"], "number": wallet}),
                             reply_markup=_kb([("✅ Сохранить", "pm:save")], [("✏️ Заново", "pm:new"),
                                                                            ("Отмена", "pm:list")]))
        return
    number = pm.clean_number(message.text)
    if not number:
        await message.answer("❌ Не похоже на номер карты или телефона. Карта — 16 цифр, "
                             "телефон — с +992. Отправьте ещё раз.")
        return
    await state.update_data(number=number)
    await state.set_state(PayWizard.holder)
    data = await state.get_data()
    await message.answer(f"➕ <b>{esc(data['bank'])}</b> · <code>{esc(number)}</code> · шаг 3 из 3\n\n"
                         "Отправьте <b>имя владельца</b> — как в банке, например: <i>Алиджон М.</i>",
                         reply_markup=_kb([("‹ Отмена", "pm:list")]))


@router.message(PayWizard.holder, F.text)
async def typed_holder(message: Message, state: FSMContext) -> None:
    holder = pm.clean_holder(message.text)
    if not holder:
        await message.answer("Имя владельца — от 2 до 60 букв, без номеров. Отправьте ещё раз.")
        return
    await state.update_data(holder=holder)
    data = await state.get_data()
    await message.answer("👀 <b>Проверьте — так увидит покупатель:</b>\n\n" + _card(data),
                         reply_markup=_kb([("✅ Сохранить", "pm:save")], [("✏️ Заново", "pm:new"),
                                                                        ("Отмена", "pm:list")]))


@router.callback_query(PayWizard.holder, F.data == "pm:save")
async def save(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    data = await state.get_data()
    need = ("bank", "number") if pm.crypto(data.get("bank", "")) else ("bank", "number", "holder")
    if not all(data.get(k) for k in need):
        await call.answer("Начните заново.", show_alert=True)
        return
    await pm.add(conn, data["bank"], data["number"], data.get("holder", ""))
    await show_list(call, state, "Сохранено ✅")


# ── Карточка способа ─────────────────────────────────────────


@router.callback_query(F.data.startswith("pm:open:"), _on)
async def open_method(call: CallbackQuery, state: FSMContext) -> None:
    await _render_method(call, state, call.data.split(":")[2])


async def _render_method(call: CallbackQuery, state: FSMContext, mid: str) -> None:
    await state.clear()
    m = pm.get(mid)
    if m is None:
        await call.answer("Способ уже удалён.", show_alert=True)
        return
    on = m.get("enabled", True)
    await _show(call, _card(m) + ("\n✅ Показывается покупателям" if on else "\n🚫 Скрыт от покупателей"),
                _kb([("🚫 Скрыть" if on else "✅ Показывать", f"pm:toggle:{m['id']}")],
                    [("✏️ Адрес кошелька", f"pm:ednum:{m['id']}")] if pm.crypto(m["bank"]) else
                    [("✏️ Номер", f"pm:ednum:{m['id']}"), ("✏️ Владелец", f"pm:edhold:{m['id']}")],
                    [("🗑 Удалить", f"pm:del:{m['id']}")], [("‹ К реквизитам", "pm:list")]))
    await call.answer()


@router.callback_query(F.data.startswith("pm:toggle:"), _on)
async def toggle(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    mid = call.data.split(":")[2]
    m = pm.get(mid)
    if m:
        await pm.update(conn, mid, enabled=not m.get("enabled", True))
    await _render_method(call, state, mid)


@router.callback_query(F.data.startswith("pm:del:"), _on)
async def ask_delete(call: CallbackQuery) -> None:
    mid = call.data.split(":")[2]
    m = pm.get(mid)
    if m is None:
        await call.answer()
        return
    await _show(call, f"Удалить <b>{esc(m['bank'])}</b> · {esc(pm.masked(m['number']))}?",
                _kb([("🗑 Да, удалить", f"pm:delok:{mid}"), ("Нет", f"pm:open:{mid}")]))
    await call.answer()


@router.callback_query(F.data.startswith("pm:delok:"), _on)
async def delete(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await pm.delete(conn, call.data.split(":")[2])
    await show_list(call, state, "Удалено")


@router.callback_query(F.data.startswith("pm:ednum:"), _on)
async def edit_number(call: CallbackQuery, state: FSMContext) -> None:
    mid = call.data.split(":")[2]
    await state.set_state(PayWizard.edit_number)
    await state.update_data(edit_id=mid)
    net = pm.crypto((pm.get(mid) or {}).get("bank", ""))
    await _show(call, f"Отправьте новый адрес кошелька в сети {esc(net[1])}." if net else
                "Отправьте новый номер карты или телефона.", _kb([("‹ Отмена", "pm:list")]))
    await call.answer()


@router.message(PayWizard.edit_number, F.text)
async def edited_number(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    mid = (await state.get_data()).get("edit_id")
    m = pm.get(mid) or {}
    net = pm.crypto(m.get("bank", ""))
    number = pm.clean_wallet(m["bank"], message.text) if net else pm.clean_number(message.text)
    if not number:
        await message.answer(f"❌ Это не адрес сети {esc(net[1])} — он {net[3]}. Отправьте ещё раз." if net else
                             "❌ Не похоже на номер карты или телефона. Отправьте ещё раз.")
        return
    await pm.update(conn, mid, number=number)
    await state.clear()
    await message.answer("✅ Номер обновлён.", reply_markup=_kb([("‹ К реквизитам", "pm:list")]))


@router.callback_query(F.data.startswith("pm:edhold:"), _on)
async def edit_holder(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PayWizard.edit_holder)
    await state.update_data(edit_id=call.data.split(":")[2])
    await _show(call, "Отправьте новое имя владельца.", _kb([("‹ Отмена", "pm:list")]))
    await call.answer()


@router.message(PayWizard.edit_holder, F.text)
async def edited_holder(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    holder = pm.clean_holder(message.text)
    if not holder:
        await message.answer("Имя владельца — от 2 до 60 букв. Отправьте ещё раз.")
        return
    await pm.update(conn, (await state.get_data()).get("edit_id"), holder=holder)
    await state.clear()
    await message.answer("✅ Владелец обновлён.", reply_markup=_kb([("‹ К реквизитам", "pm:list")]))
