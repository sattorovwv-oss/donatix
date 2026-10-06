"""Админ-панель: одна точка входа, всё остальное — кнопками.

Открывается командой /panel (или /admin). Разделы:
  • Рассылка — любой тип сообщения + кнопки-ссылки
  • Цены — себестоимость, наценка, цена продажи, тарифы Premium
  • Реквизиты — карта, владелец, банк, город, примечание
  • Заявки, тикеты, пользователи, промокоды, статистика
  • Рекламные ссылки — Deep Links и статистика источников
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from contextlib import suppress
from html import escape as esc

from app import db, emoji, links, reports, runtime, texts
from app.handlers.menu import top_basis_ru
from app.config import settings
from app.services import access
from app.emoji import substitute
from app.keyboards import DANGER, PRIMARY, SUCCESS, btn, labeled
from app.money import (
    exact_stars_cost, fmt, fmt4, parse, parse4, round_price, steam_cost,
)
from app.services import dcpay, nicknames, pricing, rates
from app.services import reviews as reviews_service
from app.services import delivery
from app.states import GameNew, Panel, PartnerMove, PartnerNew, PromoNew

log = logging.getLogger(__name__)
router = Router(name="panel")

router.message.filter(F.from_user.func(lambda u: access.is_admin(u.id)))
router.callback_query.filter(F.from_user.func(lambda u: access.is_admin(u.id)))

# Аудитории рассылки: ключ -> (подпись, SQL-условие)
AUDIENCES = {
    "all": ("Всем", "is_banned = 0"),
    "buyers": ("Только покупателям", "is_banned = 0 AND id IN (SELECT user_id FROM orders)"),
    "funded": ("У кого есть баланс", "is_banned = 0 AND balance > 0"),
    "silent": ("Без покупок", "is_banned = 0 AND id NOT IN (SELECT user_id FROM orders)"),
}


# ============================================================ главный экран


def home_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    from app.handlers import donatix
    if donatix.enabled():
        # Бот из конструктора Donatix: только то, что нужно продавцу. Ключи, связь с
        # поставщиком, кошелёк и прочее ведёт сам Donatix.
        kb.row(btn("🏦 Счёт Donatix · пополнить", "dx:home", style=SUCCESS))
        kb.row(btn("🕹 Игры и пакеты", "pn:games", style=PRIMARY))
        kb.row(InlineKeyboardButton(text="💵 Цены и наценка", callback_data="pn:prices"))
        kb.row(
            InlineKeyboardButton(text="📥 Заявки клиентов", callback_data="pn:deposits"),
            InlineKeyboardButton(text="💳 Реквизиты", callback_data="pn:pay"),
        )
        kb.row(btn("👥 Мои клиенты · поиск и история", "pn:users", style=PRIMARY))
        kb.row(
            InlineKeyboardButton(text="📢 Обязательная подписка", callback_data="pn:sponsor"),
            InlineKeyboardButton(text="⭐️ Канал отзывов", callback_data="pn:reviews"),
        )
        from app import texts as _t
        contact = _t.support_username()
        kb.row(InlineKeyboardButton(text=f"📞 Мой контакт · @{contact}" if contact else "📞 Мой контакт — не задан",
                                    callback_data="pn:set:support_username"))
        return kb.as_markup()
    kb.row(btn("📣 Рассылка", "pn:cast", style=PRIMARY))
    kb.row(
        InlineKeyboardButton(text="💵 Цены и наценка", callback_data="pn:prices"),
        InlineKeyboardButton(text="💳 Реквизиты", callback_data="pn:pay"),
    )
    kb.row(
        InlineKeyboardButton(text="📥 Заявки", callback_data="pn:deposits"),
        InlineKeyboardButton(text="📞 Тикеты", callback_data="pn:tickets"),
    )
    kb.row(
        InlineKeyboardButton(text="📦 Заказы", callback_data="pn:orders"),
        InlineKeyboardButton(text="🎟 Промокоды", callback_data="pn:promos"),
    )
    kb.row(
        InlineKeyboardButton(text="📈 Отчёты", callback_data="pn:rep"),
        InlineKeyboardButton(text="🔀 Разделы", callback_data="pn:toggles"),
    )
    kb.row(
        InlineKeyboardButton(text="👥 Клиенты", callback_data="pn:users"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="pn:stats"),
    )
    kb.row(
        InlineKeyboardButton(text="💼 Кошелёк", callback_data="pn:wallet"),
        InlineKeyboardButton(text="🔌 Проверить связь", callback_data="pn:fragment"),
    )
    kb.row(
        InlineKeyboardButton(text="🎨 Оформление", callback_data="pn:look"),
        InlineKeyboardButton(text="📝 Объявление", callback_data="pn:notice"),
    )
    kb.row(
        InlineKeyboardButton(text="🔗 Рекламные ссылки", callback_data="pn:links"),
        InlineKeyboardButton(text="⭐️ Отзывы", callback_data="pn:reviews"),
    )
    kb.row(
        InlineKeyboardButton(text="📢 Подписка на канал", callback_data="pn:sponsor"),
    )
    kb.row(
        InlineKeyboardButton(text="🎮 Steam", callback_data="pn:steam"),
        InlineKeyboardButton(text="🤝 Партнёры", callback_data="pn:partners"),
    )
    kb.row(
        *([] if donatix.enabled() else [InlineKeyboardButton(text="🧩 API", callback_data="pn:api")]),
        InlineKeyboardButton(text="🏦 Оплаты банка", callback_data="pn:bank"),
    )
    kb.row(
        InlineKeyboardButton(text="🕹 Игры", callback_data="pn:games"),
        InlineKeyboardButton(text="💳 Балансы ключей", callback_data="pn:keys"),
    )
    kb.row(
        InlineKeyboardButton(text="👮 Доступ к панели", callback_data="pn:admins"),
        InlineKeyboardButton(text="⌨️ Все команды", callback_data="pn:help"),
    )
    return kb.as_markup()


def back_kb(target: str = "pn:home", text: str = "‹ Назад") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=target)
    return kb.as_markup()


async def home_text(conn: aiosqlite.Connection) -> str:
    data = await db.global_stats(conn)
    alerts = []
    if data["pending_deposits"]:
        alerts.append(f"📥 Пополнений ждут: <b>{data['pending_deposits']}</b>")
    if data["open_tickets"]:
        alerts.append(f"📞 Тикетов открыто: <b>{data['open_tickets']}</b>")
    if data["failed_orders"]:
        alerts.append(f"⚠️ Заказов зависло: <b>{data['failed_orders']}</b>")
    if not runtime.get("pay_card_number"):
        alerts.append("❗️ <b>Не заданы реквизиты</b> — деньги принять нельзя")
    if runtime.get_bool("autostopped"):
        alerts.append(
            "🛑 <b>Продажа выключена ботом</b> — выдача не проходит. "
            "Проверьте кошелёк."
        )
    elif runtime.get_int("fail_streak"):
        alerts.append(
            f"⚠️ Подряд не прошло заказов: <b>{runtime.get_int('fail_streak')}</b>"
        )

    block = ("\n".join(alerts) + "\n\n") if alerts else ""
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"{block}"
        f"👥 Пользователей: <b>{data['users']}</b>\n"
        f"💰 Пополнено всего: <b>{fmt(data['deposits'])}</b>\n"
        f"🛒 Продано на: <b>{fmt(data['revenue'])}</b> ({data['orders']} заказов)\n"
        f"👛 На балансах клиентов: <b>{fmt(data['held_balance'])}</b>\n\n"
        f"⭐️ Цена звезды: <b>{fmt4(runtime.star_price_e4())}</b>"
        + (f" · прибыль <b>{fmt4(runtime.profit_per_star_e4())}</b>"
           if runtime.star_cost_e4() > 0 else " · себестоимость не задана")
    )


async def show_home(target: Message | CallbackQuery, conn: aiosqlite.Connection) -> None:
    text = await home_text(conn)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, home_kb())
    else:
        await target.answer(text, reply_markup=home_kb())


async def safe_edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовать экран. Если содержимое не изменилось, Telegram ругается —
    это не ошибка, просто игнорируем."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError as exc:
        if "not modified" not in str(exc):
            await call.message.answer(text, reply_markup=markup)


@router.message(Command("panel", "admin"))
async def cmd_panel(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_home(message, conn)


@router.callback_query(F.data == "pn:home")
async def cb_home(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_home(call, conn)
    await call.answer()


@router.callback_query(F.data == "pn:help")
async def cb_help(call: CallbackQuery) -> None:
    await safe_edit(call, texts.ADMIN_HELP, back_kb())
    await call.answer()


# =================================================================== цены


def prices_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="💲 Себестоимость", callback_data="pn:set:star_cost_e4"),
        InlineKeyboardButton(text="📈 Наценка %", callback_data="pn:set:margin_percent"),
    )
    kb.row(InlineKeyboardButton(text="🏷 Цена продажи вручную",
                                callback_data="pn:set:star_price_e4"))
    kb.row(btn("📡 Узнать себестоимость", "pn:cost", style=PRIMARY))
    kb.row(btn("🧮 Применить наценку", "pn:recalc", style=SUCCESS))
    kb.row(InlineKeyboardButton(
        text=f"⭐️ Наборы звёзд ({len(runtime.star_packs())})",
        callback_data="pn:set:star_packs",
    ))
    kb.row(InlineKeyboardButton(
        text=f"🧿 Округление цен: {ROUND_TITLES[round_mode()]}",
        callback_data="pn:round",
    ))
    kb.row(btn("💱 Обновить курс сейчас", "pn:rate", style=PRIMARY))
    # Экран выше пишет, включены автоцены или нет, а кнопки к ним не было:
    # обработчик есть, служба обновления крутится, включить нечем.
    on = runtime.auto_price_on()
    kb.row(btn(("🟢 Автоцены: включены" if on else "⚪️ Автоцены: выключены"),
               "pn:autoprice", style=DANGER if on else SUCCESS))
    kb.row(
        InlineKeyboardButton(
            text=("🔄 Курс: авто" if runtime.get_bool("usd_auto") else "✋ Курс: вручную"),
            callback_data="pn:rate_auto",
        ),
        InlineKeyboardButton(text="➕ Надбавка к курсу",
                             callback_data="pn:set:usd_rate_spread"),
    )
    for plan in runtime.premium_plans():
        kb.row(InlineKeyboardButton(
            text=f"👑 Premium {plan['months']} мес — {fmt(plan['price'])}",
            callback_data=f"pn:premium:{plan['months']}",
        ))
    kb.row(
        InlineKeyboardButton(text="⬇️ Мин. звёзд", callback_data="pn:set:min_stars"),
        InlineKeyboardButton(text="⬆️ Макс. звёзд", callback_data="pn:set:max_stars"),
    )
    kb.row(
        InlineKeyboardButton(text="💵 Мин. пополнение", callback_data="pn:set:min_deposit_diram"),
        InlineKeyboardButton(text="👥 Реф. %", callback_data="pn:set:referral_percent"),
    )
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


def prices_text() -> str:
    cost, price = runtime.star_cost_e4(), runtime.star_price_e4()
    margin = runtime.margin_percent()

    if cost > 0:
        profit = price - cost
        real_margin = round((price - cost) / cost * 100) if cost else 0
        economics = (
            f"├ Себестоимость: <b>{fmt4(cost)}</b>\n"
            f"├ Наценка задана: <b>{margin}%</b>\n"
            f"├ Цена продажи: <b>{fmt4(price)}</b>\n"
            f"├ Фактическая наценка: <b>{real_margin}%</b>\n"
            f"└ Прибыль с 1 звезды: <b>{fmt4(profit)}</b>\n\n"
            f"💡 С заказа в 1000 звёзд заработок: "
            f"<b>{fmt((profit * 1000 + 50) // 100)}</b>"
        )
        if profit < 0:
            economics += "\n\n❗️ <b>Продаёте ниже себестоимости — это убыток.</b>"
    else:
        economics = (
            f"├ Цена продажи: <b>{fmt4(price)}</b>\n"
            f"└ Себестоимость: <b>не задана</b>\n\n"
            "💡 Укажите себестоимость — и панель будет показывать вашу прибыль "
            "с каждого заказа."
        )

    plans = "\n".join(
        f"├ {plan['months']} мес — <b>{fmt(plan['price'])}</b>"
        for plan in runtime.premium_plans()
    )
    return (
        "💵 <b>Цены и наценка</b>\n\n"
        f"⭐️ <b>Звёзды</b>\n{economics}\n\n"
        f"👑 <b>Premium</b>\n{plans}\n\n"
        + ("🟢 <b>Автоцены включены</b> — себестоимость и наценка "
           f"обновляются каждые {runtime.get_int('auto_price_every', 60)} мин.\n\n"
           if runtime.auto_price_on() else
           "⚪️ Автоцены выключены — цена держится, пока не поменяете вручную.\n\n")
        + f"📏 Заказ: от <b>{runtime.min_stars()}</b> до <b>{runtime.max_stars()}</b> звёзд\n"
        f"💵 Мин. пополнение: <b>{fmt(runtime.min_deposit())}</b>\n"
        f"👥 Реферальный процент: <b>{runtime.referral_percent()}%</b>\n"
        + rate_line()
    )


def rate_line() -> str:
    """Курс доллара: сколько, откуда и когда."""
    if runtime.usd_rate() <= 0:
        return "💱 Курс доллара не задан — бот не сможет сам узнать себестоимость"

    line = f"💱 Курс доллара: <b>{fmt(runtime.usd_rate())}</b>"
    spread = runtime.get_int("usd_rate_spread")
    if spread:
        line += f" (с надбавкой {spread}%)"
    source, when = runtime.get("usd_rate_source"), runtime.get("usd_rate_at")
    if source and when:
        line += f"\n└ {source}, обновлён {when[:16].replace('T', ' ')} UTC"
    elif runtime.get_bool("usd_auto"):
        line += "\n└ автообновление включено, но курс ещё не приходил"
    return line


ROUND_TITLES = {
    "off": "без округления",
    "up1": "вверх до сомони",
    "near1": "до сомони",
    "up5": "вверх до 5 сомони",
}


def round_mode() -> str:
    mode = runtime.get("round_prices") or "off"
    return mode if mode in ROUND_TITLES else "off"


def round_text() -> str:
    """Экран выбора с живым примером на настоящей цене звезды."""
    current, exact = round_mode(), exact_stars_cost(100)
    lines = [
        f"{'🔘' if mode == current else '⚪️'} <b>{title}</b> — "
        f"100 звёзд за {fmt(round_price(exact, mode))}"
        for mode, title in ROUND_TITLES.items()
    ]
    return (
        "🧿 <b>Округление цен</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + "\n".join(lines)
        + "\n\n<blockquote>Округление всегда считается от точной цены, "
          "поэтому при пересчёте она не ползёт вверх. Тарифы Premium это "
          "не трогает — там суммы вы задаёте сами.</blockquote>"
    )


def round_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    current = round_mode()
    for mode, title in ROUND_TITLES.items():
        kb.row(btn(("✅ " if mode == current else "") + title.capitalize(),
                   f"pn:round:{mode}", style=SUCCESS if mode == current else None))
    kb.row(InlineKeyboardButton(text="‹ К ценам", callback_data="pn:prices"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:round")
async def cb_round(call: CallbackQuery) -> None:
    await safe_edit(call, round_text(), round_kb())
    await call.answer()


@router.callback_query(F.data.startswith("pn:round:"))
async def cb_round_set(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    mode = call.data.rsplit(":", 1)[1]
    if mode not in ROUND_TITLES:
        await call.answer("Такого способа нет.", show_alert=True)
        return
    await runtime.set_value(conn, "round_prices", mode)
    await call.answer(ROUND_TITLES[mode].capitalize())
    await safe_edit(call, round_text(), round_kb())


@router.callback_query(F.data == "pn:rate")
async def cb_rate(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Спросить курс у бесплатных источников прямо сейчас."""
    await safe_edit(call, "💱 Спрашиваю курс…", back_kb("pn:prices", "‹ К ценам"))
    await call.answer()

    old = runtime.usd_rate()
    try:
        rate = await pricing.refresh_rate(conn)
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        log.info("Курс не пришёл: %s", exc)
        await safe_edit(
            call,
            "💱 <b>Курс не пришёл</b>\n\n"
            f"<blockquote expandable>{exc}</blockquote>\n\n"
            "Задайте курс вручную — бот продолжит работать по нему.",
            back_kb("pn:set:usd_rate_diram", "💱 Задать вручную"),
        )
        return

    spread = runtime.get_int("usd_rate_spread")
    change = ""
    if old > 0 and old != rate.diram:
        arrow = "📈" if rate.diram > old else "📉"
        change = f"\n\n{arrow} Было <b>{fmt(old)}</b> → стало <b>{fmt(rate.diram)}</b>"

    await safe_edit(
        call,
        "💱 <b>Курс обновлён</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Источник: <b>{rate.source}</b>\n"
        f"├ Биржевой курс: <b>{rate.value}</b> сомони за доллар\n"
        + (f"├ Надбавка: <b>{spread}%</b>\n" if spread else "")
        + f"└ В работе: <b>{fmt(rate.diram)}</b> за доллар"
        + change
        + "\n\n<blockquote>Проверьте по обменнику. Если ваш доллар дороже — "
          "поставьте надбавку к курсу.</blockquote>",
        back_kb("pn:prices", "‹ К ценам"),
    )


@router.callback_query(F.data == "pn:rate_auto")
async def cb_rate_auto(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    on = runtime.get_bool("usd_auto")
    await runtime.set_value(conn, "usd_auto", "0" if on else "1")
    await call.answer("Курс вручную" if on else "Курс будет обновляться сам")
    await safe_edit(call, prices_text(), prices_kb())


@router.callback_query(F.data == "pn:prices")
async def cb_prices(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call, prices_text(), prices_kb())
    await call.answer()


@router.callback_query(F.data == "pn:recalc")
async def cb_recalc(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    if runtime.star_cost_e4() <= 0:
        await call.answer("Сначала задайте себестоимость.", show_alert=True)
        return
    new_price = runtime.price_from_margin_e4()
    await runtime.set_value(conn, "star_price_e4", str(new_price))
    await call.answer(f"Цена продажи: {fmt4(new_price)}")
    await safe_edit(call, prices_text(), prices_kb())


@router.callback_query(F.data.startswith("pn:premium:"))
async def cb_premium_edit(call: CallbackQuery, state: FSMContext) -> None:
    months = int(call.data.rsplit(":", 1)[1])
    plan = runtime.find_premium(months)
    if plan is None:
        await call.answer("Тариф не найден.", show_alert=True)
        return
    await state.set_state(Panel.value)
    await state.update_data(field=f"premium:{months}")
    await safe_edit(
        call,
        f"👑 <b>Premium {months} мес.</b>\n\n"
        f"Сейчас: <b>{fmt(plan['price'])}</b>\n\n"
        f"Введите новую цену в сомони (например <code>175</code> или <code>175.50</code>):",
        back_kb("pn:prices"),
    )
    await call.answer()


# ============================================================== реквизиты


PAY_FIELDS = {
    "pay_card_number": ("💳 Номер карты", "Введите номер карты:"),
    "dc_account": ("🏙 Счёт «Душанбе Сити»",
                   "По умолчанию берётся карта из реквизитов. Заполняйте, "
                   "только если деньги приходят на другой счёт "
                   "(параметр <code>a</code> из ссылки pay.dc.tj):"),
    "dc_comment": ("📝 Подпись в платеже",
                   "Что писать в комментарии к переводу перед кодом платежа — "
                   "например <code>@uwayscoder</code>:"),
    "dc_service": ("🔢 Код услуги", "Параметр <code>f1</code> из ссылки. Обычно 133:"),
    "pay_card_holder": ("👤 Владелец", "Введите имя владельца карты:"),
    "pay_card_bank": ("🏦 Банк", "Введите название банка:"),
    "pay_city": ("🏙 Город", "Введите город:"),
    "pay_extra": ("📝 Примечание", "Введите примечание под реквизитами "
                                   "(или <code>-</code>, чтобы убрать):"),
    "ru_pay_number": ("🇷🇺 Номер для России",
                      "Номер, на который переводят из Сбербанка и Тинькофф "
                      "через «Душанбе Сити» — например "
                      "<code>+992000000000</code>.\n\n"
                      "Пусто — способ «Из России» клиентам не показывается:"),
}


def pay_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (label, _) in PAY_FIELDS.items():
        kb.row(InlineKeyboardButton(text=label, callback_data=f"pn:set:{key}"))
    kb.row(btn(("🖼 Пример чека: есть" if runtime.get("ru_example_photo")
                else "🖼 Пример чека из России"), "pn:ru_photo"))
    kb.row(btn("👁 Как видит клиент", "pn:preview_pay", style=PRIMARY))
    kb.row(btn("🏙 Проверить кнопку оплаты", "pn:dctest"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


def pay_text() -> str:
    def show(key: str) -> str:
        value = runtime.get(key)
        return f"<b>{value}</b>" if value else "<i>не задано</i>"

    warning = ""
    if not runtime.get("pay_card_number"):
        warning = "\n❗️ <b>Без номера карты клиенты не смогут пополнить баланс.</b>\n"

    dc_state = "✅ <b>работает</b>" if dcpay.is_ready() else "⚪️ <i>не настроена</i>"
    dc_source = (
        "<i>та же, что в реквизитах</i>" if not runtime.get("dc_account")
        else f"<b>{runtime.get('dc_account')}</b>"
    )
    return (
        "💳 <b>Реквизиты для приёма оплаты</b>\n"
        f"{warning}\n"
        f"├ Карта: {show('pay_card_number')}\n"
        f"├ Владелец: {show('pay_card_holder')}\n"
        f"├ Банк: {show('pay_card_bank')}\n"
        f"├ Город: {show('pay_city')}\n"
        f"└ Примечание: {show('pay_extra')}\n\n"
        f"🏙 <b>Кнопка «Душанбе Сити»</b> — {dc_state}\n"
        f"├ Счёт: {dc_source}\n"
        f"├ Подпись: <b>{dcpay.comment_prefix() or '—'}</b>\n"
        f"└ Код услуги: {show('dc_service')}\n\n"
        "<blockquote>Кнопка открывает приложение с уже вписанными счётом "
        "и суммой — покупателю не нужно переписывать их вручную, а значит "
        "негде ошибиться.</blockquote>"
    )


@router.callback_query(F.data == "pn:pay")
async def cb_pay(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call, pay_text(), pay_kb())
    await call.answer()


@router.callback_query(F.data == "pn:preview_pay")
async def cb_preview_pay(call: CallbackQuery) -> None:
    holder = runtime.get("pay_card_holder")
    bank = runtime.get("pay_card_bank")
    note = runtime.get("pay_extra")
    # Собираем ровно тем же шаблоном и теми же полями, что и настоящий
    # экран пополнения. Иначе предпросмотр показывает не то, что клиент,
    # а при смене шаблона просто падает — так и было.
    where = " · ".join(part for part in (bank, runtime.get("pay_city")) if part)
    preview = texts.DEPOSIT_REQUISITES.format(
        amount=fmt(10007),
        card=runtime.get("pay_card_number") or "— реквизиты не заданы —",
        holder=f"👤 <b>{holder}</b>\n" if holder else "",
        bank=f"🏦 {where}\n" if where else "",
        extra=f"\n{note}\n" if note else "\n",
        dc_block=texts.DEPOSIT_DC_BLOCK.format(reference="TOP1234"),
    )
    await safe_edit(
        call, "👁 <b>Так это видит клиент:</b>\n\n" + preview, back_kb("pn:pay")
    )
    await call.answer()


# ==================================================== универсальный ввод


# Поле -> (заголовок, подсказка, тип). Тип: money | int | percent | text
FIELDS: dict[str, tuple[str, str, str]] = {
    "star_cost_e4": ("💲 Себестоимость звезды",
                        "Сколько ОДНА звезда стоит вам на Fragment, в сомони.\n"
                        "Например <code>0.1416</code>:", "price4"),
    "star_price_e4": ("🏷 Цена продажи звезды",
                         "За сколько продаёте ОДНУ звезду, в сомони.\n"
                         "Например <code>0.1629</code>:", "price4"),
    "usd_rate_diram": ("💱 Курс доллара",
                       "Сколько сомони стоит 1 доллар. Например <code>10.90</code>:", "money"),
    "usd_rate_spread": ("➕ Надбавка к курсу",
                        "На сколько процентов курс в обменнике выше биржевого.\n"
                        "Источники дают биржевой курс, а доллары вы покупаете "
                        "дороже — эта надбавка выравнивает разницу.\n"
                        "Например <code>3</code>. Не уверены — оставьте 0:",
                        "percent"),
    "margin_percent": ("📈 Наценка",
                       "Процент наценки к себестоимости. Например <code>30</code>:", "percent"),
    "api_margin": ("💰 Наценка для разработчиков",
                   "Процент к себестоимости для покупок через API.\n"
                   "Например <code>8</code>: разработчик платит закупку "
                   "плюс 8%.\n\n"
                   "Он перепродаёт наш товар и на витринной цене бота "
                   "заработать не сможет — потому цена у него своя.\n\n"
                   "Пришлите <code>0</code>, чтобы продавать ему по ценам "
                   "витрины:", "percent"),
    "sponsor_channel": ("📢 Канал обязательной подписки",
                        "Пришлите <code>@kanal</code>. Несколько — через "
                        "пробел.\n\n"
                        "Бот должен быть в канале администратором, иначе "
                        "он не сможет проверить подписку.\n\n"
                        "<code>-</code> — убрать проверку:", "text"),
    "reviews_channel": ("📣 Канал отзывов",
                        "Куда публиковать одобренные отзывы: <code>@kanal</code> "
                        "или числовой ID.\n\n"
                        "Бот должен быть в канале администратором с правом "
                        "публиковать. Пришлите <code>-</code>, чтобы убрать:",
                        "text"),
    "supplier_rate_per_min": ("⏱ Запросов к поставщику в минуту",
                             "Сколько запросов в минуту разрешает поставщик "
                             "на один ключ.\n\n"
                             "Ставьте <b>чуть ниже</b> объявленного: в лимит "
                             "идут и заказы, и проверки статуса, и каталог, "
                             "а часы у поставщика с нашими не совпадают до "
                             "секунды.\n\n"
                             "При 60 разрешённых ставьте <code>55</code>:",
                             "int"),
    "fazer_games_key": ("🔑 Ключ поставщика для игр",
                        "Ключ того, чей счёт тратится на игры. Оставьте "
                        "пустым — игры пойдут с основного счёта.\n"
                        "<code>-</code> — вернуть на основной:", "text"),
    "gameskinbo_key": ("🔑 Ключ для ников Free Fire",
                       "Ключ с gameskinbo.com — бот показывает ник игрока "
                       "по его ID. Без ключа работает запасной источник, "
                       "но медленнее. <code>-</code> — убрать:", "text"),
    "steam_price_e4": ("🎮 Цена единицы Steam",
                       "За сколько сомони продаёте 1 единицу валюты Steam.\n"
                       "Например <code>0.14</code> за рубль:", "price4"),
    "steam_cost_e4": ("🎮 Себестоимость Steam",
                      "Во сколько 1 единица обходится вам, в сомони.\n"
                      "Например <code>0.12</code>:", "price4"),
    "steam_currency": ("🎮 Валюта Steam",
                       "Валюта кошелька: <code>RUB</code>, <code>KZT</code>, "
                       "<code>USD</code>:", "text"),
    "steam_packs": ("🎮 Суммы Steam",
                    "Через запятую — какие суммы показывать кнопками.\n"
                    "Например <code>100, 250, 500, 1000</code>:", "packs"),
    "star_packs": ("⭐️ Наборы звёзд",
                   "Через запятую — какие кнопки показывать в магазине.\n"
                   "Например <code>50, 100, 250, 500, 1000, 2500</code>\n\n"
                   "Цена каждой кнопки считается сама по текущей цене звезды.",
                   "packs"),
    "min_stars": ("⬇️ Минимум звёзд", "Минимум звёзд в одном заказе:", "int"),
    "max_stars": ("⬆️ Максимум звёзд", "Максимум звёзд в одном заказе:", "int"),
    "min_deposit_diram": ("💵 Минимальное пополнение",
                          "Минимальная сумма пополнения в сомони:", "money"),
    "referral_percent": ("👥 Реферальный процент",
                         "Сколько процентов получает пригласивший "
                         "с каждого пополнения:", "percent"),
    "autostop_after": ("🔢 Порог автостопа",
                       "После скольких неудачных заказов подряд бот гасит "
                       "продажу. Обычно 3:", "int"),
    "fazer_webhook_secret": ("🔔 Секрет вебхука",
                            "Секрет <code>whsec_…</code> из кабинета "
                            "поставщика — им подписан каждый отчёт:", "text"),
    "webhook_port": ("🔌 Порт вебхука",
                     "На каком порту слушать отчёты поставщика "
                     "(например 8081). Ноль — выключить:", "int"),
    "webhook_public_url": ("🌐 Адрес бота",
                           "Публичный адрес бота, например "
                           "<code>https://bot.example.com</code>:", "text"),
    "games_timeout_min": ("⏱ Ожидание выдачи (игры)",
                          "Через сколько минут без ответа поставщика предупредить вас. "
                          "Деньги клиенту бот сам не возвращает — только если поставщик "
                          "отменил заказ. Обычно 20:", "int"),
    "support_username": ("📞 Мой контакт для покупателей",
                         "Ваш Telegram, куда покупатели пишут при проблемах.\n"
                         "Пришлите <code>@username</code> (или ссылку t.me/username):", "username"),
    "support_notice": ("📝 Объявление в поддержке",
                       "Текст, который увидят клиенты в разделе «Поддержка» "
                       "(или <code>-</code>, чтобы убрать):", "text"),
}
FIELDS.update({
    key: (label, prompt, "text") for key, (label, prompt) in PAY_FIELDS.items()
})

# Куда возвращаться после сохранения
FIELD_PARENT = {key: "pn:pay" for key in PAY_FIELDS}
FIELDS["volsever_key"] = (
    "🔑 Ключ проверки ID (Volsever)",
    "Ключ <code>pk_live_…</code> из кабинета volsever.com "
    "(или <code>-</code>, чтобы убрать). Им проверяется ID игрока "
    "и показывается его ник перед покупкой:", "text",
)

FIELDS["ff_community_key"] = (
    "🔑 Второй справочник ников",
    "Ключ с <b>developers.freefirecommunity.com</b> "
    "(или <code>-</code>, чтобы убрать). Справочник работает и без ключа — "
    "ключ снимает ограничения:", "text",
)

FIELD_PARENT.update({
    "star_cost_e4": "pn:prices", "star_price_e4": "pn:prices",
    "margin_percent": "pn:prices", "min_stars": "pn:prices",
    "star_packs": "pn:prices", "reviews_channel": "pn:reviews",
    "sponsor_channel": "pn:sponsor",
    "gameskinbo_key": "pn:games", "fazer_games_key": "pn:games",
    "supplier_rate_per_min": "pn:keys",
    "ff_community_key": "pn:games", "volsever_key": "pn:checker",
    "steam_price_e4": "pn:steam", "steam_cost_e4": "pn:steam",
    "steam_currency": "pn:steam", "steam_packs": "pn:steam",
    "usd_rate_diram": "pn:prices", "usd_rate_spread": "pn:prices",
    "max_stars": "pn:prices", "min_deposit_diram": "pn:prices",
    "referral_percent": "pn:prices", "support_notice": "pn:home", "support_username": "pn:home",
    "autostop_after": "pn:wallet",
    "games_timeout_min": "pn:games",
    "api_margin": "pn:api",
    "fazer_webhook_secret": "pn:hook", "webhook_port": "pn:hook",
    "webhook_public_url": "pn:hook",
})


def current_display(key: str) -> str:
    kind = FIELDS[key][2]
    raw = runtime.get(key)
    if kind == "price4":
        return fmt4(runtime.get_int(key))
    if kind == "money":
        return fmt(runtime.get_int(key))
    if kind == "percent":
        return f"{raw or 0}%"
    if kind == "packs":
        packs = runtime.star_packs()
        return ", ".join(str(quantity) for quantity in packs) or "не заданы"
    return raw or "не задано"


async def start_field_edit(call: CallbackQuery, state: FSMContext, key: str) -> bool:
    """Показать экран ввода значения. False — поля не существует."""
    if key not in FIELDS:
        return False
    label, prompt, _ = FIELDS[key]
    await state.set_state(Panel.value)
    await state.update_data(field=key)
    await safe_edit(
        call,
        f"{label}\n\nСейчас: <b>{current_display(key)}</b>\n\n{prompt}",
        back_kb(FIELD_PARENT.get(key, "pn:home"), "❌ Отмена"),
    )
    return True


@router.callback_query(F.data.startswith("pn:set:"))
async def cb_set_field(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.rsplit(":", 1)[1]
    if not await start_field_edit(call, state, key):
        await call.answer("Неизвестное поле.", show_alert=True)
        return
    await call.answer()


@router.message(Panel.value, F.text)
async def on_field_value(
    message: Message, state: FSMContext, conn: aiosqlite.Connection,
    provider=None,
) -> None:
    data = await state.get_data()
    field = data.get("field", "")
    raw = (message.text or "").strip()

    # Своя цена пакета: ключ вида game_price:игра:пакет.
    if field.startswith("game_price:"):
        _, code, offer_id = field.split(":", 2)
        amount = parse(raw)
        if amount is None or amount <= 0:
            await message.answer(
                "❌ Введите цену числом, например <code>25</code> "
                "или <code>24.50</code>."
            )
            return
        await db.set_game_price(conn, code, offer_id, amount)
        await state.clear()
        game = await db.get_game(conn, code)
        await message.answer(
            f"✅ <b>{game.title if game else code}</b> — пакет стоит "
            f"<b>{fmt(amount)}</b>\n\n"
            "<i>Эта цена держится сама по себе: смена курса и наценки её "
            "не трогает.</i>",
            reply_markup=back_kb(f"pn:game_packs:{code}", "‹ К пакетам"),
        )
        return

    # Прайс целиком: разбираем и показываем, что получилось.
    if field.startswith("price_list:"):
        from app.handlers.games import offers_of
        from app.services import pricelist, suppliers

        code = field.split(":", 1)[1]
        game = await db.get_game(conn, code)
        if game is None:
            await state.clear()
            await message.answer("❌ Игра не найдена.")
            return

        try:
            offers = await offers_of(suppliers.for_games(provider), game,
                                     conn, for_owner=True)
        except Exception as exc:  # noqa: BLE001 — показать владельцу поломку
            await state.clear()
            await message.answer(
                f"📦 <b>Пакеты {game.title} не пришли</b>\n\n"
                f"<blockquote expandable>{esc(str(exc))}</blockquote>",
                reply_markup=back_kb(f"pn:game:{code}", "‹ К игре"),
            )
            return

        plan = pricelist.build(raw, offers)
        if not plan.matched:
            await message.answer(
                "❌ <b>Ни одна строка не легла на пакет</b>\n\n"
                "<blockquote>Проверьте, что в строке есть название и цена: "
                "<code>60 UC - 10</code>.\n\nЕсли названия у поставщика "
                "другие — сначала переименуйте пакет, а потом присылайте "
                "прайс.</blockquote>",
                reply_markup=back_kb(f"pn:game_packs:{code}", "‹ К пакетам"),
            )
            return

        await state.clear()
        _plans[code] = plan
        kb = InlineKeyboardBuilder()
        kb.row(btn(f"✅ Поставить цены ({len(plan.changed)})",
                   f"pn:price_go:{code}", style=SUCCESS))
        if any(r.name != r.offer["name"] for r in plan.matched):
            kb.row(btn("🏷 Цены и названия из прайса",
                       f"pn:price_name:{code}"))
        kb.row(btn("❌ Отмена", f"pn:game_packs:{code}", style=DANGER))
        await message.answer(_price_preview(game, plan), reply_markup=kb.as_markup())
        return

    # Своё название игры — так её увидят покупатели в меню.
    if field.startswith("game_title:"):
        code = field.split(":", 1)[1]
        title = raw.strip()[:48]
        if len(title) < 2:
            await message.answer("Название — хотя бы две буквы. Пришлите ещё раз.")
            return
        await db.update_game(conn, code, title=title)
        await db.load_game_titles(conn)
        await state.clear()
        fresh = await db.get_game(conn, code)
        await message.answer(f"✅ Теперь игра называется <b>{title}</b>",
                             reply_markup=game_kb(fresh) if fresh else back_kb("pn:games", "‹ К играм"))
        return

    # Своё название пакета: у поставщика они часто безликие.
    if field.startswith("pack_name:"):
        _, code, offer_id = field.split(":", 2)
        title = "" if raw == "-" else raw.strip()[:64]
        await db.set_game_offer_title(conn, code, offer_id, title)
        await state.clear()
        game = await db.get_game(conn, code)
        await message.answer(
            f"✅ <b>{game.title if game else code}</b> — пакет "
            + (f"теперь называется <b>{title}</b>" if title
               else "снова называется как у поставщика"),
            reply_markup=back_kb(f"pn:game_packs:{code}", "‹ К пакетам"),
        )
        return

    # Премиум-значок игры: хранится прямым ID, а не ключом.
    if field.startswith("game_emoji:"):
        code = field.split(":", 1)[1]
        value = "" if raw == "-" else raw.strip()
        if value and not re.fullmatch(r"\d{5,25}", value):
            await message.answer(
                "❌ Нужен <b>числовой ID</b> премиум-эмодзи.\n\n"
                "<blockquote>Перешлите сам эмодзи боту @idstickerbot — "
                "он пришлёт ID.</blockquote>"
            )
            return
        await db.update_game(conn, code, emoji=value)
        await state.clear()
        game = await db.get_game(conn, code)
        await message.answer(
            f"✅ <b>{game.title if game else code}</b> — значок "
            + (f"<code>{value}</code>" if value else "<i>убран</i>"),
            reply_markup=back_kb(f"pn:game:{code}", "‹ К игре"),
        )
        return

    # Код игры у сервиса проверки ID.
    if field.startswith("game_checker:"):
        code = field.split(":", 1)[1]
        value = "" if raw == "-" else raw.strip().lower()
        if value and not re.fullmatch(r"[a-z0-9_\-]{2,48}", value):
            await message.answer(
                "❌ Код — латиница, цифры, дефис и подчёркивание, "
                "например <code>free-fire-asia</code>."
            )
            return
        await db.update_game(conn, code, checker=value)
        await state.clear()
        game = await db.get_game(conn, code)
        await message.answer(
            f"✅ <b>{game.title if game else code}</b> — проверка ID "
            + (f"<code>{value}</code>" if value else "<i>выключена</i>"),
            reply_markup=back_kb("pn:checker", "‹ К проверке"),
        )
        return

    # Имя поля с ID игрока — тоже из таблицы игр.
    if field.startswith("game_field:"):
        from app.services import games as gsvc

        code = field.split(":", 1)[1]
        # Полей может быть несколько: у Magic Chess и Mobile Legends
        # аккаунт задаётся парой «ID игрока + номер сервера».
        names = [part.strip().lower()
                 for part in re.split(r"[,\s;]+", raw) if part.strip()]
        if not names or not all(
            re.fullmatch(r"[a-z0-9_]{2,32}", name) for name in names
        ):
            await message.answer(
                "❌ Имя поля — латиница, цифры и подчёркивание, например\n"
                "<code>player_id</code> — одно поле\n"
                "<code>player_id, server_id</code> — два поля"
            )
            return
        value = ",".join(names)
        await db.update_game(conn, code, field=value)
        await state.clear()
        game = await db.get_game(conn, code)
        asked = ", ".join(gsvc.field_label(name) for name in names)
        await message.answer(
            f"✅ <b>{game.title if game else code}</b> — поля "
            f"<code>{value}</code>\n\n"
            f"<i>Бот спросит у клиента: {asked}.</i>",
            reply_markup=back_kb(f"pn:game:{code}", "‹ К игре"),
        )
        return

    # Наценка конкретной игры — своя ветка: она лежит в таблице игр.
    if field.startswith("game_margin:"):
        code = field.split(":", 1)[1]
        if not raw.isdigit() or int(raw) > 500:
            await message.answer("❌ Введите процент числом, например <code>20</code>.")
            return
        await db.update_game(conn, code, margin=int(raw))
        await state.clear()
        game = await db.get_game(conn, code)
        await message.answer(
            f"✅ Наценка <b>{game.title if game else code}</b> — <b>{raw}%</b>"
            + ("" if int(raw) else " <i>(берётся общая)</i>"),
            reply_markup=back_kb(f"pn:game:{code}", "‹ К игре"),
        )
        return

    # Тариф Premium — отдельная ветка, он живёт в JSON-списке.
    if field.startswith("premium:"):
        months = int(field.split(":")[1])
        amount = parse(raw)
        if amount is None or amount <= 0:
            await message.answer("❌ Введите сумму числом, например <code>175</code>.")
            return
        plans = runtime.premium_plans()
        for plan in plans:
            if plan["months"] == months:
                plan["price"] = amount
        await runtime.save_premium_plans(conn, plans)
        await state.clear()
        await message.answer(
            f"✅ Premium {months} мес. теперь <b>{fmt(amount)}</b>",
            reply_markup=back_kb("pn:prices", "‹ К ценам"),
        )
        return

    if field not in FIELDS:
        await state.clear()
        await message.answer("Не понял, что менять. Откройте /panel заново.")
        return

    kind = FIELDS[field][2]
    note = ""     # приписка к ответу: что-то приняли не целиком
    if kind == "username":
        name = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", raw.strip()).split("?")[0].strip("/")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
            await message.answer("❌ Пришлите юзернейм Telegram, например <code>@my_support</code> "
                                 "(5–32 символа: латиница, цифры, _).")
            return
        await runtime.set_value(conn, field, name)
        await state.clear()
        await message.answer(f"✅ Контакт для покупателей: <b>@{name}</b>\n"
                             "Его увидят в разделе «Поддержка» и в сообщениях о проблемах с заказом.",
                             reply_markup=back_kb("pn:home", "‹ В панель"))
        return
    if kind == "price4":
        amount = parse4(raw)
        if amount is None or amount < 0:
            await message.answer(
                "❌ Введите цену числом, например <code>0.1629</code>. "
                "Можно до четырёх знаков после точки."
            )
            return
        value, shown = str(amount), fmt4(amount)
    elif kind == "money":
        amount = parse(raw)
        if amount is None or amount < 0:
            await message.answer("❌ Введите сумму числом, например <code>0.25</code> или <code>150</code>.")
            return
        value = str(amount)
        shown = fmt(amount)
    elif kind in ("int", "percent"):
        if not raw.isdigit():
            await message.answer("❌ Введите целое число.")
            return
        number = int(raw)
        if kind == "percent" and number > 100:
            await message.answer("❌ Процент не может быть больше 100.")
            return
        if kind == "int" and number <= 0:
            await message.answer("❌ Число должно быть больше нуля.")
            return
        value = str(number)
        shown = f"{number}%" if kind == "percent" else str(number)
    elif kind == "packs":
        # Разделителем считаем что угодно, кроме цифр: и запятые, и пробелы.
        packs = sorted({int(chunk) for chunk in re.split(r"\D+", raw) if chunk})
        if not packs:
            await message.answer(
                "❌ Пришлите количества через запятую, например "
                "<code>50, 100, 250, 500</code>."
            )
            return
        skipped = [q for q in packs
                   if not runtime.min_stars() <= q <= runtime.max_stars()]
        packs = [q for q in packs if q not in skipped]
        if not packs:
            await message.answer(
                f"❌ Все числа вне лимитов заказа "
                f"({runtime.min_stars()}–{runtime.max_stars()} звёзд)."
            )
            return
        value = ",".join(str(q) for q in packs)
        shown = ", ".join(str(q) for q in packs)
        if skipped:
            note = ("\n\n⚠️ Не влезли в лимиты заказа и пропущены: "
                    + ", ".join(str(q) for q in skipped))
    else:
        value = "" if raw == "-" else raw
        shown = value or "убрано"

    await runtime.set_value(conn, field, value)
    # Лимит запросов API — простое число с разумными границами.
    if field == "api_rate_per_min":
        if not raw.isdigit() or not 1 <= int(raw) <= 10_000:
            await message.answer("❌ Нужно число от 1 до 10000.")
            return
        await runtime.set_value(conn, "api_rate_per_min", raw)
        await state.clear()
        await message.answer(
            f"✅ Лимит: <b>{raw}</b> запросов в минуту на ключ.",
            reply_markup=back_kb("pn:api", "‹ К API"),
        )
        return

    if field == "fazer_games_key":
        # Старый клиент держит прежний ключ — выбрасываем его.
        from app.services import suppliers

        suppliers.forget()

    if field == "sponsor_channel":
        # Память о том, кто подписан, относится к прежним каналам.
        # Оставить её — значит пускать без подписки на новый.
        from app.services import sponsor

        sponsor.forget()
    await state.clear()

    extra = note
    if field == "margin_percent":
        # Наценка сразу поднимает цены: звёзды, Premium и все игры без своей наценки
        changes = await pricing.apply_margin_now(conn, provider)
        extra += ("\n\n📈 Цены пересчитаны:\n" + "\n".join(f"├ {c}" for c in changes[:8])
                  if changes else "\n\n📈 Цены уже по этой наценке.")
        extra += "\n🕹 Игры — по новой наценке сразу (кроме игр со своей наценкой)."
    if field == "star_cost_diram" and runtime.margin_percent() > 0:
        extra = (f"\n\nПо наценке {runtime.margin_percent()}% цена продажи должна быть "
                 f"<b>{fmt(runtime.price_from_margin())}</b> — нажмите «Пересчитать по наценке».")
    if field in ("min_stars", "max_stars") and runtime.min_stars() > runtime.max_stars():
        extra = "\n\n❗️ Минимум больше максимума — клиенты не смогут купить ничего."

    await message.answer(
        f"✅ <b>{FIELDS[field][0]}</b> → <b>{shown}</b>{extra}",
        reply_markup=back_kb(FIELD_PARENT.get(field, "pn:home"), "‹ Назад"),
    )


@router.callback_query(F.data == "pn:notice")
async def cb_notice(call: CallbackQuery, state: FSMContext) -> None:
    await start_field_edit(call, state, "support_notice")
    await call.answer()


# ============================================================== разделы


@router.callback_query(F.data == "pn:toggles")
async def cb_toggles(call: CallbackQuery) -> None:
    await safe_edit(call, toggles_text(), toggles_kb())
    await call.answer()


def toggles_text() -> str:
    return (
        "🔀 <b>Разделы бота</b>\n\n"
        "Выключенный раздел пропадает из меню у клиентов. Удобно, когда "
        "закончились звёзды на Fragment или меняете реквизиты.\n\n"
        f"{'✅' if runtime.get_bool('stars_enabled') else '🚫'} Продажа звёзд\n"
        f"{'✅' if runtime.get_bool('premium_enabled') else '🚫'} Продажа Premium\n"
        f"{'✅' if runtime.get_bool('deposit_enabled') else '🚫'} Пополнение баланса\n\n"
        f"🏆 Топ клиентов считается по сумме {top_basis_ru(runtime.get('top_by'))}."
    )


def toggles_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in (
        ("stars_enabled", "звёзды"),
        ("premium_enabled", "Premium"),
        ("deposit_enabled", "пополнение"),
    ):
        state = runtime.get_bool(key)
        kb.row(btn(
            ("🚫 Выключить " if state else "✅ Включить ") + label,
            f"pn:toggle:{key}", style=DANGER if state else SUCCESS,
        ))
    by_deposits = runtime.get("top_by") == "deposits"
    kb.row(btn(
        "🏆 Топ по " + ("покупкам" if by_deposits else "пополнениям"),
        "pn:topby", style=PRIMARY,
    ))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:topby")
async def cb_topby(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Переключить, по чему строится топ клиентов."""
    value = "purchases" if runtime.get("top_by") == "deposits" else "deposits"
    await runtime.set_value(conn, "top_by", value)
    await call.answer("Готово")
    await safe_edit(call, toggles_text(), toggles_kb())


@router.callback_query(F.data.startswith("pn:toggle:"))
async def cb_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    key = call.data.rsplit(":", 1)[1]
    if key not in ("stars_enabled", "premium_enabled", "deposit_enabled"):
        await call.answer("Неизвестный раздел.", show_alert=True)
        return
    turning_on = not runtime.get_bool(key)
    await runtime.set_value(conn, key, "1" if turning_on else "0")
    # Владелец включил продажу руками — значит бот её больше не держит.
    # Оставить флаг поднятым нельзя: по нему защита решает, что о поломке
    # уже предупреждала, и следующее отключение пройдёт молча.
    if turning_on and key in ("stars_enabled", "premium_enabled"):
        await runtime.set_value(conn, "autostopped", "0")
    await call.answer("Готово")
    await safe_edit(call, toggles_text(), toggles_kb())


# ============================================================== списки


@router.callback_query(F.data == "pn:stats")
async def cb_stats(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    await safe_edit(call, texts.money_stats(await db.global_stats(conn)), back_kb())
    await call.answer()


@router.callback_query(F.data == "pn:deposits")
async def cb_deposits(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    deposits = await db.list_deposits(conn, status=db.DEP_PENDING, limit=20)
    kb = InlineKeyboardBuilder()

    # Заявка заводится, как только клиент назвал сумму, — иначе юзерботу
    # не с чем сопоставлять пришедшие деньги. Поэтому здесь два разных
    # списка: где чек уже прислали и где ещё просто ждут перевода.
    # Мешать их в кучу нельзя: во втором делать нечего, пока не заплатят.
    with_receipt = [d for d in deposits if d.receipt_file_id]
    waiting = [d for d in deposits if not d.receipt_file_id]

    if not deposits:
        body = ("📥 <b>Заявки на пополнение</b>\n\nНа проверке ничего нет.\n\n"
                "<blockquote>Оплаты, которые банк подтвердил сам, "
                "зачисляются без вас — смотрите «🏦 Оплаты банка»."
                "</blockquote>")
    else:
        parts = []
        if with_receipt:
            rows = []
            for dep in with_receipt[:10]:
                rows.append(f"<b>№{dep.id}</b> · {fmt(dep.amount)} · "
                            f"<code>{dep.user_id}</code>")
                kb.row(
                    InlineKeyboardButton(text=f"✅ №{dep.id}",
                                         callback_data=f"a:dep_ok:{dep.id}"),
                    InlineKeyboardButton(text=f"❌ №{dep.id}",
                                         callback_data=f"a:dep_no:{dep.id}"),
                )
            parts.append("📸 <b>Чек прислан — нужна проверка</b>\n"
                         + "\n".join(rows))

        if waiting:
            rows = "\n".join(
                f"<b>№{dep.id}</b> · {fmt(dep.amount)} · "
                f"<code>{dep.user_id}</code> · <i>{dep.created_at[11:16]}</i>"
                for dep in waiting[:8]
            )
            parts.append(f"⏳ <b>Ждут перевода — {len(waiting)}</b>\n{rows}\n"
                         "<i>Делать ничего не надо: придут деньги — "
                         "зачислится само.</i>")

        body = ("📥 <b>Заявки на пополнение</b>\n\n" + "\n\n".join(parts)
                + "\n\n<blockquote>💡 Зачисляйте только то, что видите "
                  "в банке. Чек можно нарисовать.</blockquote>")
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="pn:deposits"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    await safe_edit(call, body, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "pn:tickets")
async def cb_tickets(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    tickets = await db.list_tickets(conn, status=db.TICKET_OPEN, limit=10)
    if not tickets:
        body = "📞 <b>Тикеты</b>\n\nОткрытых нет."
    else:
        lines = [
            f"<b>№{t.id}</b> от <code>{t.user_id}</code>\n"
            f"<i>{t.subject[:150]}</i>\n"
            f"Ответить: <code>/answer {t.id} текст</code>"
            for t in tickets
        ]
        body = "📞 <b>Открытые тикеты</b>\n\n" + "\n\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="pn:tickets"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    await safe_edit(call, body, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "pn:orders")
async def cb_orders(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    failed = await db.list_orders(conn, status=db.ORDER_FAILED, limit=5)
    recent = await db.list_orders(conn, limit=8)

    blocks = []
    if failed:
        blocks.append(
            "⚠️ <b>Требуют решения</b>\n"
            + "\n".join(
                f"<b>№{o.id}</b> {o.title} → @{o.recipient} · {fmt(o.price)}\n"
                f"Дошёл: <code>/done {o.id}</code> · Вернуть: <code>/refund {o.id}</code>"
                for o in failed
            )
        )
    if recent:
        blocks.append(
            "📦 <b>Последние заказы</b>\n"
            + "\n".join(
                f"<b>№{o.id}</b> {o.title} → @{o.recipient}\n"
                f"{fmt(o.price)} · {o.status_title}"
                for o in recent
            )
        )
    body = "\n\n".join(blocks) if blocks else "📦 <b>Заказы</b>\n\nЗаказов ещё нет."

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="pn:orders"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    await safe_edit(call, body, kb.as_markup())
    await call.answer()


def promo_line(row) -> str:
    """Одна строка списка: что за код, сколько потрачено и жив ли он."""
    left = max(row["max_uses"] - row["used_count"], 0)
    alive = left > 0
    value = (f"−{row['percent']}%" if row["kind"] == "discount"
             else f"+{fmt(row['amount'])}")
    return (
        f"{'🟢' if alive else '🔴'} <code>{row['code']}</code> — <b>{value}</b>\n"
        f"├ Использован: <b>{row['used_count']}</b> из <b>{row['max_uses']}</b>\n"
        f"├ Осталось: <b>{left}</b>\n"
        f"└ Статус: <b>{'Активен' if alive else 'Закончился'}</b>"
    )


def promos_text(rows: list) -> str:
    if not rows:
        body = (
            "<blockquote>Пока ни одного. Нажмите «Создать промокод» — "
            "спрошу код, процент скидки и число активаций.</blockquote>"
        )
    else:
        body = "\n\n".join(promo_line(row) for row in rows)
    return (
        "🎟 <b>Промокоды</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{body}\n\n"
        "<blockquote>Скидка действует на все товары бота. Активация "
        "списывается только после успешно выданного заказа.</blockquote>"
    )


#: Сколько промокодов показываем на экране. Кнопка удаления ищет код
#: среди них же, поэтому число должно быть одно на обоих концах.
PROMO_LIMIT = 15


def promo_tag(code: str) -> str:
    """Короткая метка кода для кнопки.

    Раньше кнопка несла код целиком, и он же служил адресом. У Telegram
    на callback_data 64 байта, а кириллица занимает два байта на букву:
    код из двадцати шести русских букв выносил кнопку за предел. Такую
    кнопку Telegram рисует как обычную, но нажатие до бота не доходит —
    со стороны владельца она просто не работает.
    """
    return hashlib.sha1(code.encode()).hexdigest()[:16]


def promo_del_data(code: str) -> str:
    return f"pn:promo_del:{promo_tag(code)}"


def promos_kb(rows: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("➕ Создать промокод", "pn:promo_new", style=SUCCESS))
    for row in rows[:15]:
        kb.row(InlineKeyboardButton(
            text=f"🗑 {row['code']}", callback_data=promo_del_data(row["code"]),
        ))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:promos")
async def cb_promos(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    rows = await db.list_promos(conn, limit=15)
    await safe_edit(call, promos_text(rows), promos_kb(rows))
    await call.answer()


@router.callback_query(F.data.startswith("pn:promo_del:"))
async def cb_promo_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    tag = call.data.split(":", 2)[2]
    rows = await db.list_promos(conn, limit=PROMO_LIMIT)
    code = next((r["code"] for r in rows if promo_tag(r["code"]) == tag), "")
    if not code:
        await call.answer("Этот промокод уже удалили.", show_alert=True)
    else:
        await db.delete_promo(conn, code)
        await call.answer(f"{code} удалён")
        rows = await db.list_promos(conn, limit=PROMO_LIMIT)
    await safe_edit(call, promos_text(rows), promos_kb(rows))


# ------------------------------------------- пошаговое создание промокода


@router.callback_query(F.data == "pn:promo_new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoNew.code)
    await state.update_data(promo={})
    await safe_edit(
        call,
        "🎟 <b>Новый промокод</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<b>Шаг 1 из 3 — Промокод</b>\n\n"
        "<blockquote>Введите промокод.\n\nНапример: <code>ALI10</code></blockquote>",
        back_kb("pn:promos", "❌ Отмена"),
    )
    await call.answer()


@router.message(PromoNew.code, F.text)
async def on_promo_code(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    code = (message.text or "").strip().upper()
    if not code.isalnum() or len(code) > 32:
        await message.answer(
            "❌ Код — латинские буквы и цифры, до 32 символов.\n\n"
            "<i>Например: <code>ALI10</code></i>"
        )
        return
    if await db.get_promo(conn, code):
        await message.answer(f"❌ Промокод <code>{code}</code> уже есть.")
        return

    await state.update_data(promo={"code": code})
    await state.set_state(PromoNew.percent)
    await message.answer(
        f"🎟 <b>Промокод {code}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<b>Шаг 2 из 3 — Скидка</b>\n\n"
        "<blockquote>Сколько процентов скидка?\n\nНапример: <code>10</code></blockquote>",
        reply_markup=back_kb("pn:promos", "❌ Отмена"),
    )


@router.message(PromoNew.percent, F.text)
async def on_promo_percent(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().rstrip("%").replace(",", ".").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        await message.answer("❌ Введите целое число от 1 до 100.")
        return

    data = await state.get_data()
    promo = dict(data.get("promo") or {})
    promo["percent"] = int(raw)
    await state.update_data(promo=promo)
    await state.set_state(PromoNew.limit)
    await message.answer(
        f"🎟 <b>Промокод {promo['code']}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<b>Шаг 3 из 3 — Количество активаций</b>\n\n"
        "<blockquote>Сколько раз можно активировать этот промокод?\n\n"
        "Например: <code>100</code></blockquote>",
        reply_markup=back_kb("pn:promos", "❌ Отмена"),
    )


@router.message(PromoNew.limit, F.text)
async def on_promo_limit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 1_000_000:
        await message.answer("❌ Введите целое число активаций, например <code>100</code>.")
        return

    data = await state.get_data()
    promo = dict(data.get("promo") or {})
    promo["limit"] = int(raw)
    await state.update_data(promo=promo)
    await state.set_state(PromoNew.confirm)

    kb = InlineKeyboardBuilder()
    kb.row(btn("💾 Сохранить", "pn:promo_save", style=SUCCESS))
    kb.row(btn("❌ Отмена", "pn:promos", style=DANGER))
    await message.answer(promo_preview(promo), reply_markup=kb.as_markup())


def promo_preview(promo: dict) -> str:
    return (
        "🎟 <b>Проверьте промокод</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Промокод: <code>{promo.get('code')}</code>\n"
        f"├ Скидка: <b>{promo.get('percent')}%</b>\n"
        f"└ Лимит активаций: <b>{promo.get('limit')}</b>\n\n"
        "<blockquote>После сохранения код сразу заработает: клиенты смогут "
        "ввести его при покупке любого товара.</blockquote>"
    )


@router.callback_query(PromoNew.confirm, F.data == "pn:promo_save")
async def cb_promo_save(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    promo = data.get("promo") or {}
    await state.clear()

    if not promo.get("code") or not promo.get("percent") or not promo.get("limit"):
        await call.answer("Данные потерялись — начните заново.", show_alert=True)
        rows = await db.list_promos(conn, limit=15)
        await safe_edit(call, promos_text(rows), promos_kb(rows))
        return

    saved = await db.create_promo(
        conn, promo["code"], amount=0, max_uses=promo["limit"],
        kind="discount", percent=promo["percent"],
    )
    if not saved:
        await call.answer("Такой промокод уже есть.", show_alert=True)
        rows = await db.list_promos(conn, limit=15)
        await safe_edit(call, promos_text(rows), promos_kb(rows))
        return

    await call.answer("Сохранён")
    await safe_edit(
        call,
        f"✅ <b>Промокод {promo['code']} создан</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Скидка: <b>{promo['percent']}%</b>\n"
        f"└ Активаций: <b>{promo['limit']}</b>\n\n"
        "<blockquote>Код уже активен.</blockquote>",
        back_kb("pn:promos", "‹ К промокодам"),
    )


# ----------------------------- старый однострочный ввод (код на баланс)


@router.message(PromoNew.data, F.text)
async def on_promo_new(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer("❌ Формат: <code>КОД сумма лимит</code>, например "
                             "<code>SALE10 10 100</code>")
        return
    amount = parse(parts[1])
    if amount is None or amount <= 0:
        await message.answer("❌ Сумма должна быть положительным числом.")
        return
    if not await db.create_promo(conn, parts[0], amount, int(parts[2])):
        await message.answer("❌ Такой промокод уже есть.")
        return
    await state.clear()
    await message.answer(
        f"✅ Промокод <code>{parts[0].upper()}</code> на <b>{fmt(amount)}</b>, "
        f"активаций: <b>{parts[2]}</b>",
        reply_markup=back_kb("pn:promos", "‹ К промокодам"),
    )


# =========================================================== проверка связи


@router.callback_query(F.data == "pn:fragment")
async def cb_fragment(call: CallbackQuery, provider) -> None:
    """Пошаговая проверка: можно ли вообще выдавать товар."""
    await safe_edit(call, "🔌 Проверяю связь с Fragment…", back_kb())
    await call.answer()

    try:
        report = await provider.healthcheck()
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        log.exception("Проверка Fragment упала")
        await safe_edit(
            call,
            f"🔌 <b>Проверка Fragment</b>\n\n❌ Сорвалась с ошибкой:\n"
            f"<code>{type(exc).__name__}: {exc}</code>",
            back_kb(),
        )
        return

    lines = [f"├ {name}: {result}" for name, result in report["steps"]]
    if lines:
        lines[-1] = "└" + lines[-1][1:]

    if report["mode"] == "mock":
        verdict = (
            "⚠️ <b>Режим MOCK.</b>\n\n"
            "Бот работает целиком, но звёзды <b>никуда не отправляются</b> — "
            "это режим для проверки. Клиенты будут получать «заказ выполнен», "
            "не получая звёзд.\n\n"
            "Для реальных продаж поставьте <code>FRAGMENT_MODE=api</code> "
            "в .env и перезапустите бота."
        )
    elif report["ok"]:
        verdict = (
            "✅ <b>Связь есть, выдача возможна.</b>\n\n"
            "Проверьте, что на кошельке хватает средств: при нехватке "
            "заказ упадёт, а деньги вернутся клиенту на баланс."
        )
    else:
        verdict = (
            "❌ <b>Выдача невозможна.</b>\n\n"
            "Пока не починить — не включайте продажу звёзд, иначе клиенты "
            "будут платить и получать возврат. Выключить можно "
            "в разделе «Разделы»."
        )

    kb = InlineKeyboardBuilder()
    if getattr(provider, "probe_paths", None) is not None:
        kb.row(btn("🔍 Найти адреса API", "pn:probe", style=PRIMARY))
    kb.row(InlineKeyboardButton(text="🔄 Проверить снова", callback_data="pn:fragment"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))

    await safe_edit(
        call,
        "🔌 <b>Проверка связи</b>\n\n" + "\n".join(lines) + "\n\n" + verdict,
        kb.as_markup(),
    )


@router.callback_query(F.data == "pn:probe")
async def cb_probe(call: CallbackQuery, provider) -> None:
    """Найти рабочие адреса перебором — быстрее, чем сверять документацию."""
    probe = getattr(provider, "probe_paths", None)
    if probe is None:
        await call.answer("Этот сервис не умеет искать адреса.", show_alert=True)
        return

    await safe_edit(call, "🔍 Перебираю адреса, это займёт секунд десять…",
                    back_kb("pn:fragment"))
    await call.answer()

    try:
        found = await probe()
    except Exception as exc:  # noqa: BLE001
        log.warning("Перебор адресов не удался: %s", exc)
        await safe_edit(call, f"❌ Не получилось: <code>{exc}</code>",
                        back_kb("pn:fragment"))
        return

    kb = InlineKeyboardBuilder()
    blocks = []

    for kind, title, prefix in (
        ("balance", "💰 Баланс", "pn:usepath:balance:"),
        ("orders", "📦 Заказы", "pn:usepath:orders:"),
    ):
        hits = found.get(kind) or []
        if not hits:
            blocks.append(f"{title}\n└ ничего не нашлось")
            continue
        lines = [f"├ <code>{path}</code>\n│  <i>{sample}</i>" for path, sample in hits]
        blocks.append(f"{title}\n" + "\n".join(lines))
        for path, _ in hits[:3]:
            kb.row(InlineKeyboardButton(
                text=f"✅ Взять {path}", callback_data=prefix + path,
            ))

    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:fragment"))
    await safe_edit(
        call,
        "🔍 <b>Что ответило сервисом</b>\n\n" + "\n\n".join(blocks)
        + "\n\nВыберите подходящий адрес — бот запомнит его.",
        kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:usepath:"))
async def cb_use_path(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    _, _, kind, path = call.data.split(":", 3)
    if kind == "balance":
        await runtime.set_value(conn, "fazer_balance_path", path)
    else:
        # Одиночный заказ обычно лежит рядом со списком.
        await runtime.set_value(conn, "fazer_order_path", path.rstrip("/") + "/{order_id}")
    await call.answer("Адрес сохранён — нажмите «Проверить связь»")


# ============================================================== кошелёк


def wallet_text() -> str:
    stars = runtime.get_int("stars_since_topup")
    months = runtime.get_int("premium_since_topup")
    topup = runtime.get("topup_at") or "не отмечалось"
    cost = runtime.star_cost()

    spent = f"~{fmt(stars * cost)}" if cost > 0 else "неизвестно (не задана себестоимость)"
    status = (
        "🛑 <b>Продажа выключена ботом</b> — выдача не проходила подряд."
        if runtime.get_bool("autostopped")
        else f"✅ Продажа работает. Неудач подряд: <b>{runtime.get_int('fail_streak')}</b>."
    )

    return (
        "💼 <b>Кошелёк Fragment</b>\n\n"
        f"{status}\n\n"
        f"📅 Последнее пополнение: <b>{topup}</b>\n\n"
        "<b>Выдано с тех пор:</b>\n"
        f"├ Звёзд: <b>{stars}</b> (себестоимость {spent})\n"
        f"└ Premium: <b>{months} мес.</b>\n\n"
        f"🛑 Бот гасит продажу после <b>{runtime.autostop_after()}</b> неудач подряд — "
        "чтобы клиенты не платили в пустоту, пока вас нет.\n\n"
        "💡 Пополнили кошелёк — нажмите кнопку ниже: счётчики обнулятся, "
        "продажа включится обратно."
    )


def wallet_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("✅ Я пополнил кошелёк", "pn:topup", style=SUCCESS))
    kb.row(InlineKeyboardButton(text="🔢 Порог автостопа", callback_data="pn:set:autostop_after"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:wallet")
async def cb_wallet(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call, wallet_text(), wallet_kb())
    await call.answer()


@router.callback_query(F.data == "pn:topup")
async def cb_topup(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    was_stopped = runtime.get_bool("autostopped")
    # Дату берём из сообщения Telegram, а не из системных часов сервера:
    # так она совпадает с тем, что видит владелец в переписке.
    await runtime.mark_topup(conn, call.message.date.strftime("%d.%m.%Y %H:%M UTC"))
    await call.answer("Счётчики обнулены" + (", продажа включена" if was_stopped else ""))
    await safe_edit(call, wallet_text(), wallet_kb())


# ================================================= себестоимость из API


@router.callback_query(F.data == "pn:cost")
async def cb_cost(call: CallbackQuery, provider) -> None:
    """Спросить у сервиса выдачи, во что заказ обходится владельцу."""
    estimate_fn = getattr(provider, "cost_estimate", None)
    if estimate_fn is None:
        await call.answer(
            "Этот способ выдачи не умеет отдавать цену. "
            "Себестоимость придётся вписать вручную.",
            show_alert=True,
        )
        return

    rate = runtime.usd_rate()
    if rate <= 0:
        await safe_edit(
            call,
            "💱 <b>Сначала задайте курс доллара</b>\n\n"
            "Сервис выдачи считает в TON и долларах, а вы продаёте за сомони. "
            "Чтобы перевести одно в другое, боту нужен курс.\n\n"
            "Посмотрите курс доллара к сомони и нажмите кнопку ниже.",
            back_kb("pn:set:usd_rate_diram", "💱 Задать курс"),
        )
        await call.answer()
        return

    await safe_edit(call, "📡 Спрашиваю цену у сервиса выдачи…", back_kb("pn:prices"))
    await call.answer()

    try:
        # Считаем на 1000 звёзд: так точнее, чем на 50, и меньше округления.
        estimate = await estimate_fn("stars", 1000)
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        log.warning("Не удалось узнать себестоимость: %s", exc)
        await safe_edit(
            call,
            f"❌ <b>Цена не пришла</b>\n\n<code>{exc}</code>\n\n"
            "Проверьте связь: /panel → 🔌 Проверить связь.",
            back_kb("pn:prices"),
        )
        return

    # Себестоимость одной звезды в дирамах.
    cost = int((estimate.usd_per_unit * rate * 100).to_integral_value(
        rounding="ROUND_HALF_UP"))
    margin = runtime.margin_percent()
    suggested = round(cost * (100 + margin) / 100) if margin else cost

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=f"✅ Записать {fmt4(cost)} как себестоимость",
        callback_data=f"pn:cost_save:{cost}",
    ))
    kb.row(InlineKeyboardButton(text="📈 Изменить наценку", callback_data="pn:set:margin_percent"))
    kb.row(InlineKeyboardButton(text="💱 Изменить курс", callback_data="pn:set:usd_rate_diram"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:prices"))

    margin_line = (
        f"\n📈 С вашей наценкой <b>{margin}%</b> продавать по "
        f"<b>{fmt4(suggested)}</b> за звезду\n"
        f"   (прибыль <b>{fmt4(suggested - cost)}</b> со звезды, "
        f"<b>{fmt(((suggested - cost) * 1000 + 50) // 100)}</b> с 1000)"
        if margin else
        "\n📈 Наценка не задана — поставьте её, и бот посчитает цену продажи."
    )

    await safe_edit(
        call,
        "📡 <b>Себестоимость от сервиса выдачи</b>\n\n"
        f"За <b>{estimate.quantity} звёзд</b> вы платите "
        f"<b>{estimate.amount} {estimate.currency.upper()}</b>\n"
        f"Это <b>${estimate.usd_total:.2f}</b>"
        + (f" (курс TON: ${estimate.usdt_per_ton})" if estimate.usdt_per_ton else "")
        + f"\n\n💱 По вашему курсу <b>{fmt(rate)}</b> за доллар:\n"
        f"└ одна звезда обходится в <b>{fmt4(cost)}</b>\n"
        f"{margin_line}\n\n"
        "⚠️ Курс TON меняется — проверяйте себестоимость раз в несколько дней.",
        kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:cost_save:"))
async def cb_cost_save(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    cost = int(call.data.rsplit(":", 1)[1])
    await runtime.set_value(conn, "star_cost_e4", str(cost))

    margin = runtime.margin_percent()
    if margin > 0:
        await runtime.set_value(conn, "star_price_e4",
                                str(runtime.price_from_margin_e4()))
        await call.answer(f"Себестоимость {fmt4(cost)}, цена пересчитана")
    else:
        await call.answer(f"Себестоимость {fmt4(cost)} записана")
    await safe_edit(call, prices_text(), prices_kb())


# ============================================================= оформление


@router.callback_query(F.data == "pn:look")
async def cb_look(call: CallbackQuery, state: FSMContext) -> None:
    """Список групп значков. Меняются по одному, видно сразу."""
    await state.clear()
    kb = InlineKeyboardBuilder()
    for group in emoji.GROUPS:
        kb.row(InlineKeyboardButton(text=group, callback_data=f"pn:emg:{group}"))
    kb.row(btn(
        "🚫 Выключить премиум-эмодзи" if emoji.premium_on()
        else "💎 Проверить премиум-эмодзи",
        "pn:emtest",
        style=DANGER if emoji.premium_on() else PRIMARY,
    ))
    kb.row(btn("♻️ Вернуть все по умолчанию", "pn:emreset", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))

    preview = "  ".join(emoji.em(key) for key in list(emoji.DEFAULTS)[:12])
    custom_count = sum(1 for key in emoji.DEFAULTS if emoji.custom_id(key))
    await safe_edit(
        call,
        "🎨 <b>Оформление</b>\n\n"
        "<blockquote>Каждый значок в боте можно заменить своим. "
        "Изменения видны клиентам сразу, перезапуск не нужен.</blockquote>\n\n"
        f"Сейчас: {preview}\n\n"
        f"💎 Премиум-эмодзи: <b>{'включены' if emoji.premium_on() else 'выключены'}</b> "
        f"<i>({custom_count} шт. задано)</i>\n\n"
        "<i>Выберите группу</i> " + emoji.em("point"),
        kb.as_markup(),
    )
    await call.answer()


async def render_emoji_group(call: CallbackQuery, group: str) -> bool:
    """Показать значки одной группы. False — такой группы нет."""
    items = emoji.GROUPS.get(group)
    if not items:
        return False

    kb = InlineKeyboardBuilder()
    lines = []
    for key, (default, title) in items.items():
        current = emoji.em(key)
        changed = " <i>(изменено)</i>" if current != default else ""
        lines.append(f"{current} — {title}{changed}")
        kb.row(InlineKeyboardButton(
            text=f"{current} {title}", callback_data=f"pn:emset:{key}",
        ))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:look"))

    await safe_edit(
        call,
        f"🎨 <b>{group}</b>\n\n" + "\n".join(lines)
        + "\n\n<i>Нажмите на значок, чтобы заменить.</i>",
        kb.as_markup(),
    )
    return True


@router.callback_query(F.data.startswith("pn:emg:"))
async def cb_emoji_group(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await render_emoji_group(call, call.data.split(":", 2)[2]):
        await call.answer("Группа не найдена.", show_alert=True)
        return
    await call.answer()


@router.callback_query(F.data.startswith("pn:emset:"))
async def cb_emoji_set(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.rsplit(":", 1)[1]
    if key not in emoji.DEFAULTS:
        await call.answer("Такого значка нет.", show_alert=True)
        return
    await state.set_state(Panel.emoji)
    await state.update_data(emoji_key=key)

    group = next(g for g, items in emoji.GROUPS.items() if key in items)
    kb = InlineKeyboardBuilder()
    if emoji.custom_id(key):
        kb.row(btn("🚫 Убрать премиум-эмодзи", f"pn:emcustdel:{key}", style=DANGER))
    kb.row(InlineKeyboardButton(text=f"♻️ Вернуть {emoji.DEFAULTS[key]}",
                                callback_data=f"pn:emdef:{key}"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data=f"pn:emg:{group}"))

    premium_line = ""
    if emoji.custom_id(key):
        state_note = "работает" if emoji.premium_on() else "выключен — включите проверку"
        premium_line = (
            f"├ Премиум-эмодзи: <code>{emoji.custom_id(key)}</code> "
            f"<i>({state_note})</i>\n"
        )

    await safe_edit(
        call,
        f"🎨 <b>{emoji.TITLES[key]}</b>\n\n"
        f"├ Сейчас: {emoji.em(key)}\n"
        f"{premium_line}"
        f"└ По умолчанию: {emoji.DEFAULTS[key]}\n\n"
        "<blockquote>Пришлите новый значок одним сообщением.\n\n"
        "Обычный эмодзи — заменит везде, включая кнопки.\n"
        "Можно прислать и <b>ID премиум-эмодзи</b> числом.\n"
        "<b>Премиум-эмодзи</b> — бот сам возьмёт его ID. В кнопках он "
        "не отображается (Telegram не поддерживает), поэтому там "
        "останется обычный.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.message(Panel.emoji, F.text)
async def on_emoji_value(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    key = data.get("emoji_key")
    if key not in emoji.DEFAULTS:
        await state.clear()
        await message.answer("Не понял, что менять. Откройте /panel заново.")
        return

    group = next(g for g, items in emoji.GROUPS.items() if key in items)

    # Премиум-эмодзи приходит обычным символом плюс сущность с его ID —
    # владельцу не надо искать ID руками, достаточно прислать сам эмодзи.
    custom = emoji.extract_custom(message)
    if custom is not None:
        emoji_id, fallback = custom
        await runtime.set_value(conn, f"emoji_id_{key}", emoji_id)
        await runtime.set_value(conn, f"emoji_{key}", fallback)
        await state.clear()

        note = (
            "" if emoji.premium_on() else
            "\n\n<blockquote>[[warn]] Премиум-эмодзи пока выключены. "
            "Нажмите «Проверить премиум-эмодзи» в разделе «Оформление» — "
            "бот убедится, что Telegram их принимает, и включит.</blockquote>"
        )
        await message.answer(
            f"✅ <b>{emoji.TITLES[key]}</b> — премиум-эмодзи принят\n\n"
            f"├ ID: <code>{emoji_id}</code>\n"
            f"└ Запасной значок: {fallback}\n\n"
            "<i>Запасной увидят там, где премиум-эмодзи не отображается "
            "(в кнопках и у части клиентов).</i>" + substitute(note),
            reply_markup=back_kb(f"pn:emg:{group}", "‹ К группе"),
        )
        return

    value = (message.text or "").strip()

    # ID премиум-эмодзи можно прислать и числом — так удобнее, когда ID
    # уже выписан, а сам эмодзи под рукой не вставить.
    if value.isdigit() and 15 <= len(value) <= 25:
        await runtime.set_value(conn, f"emoji_id_{key}", value)
        await state.clear()
        note = (
            "" if emoji.premium_on() else
            "\n\n<blockquote>[[warn]] Премиум-эмодзи пока выключены. "
            "Нажмите «Проверить премиум-эмодзи» в разделе «Оформление».</blockquote>"
        )
        await message.answer(
            f"✅ <b>{emoji.TITLES[key]}</b> — ID принят\n\n"
            f"├ ID: <code>{value}</code>\n"
            f"└ Запасной значок: {emoji.em(key)}\n\n"
            "<i>Проверьте, как он выглядит: если Telegram этот ID не знает, "
            "клиенты увидят запасной значок.</i>" + substitute(note),
            reply_markup=back_kb(f"pn:emg:{group}", "‹ К группе"),
        )
        return

    if not emoji.is_emoji_like(value):
        await message.answer(
            "❌ Нужен один значок — эмодзи или символ вроде <code>•</code>.\n"
            "Слова и длинный текст не подойдут."
        )
        return

    await runtime.set_value(conn, f"emoji_{key}", value)
    await runtime.set_value(conn, f"emoji_id_{key}", "")  # обычный значок отменяет премиум
    await state.clear()
    await message.answer(
        f"✅ <b>{emoji.TITLES[key]}</b> теперь {value}\n\n"
        "<i>Клиенты увидят изменение сразу.</i>",
        reply_markup=back_kb(f"pn:emg:{group}", "‹ К группе"),
    )


@router.callback_query(F.data.startswith("pn:emdef:"))
async def cb_emoji_default(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    key = call.data.rsplit(":", 1)[1]
    await runtime.reset(conn, f"emoji_{key}")
    await call.answer(f"Вернул {emoji.DEFAULTS.get(key, '')}")
    group = next((g for g, items in emoji.GROUPS.items() if key in items), None)
    if group:
        await render_emoji_group(call, group)


@router.callback_query(F.data == "pn:emreset")
async def cb_emoji_reset_all(call: CallbackQuery, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(btn("♻️ Да, вернуть всё", "pn:emreset2", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ Отмена", callback_data="pn:look"))
    await safe_edit(
        call,
        "♻️ <b>Вернуть значки по умолчанию?</b>\n\n"
        "<blockquote>Все ваши замены будут сброшены.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:emreset2")
async def cb_emoji_reset_confirm(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    for key in emoji.DEFAULTS:
        await runtime.reset(conn, f"emoji_{key}")
    await call.answer("Значки возвращены")
    await cb_look(call, state)


@router.callback_query(F.data.startswith("pn:emcustdel:"))
async def cb_custom_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    key = call.data.rsplit(":", 1)[1]
    # Именно пустое значение, а не сброс: у части значков ID прописан
    # по умолчанию, и сброс вернул бы его обратно.
    await runtime.set_value(conn, f"emoji_id_{key}", "")
    await call.answer("Премиум-эмодзи убран")
    group = next((g for g, items in emoji.GROUPS.items() if key in items), None)
    if group:
        await render_emoji_group(call, group)


@router.callback_query(F.data == "pn:emtest")
async def cb_emoji_test(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    """Проверить, принимает ли Telegram премиум-эмодзи от этого бота.

    Без проверки включать нельзя: если у владельца нет Premium, Telegram
    отвергает каждое такое сообщение, и бот перестаёт отвечать вообще.
    """
    if emoji.premium_on():
        await runtime.set_value(conn, "custom_emoji_on", "0")
        await call.answer("Премиум-эмодзи выключены")
        await cb_look(call, state)
        return

    sample = next((key for key in emoji.DEFAULTS if emoji.custom_id(key)), None)
    if sample is None:
        await call.answer(
            "Сначала пришлите хотя бы один премиум-эмодзи — "
            "выберите значок в любой группе.",
            show_alert=True,
        )
        return

    probe = (
        f'<tg-emoji emoji-id="{emoji.custom_id(sample)}">{emoji.em(sample)}</tg-emoji>'
        " проверка премиум-эмодзи"
    )
    try:
        sent = await bot.send_message(call.from_user.id, probe)
    except TelegramAPIError as exc:
        await safe_edit(
            call,
            "💎 <b>Премиум-эмодзи не работают</b>\n\n"
            f"<blockquote>Telegram ответил:\n<code>{exc}</code></blockquote>\n\n"
            "Такое бывает, если у владельца бота нет Telegram Premium. "
            "Обычные значки продолжают работать.",
            back_kb("pn:look", "‹ К оформлению"),
        )
        await call.answer()
        return

    with suppress(TelegramAPIError):
        await bot.delete_message(sent.chat.id, sent.message_id)

    await runtime.set_value(conn, "custom_emoji_on", "1")
    await call.answer("Работает — включил")
    await cb_look(call, state)


# ══════════════════════════════════════════════════════════════ отчёты


def report_kb(active: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    row = []
    for key, (label, _) in reports.PRESETS.items():
        mark = "• " if key == active else ""
        row.append(btn(mark + label, f"pn:rep:{key}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(btn("📅 Свой период", "pn:repcustom", style=PRIMARY))
    kb.row(
        btn("⭐ Поставщик 1", f"pn:sup:main:{active or 'today'}"),
        btn("🕹 Поставщик 2", f"pn:sup:games:{active or 'today'}"),
    )
    kb.row(btn("‹ Назад", "pn:home"))
    return kb.as_markup()


#: Кто чей: у звёзд, Premium и Steam один счёт у поставщика, у игр —
#: другой. Так же решает и app/services/suppliers.py, когда выбирает
#: ключ для заказа: всё, что не игра, идёт с основного счёта.
SUPPLIERS: dict[str, tuple[str, str]] = {
    "main":  ("⭐ Поставщик 1", "Звёзды, Telegram Premium и Steam"),
    "games": ("🕹 Поставщик 2", "Игры"),
}


def supplier_kb(who: str, active: str) -> InlineKeyboardMarkup:
    """Тот же выбор периода, но не уходя с экрана поставщика."""
    kb = InlineKeyboardBuilder()
    row = []
    for key, (label, _) in reports.PRESETS.items():
        mark = "• " if key == active else ""
        row.append(btn(mark + label, f"pn:sup:{who}:{key}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    other = "games" if who == "main" else "main"
    kb.row(btn(SUPPLIERS[other][0], f"pn:sup:{other}:{active}", style=PRIMARY))
    kb.row(btn("‹ К отчёту", f"pn:rep:{active}"))
    return kb.as_markup()


def supplier_report(who: str, title: str, rows: list, same_key: bool) -> str:
    """Сколько мы должны одному поставщику за период.

    Главная цифра здесь не выручка, а себестоимость: именно её владелец
    отправляет поставщику. Выручку и прибыль показываем рядом, чтобы
    было видно, из чего сумма сложилась.
    """
    name, what = SUPPLIERS[who]
    mine = [r for r in rows if (r["is_game"] if who == "games" else not r["is_game"])]

    orders = sum(r["orders"] for r in mine)
    revenue = sum(r["revenue"] for r in mine)
    cost = sum(r["cost"] for r in mine)
    refunds = sum(r["refunds"] for r in mine)
    refunded_sum = sum(r["refunded_sum"] for r in mine)
    profit = revenue - cost
    margin = round(profit * 100 / cost) if cost else 0

    head = (f"🏷 <b>{name}</b> — {what}\n"
            f"<i>{title}</i>\n"
            f"<code>{texts.LINE}</code>\n\n")

    if not orders:
        body = ("<blockquote>За этот период по этому поставщику продаж "
                "не было.</blockquote>")
        if refunds:
            body += (f"\n\n↩️ <b>Возвраты:</b> {refunds} "
                     f"<i>({fmt(refunded_sum)})</i>")
        return head + body + _same_key_note(same_key)

    sold = [r for r in mine if r["orders"]]
    lines = "\n".join(
        f"{'└' if index == len(sold) - 1 else '├'} {r['title']} — "
        f"<b>{r['orders']}</b> зак. · продано {fmt(r['revenue'])} · "
        f"<b>ему {fmt(r['cost'])}</b>"
        for index, r in enumerate(sold)
    )

    return (
        head
        + f"💸 <b>Ему за период: {fmt(cost)}</b>\n"
        "<i>это себестоимость — сумма, которую вы ему отправляете</i>\n\n"
        f"📦 <b>Итого</b>\n"
        f"├ Заказов выполнено: <b>{orders}</b>\n"
        f"├ Продано клиентам на: <b>{fmt(revenue)}</b>\n"
        f"└ <b>Ваша прибыль: {fmt(profit)}</b> <i>({margin}%)</i>\n\n"
        f"🧾 <b>По товарам</b>\n{lines}\n"
        + (f"\n↩️ <b>Возвраты:</b> {refunds} <i>({fmt(refunded_sum)})</i>\n"
           "<i>за них поставщик денег не берёт</i>\n" if refunds else "")
        + _same_key_note(same_key)
    )


def _same_key_note(same_key: bool) -> str:
    """Один ключ на всё — значит и счёт один, и делить нечего."""
    if not same_key:
        return ""
    return ("\n\n<blockquote>⚠️ Отдельный ключ для игр не задан — игры идут "
            "с того же счёта, что звёзды. Сумму поставщику считайте "
            "по обоим экранам вместе.</blockquote>")


@router.callback_query(F.data.startswith("pn:sup:"))
async def cb_supplier_report(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Отчёт по одному поставщику: сколько ему отправлять за период."""
    from app.services import suppliers

    _, _, who, preset = call.data.split(":", 3)
    if who not in SUPPLIERS:
        await call.answer("Неизвестный поставщик.", show_alert=True)
        return

    await state.clear()
    start, end, title = reports.preset_range(preset)
    rows = await db.report_by_product(conn, *reports.bounds(start, end))
    await safe_edit(
        call,
        substitute(supplier_report(who, title, rows,
                                   not suppliers.has_own_games_key())),
        supplier_kb(who, preset),
    )
    await call.answer()


def product_block(rows: list) -> str:
    """Что продано по направлениям: звёзды, Premium, Steam, каждая игра.

    Одна общая цифра выручки не отвечает на главный вопрос владельца —
    это звёзды или игры. Игры сводим ещё и в одну строку: их много, и
    по отдельности видно каждую, а вместе — сколько даёт направление.

    Возвраты показываем тут же. Пятнадцать возвратов на тринадцать
    выдач в общей сводке выглядят загадкой, а в разбивке сразу видно,
    какое направление их делает.
    """
    if not rows:
        return ""

    def line(mark: str, row: dict) -> str:
        parts = [f"<b>{row['orders']}</b> зак.", f"<b>{fmt(row['revenue'])}</b>"]
        if row["cost"]:
            parts.append(f"<i>+{fmt(row['profit'])}</i>")
        text = f"{mark} {row['title']} — " + " · ".join(parts)
        if row["refunds"]:
            # Вторая строка идёт под своей веткой: у «├» ветка тянется
            # дальше, у «└» она уже закончилась.
            cont = mark.replace("├", "│").replace("└", " ") + "   "
            text += (f"\n{cont}↩️ возвратов: <b>{row['refunds']}</b> "
                     f"<i>({fmt(row['refunded_sum'])})</i>")
        return text

    games = [r for r in rows if r["is_game"]]
    plain = [r for r in rows if not r["is_game"]]

    out = []
    for index, row in enumerate(plain):
        last = index == len(plain) - 1 and not games
        out.append(line("└" if last else "├", row))

    if games:
        # Одна игра сама себе итог — вторая такая же строка только мешает.
        if len(games) == 1:
            out.append(line("└", games[0]))
        else:
            out.append(line("└", {
                "title": "🕹 Игры",
                "orders": sum(r["orders"] for r in games),
                "revenue": sum(r["revenue"] for r in games),
                "cost": sum(r["cost"] for r in games),
                "profit": sum(r["profit"] for r in games),
                "refunds": sum(r["refunds"] for r in games),
                "refunded_sum": sum(r["refunded_sum"] for r in games),
            }))
            for index, row in enumerate(games):
                out.append(line("   └" if index == len(games) - 1 else "   ├",
                                row))

    return "🧾 <b>Что продано</b>\n" + "\n".join(out) + "\n\n"


def format_report(title: str, data: dict, days: list, hint: str = "",
                  by_product: list | None = None) -> str:
    """Отчёт за период. Ручные правки показываем отдельной строкой:
    без этого деньги на балансах не сходились бы с пополнениями."""
    """Отчёт за период. Прибыль показываем, только если знаем себестоимость."""
    revenue, cost, profit = data["revenue"], data["cost"], data["profit"]
    margin = round(profit * 100 / cost) if cost else 0

    money_block = (
        f"├ Продано на: <b>{fmt(revenue)}</b>\n"
        f"├ Себестоимость: <b>{fmt(cost)}</b>\n"
        f"└ <b>Прибыль: {fmt(profit)}</b> <i>({margin}%)</i>"
        if cost else
        f"├ Продано на: <b>{fmt(revenue)}</b>\n"
        "└ <i>Прибыль не посчитать — себестоимость по этим заказам "
        "не сохранялась</i>"
    )

    chart = ""
    if len(days) > 1:
        rows = days[-7:]
        peak = max((r[2] for r in rows), default=0) or 1
        bars = []
        for day, done, day_revenue, day_profit in rows:
            filled = round(day_revenue * 10 / peak)
            bars.append(
                f"<code>{day[5:]}</code> {'█' * filled}{'░' * (10 - filled)} "
                f"{fmt(day_revenue)}"
            )
        chart = "\n\n<b>По дням</b>\n" + "\n".join(bars)

    return (
        f"📈 <b>{title}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"[[money]] <b>Деньги</b>\n{money_block}\n\n"
        f"📦 <b>Заказы</b>\n"
        f"├ Выполнено: <b>{data['done']}</b>\n"
        f"├ Возвращено: <b>{data['refunded']}</b> "
        f"<i>({fmt(data['refunded_sum'])})</i>\n"
        f"├ На разборе: <b>{data['failed']}</b>\n"
        f"├ Звёзд продано: <b>{data['stars']}</b>\n"
        f"└ Premium: <b>{data['premium_months']}</b> мес.\n\n"
        + product_block(by_product or [])
        + f"[[referral]] <b>Клиенты</b>\n"
        f"├ Новых: <b>{data['new_users']}</b>\n"
        f"├ Покупали: <b>{data['buyers']}</b>\n"
        f"└ Пополнений: <b>{data['deposits']}</b> "
        f"<i>({fmt(data['deposits_sum'])})</i>"
        + (f"\n\n✍️ <b>Правки вручную</b>\n"
           f"├ Начислено: <b>{fmt(data['adjust_added'])}</b>\n"
           f"└ Списано: <b>{fmt(data['adjust_taken'])}</b>"
           if data.get("adjust_added") or data.get("adjust_taken") else "")
        + chart + (f"\n\n<i>{hint}</i>" if hint else "")
    )


async def show_report(
    call: CallbackQuery, conn: aiosqlite.Connection,
    start, end, title: str, active: str = "",
) -> None:
    since, until = reports.bounds(start, end)
    data = await db.report(conn, since, until)
    days = await db.daily_series(conn, since, until, reports.tz_hours())
    by_product = await db.report_by_product(conn, since, until)
    await safe_edit(
        call,
        substitute(format_report(title, data, days, by_product=by_product)),
        report_kb(active),
    )


@router.callback_query(F.data == "pn:rep")
async def cb_report(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    start, end, title = reports.preset_range("today")
    await show_report(call, conn, start, end, title, "today")
    await call.answer()


@router.callback_query(F.data.startswith("pn:rep:"))
async def cb_report_preset(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    await state.clear()
    key = call.data.rsplit(":", 1)[1]
    start, end, title = reports.preset_range(key)
    await show_report(call, conn, start, end, title, key)
    await call.answer()


@router.callback_query(F.data == "pn:repcustom")
async def cb_report_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Panel.period)
    await safe_edit(
        call,
        "📅 <b>Свой период</b>\n\n"
        "<blockquote>Пришлите две даты через пробел:\n"
        "<code>01.08 15.08</code>\n\n"
        "Год можно не писать. Одна дата — отчёт за этот день."
        "</blockquote>",
        back_kb("pn:rep", "‹ Назад"),
    )
    await call.answer()


@router.message(Panel.period, F.text)
async def on_report_period(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    parsed = reports.parse_range(message.text or "")
    if parsed is None:
        await message.answer(
            "❌ Не разобрал даты. Формат: <code>01.08 15.08</code>"
        )
        return

    start, end = parsed
    await state.clear()
    since, until = reports.bounds(start, end)
    data = await db.report(conn, since, until)
    days = await db.daily_series(conn, since, until, reports.tz_hours())
    by_product = await db.report_by_product(conn, since, until)
    title = (f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
             if start != end else start.strftime("%d.%m.%Y"))
    await message.answer(
        substitute(format_report(title, data, days, by_product=by_product)),
        reply_markup=report_kb(),
    )


@router.callback_query(F.data == "pn:autoprice")
async def cb_autoprice(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Включить или выключить автоматическое обновление цен."""
    if runtime.auto_price_on():
        await runtime.set_value(conn, "auto_price", "0")
        await call.answer("Автоцены выключены")
        await safe_edit(call, prices_text(), prices_kb())
        return

    # Включаем только после успешного пробного обновления: иначе владелец
    # решит, что цены обновляются, а они молча стоят.
    await safe_edit(call, "📡 Проверяю, получится ли обновить цены…",
                    back_kb("pn:prices"))
    await call.answer()

    result = await pricing.refresh_once(conn, provider)
    if not result["ok"]:
        await safe_edit(
            call,
            f"❌ <b>Автоцены не включить</b>\n\n"
            f"<blockquote>{result['reason']}</blockquote>\n\n"
            "Задайте курс доллара и наценку, потом попробуйте снова.",
            back_kb("pn:prices"),
        )
        return

    await runtime.set_value(conn, "auto_price", "1")
    changed = "\n".join(
        f"├ {name}: {fmt(old)} → <b>{fmt(new)}</b>"
        for name, old, new, _ in result["changed"]
    ) or "├ цены уже верные"
    await safe_edit(
        call,
        "🟢 <b>Автоцены включены</b>\n\n"
        f"{changed}\n\n"
        f"<blockquote>Бот будет спрашивать цену каждые "
        f"{runtime.get_int('auto_price_every', 60)} мин. и держать вашу "
        f"наценку {runtime.margin_percent()}%. О заметных скачках предупредит."
        "</blockquote>",
        prices_kb(),
    )


@router.callback_query(F.data == "pn:dctest")
async def cb_dc_test(call: CallbackQuery) -> None:
    """Показать готовую ссылку на пробную сумму — проверить, что открывается."""
    if not dcpay.is_ready():
        await safe_edit(
            call,
            "🏙 <b>Кнопка оплаты не настроена</b>\n\n"
            "<blockquote>Возьмите свою ссылку вида\n"
            "<code>pay.dc.tj/?a=НОМЕР...&amp;c=...&amp;f1=133&amp;s=50</code>\n\n"
            "и впишите из неё номер счёта — это параметр <code>a</code>."
            "</blockquote>",
            back_kb("pn:set:dc_account", "🏙 Вписать счёт"),
        )
        await call.answer()
        return

    reference = dcpay.make_reference()
    link = dcpay.build_link(
        dcpay.account(), 5000,
        dcpay.build_comment(dcpay.comment_prefix(), reference),
        dcpay.service(),
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn("🏙 Открыть (проба на 50 с.)", url=link, style=SUCCESS))
    kb.row(btn("‹ Назад", "pn:pay"))
    await safe_edit(
        call,
        "🏙 <b>Проверка кнопки оплаты</b>\n\n"
        "<blockquote>Нажмите кнопку ниже: должно открыться приложение "
        "со счётом и суммой 50 сомони. Платить не нужно — просто "
        "убедитесь, что данные подставились.</blockquote>\n\n"
        f"Ссылка:\n<code>{link}</code>",
        kb.as_markup(),
    )
    await call.answer()


# ═════════════════════════════════════════════════════════════ клиенты


def user_card(user: db.User, stats: dict, history: list, spent: int = 0, last: str = "") -> str:
    name = f"@{user.username}" if user.username else (user.first_name or "без имени")
    lines = "\n".join(
        f"├ {'+' if adj.amount > 0 else '−'}{fmt(abs(adj.amount))}"
        + (f" — <i>{adj.reason}</i>" if adj.reason else "")
        for adj in history[:5]
    )
    manual = f"\n\n✍️ <b>Правки баланса</b>\n{lines}" if history else ""

    return (
        f"👤 <b>{name}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ ID: <code>{user.id}</code>\n"
        f"├ С нами с {user.created_at[:10]}\n"
        f"└ Пришёл: <b>{user.source or 'сам'}</b>\n\n"
        f"[[money]] <b>Финансы</b>\n"
        f"├ Баланс: <b>{fmt(user.balance)}</b>\n"
        f"├ Пополнено всего: <b>{fmt(user.total_deposit)}</b>\n"
        f"└ Потрачено на покупки: <b>{fmt(spent)}</b>\n\n"
        f"📦 <b>Заказы</b>\n"
        f"├ Всего: <b>{stats['total']}</b>, выполнено: <b>{stats['done']}</b>\n"
        f"├ Звёзд куплено: <b>{stats['stars']}</b>\n"
        f"└ Последняя покупка: <b>{_when(last) if last else '—'}</b>\n\n"
        + (f"[[block]] <b>Заблокирован</b>" if user.is_banned else "")
        + manual
    )


def user_kb(user: db.User) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("➕ Начислить", f"pn:give:{user.id}", style=SUCCESS),
        btn("➖ Списать", f"pn:take:{user.id}", style=DANGER),
    )
    kb.row(btn("📜 Вся история: пополнения и покупки", f"pn:uhist:{user.id}:0", style=PRIMARY))
    kb.row(btn(
        "✅ Разблокировать" if user.is_banned else "🚫 Заблокировать",
        f"pn:ban:{user.id}",
        style=SUCCESS if user.is_banned else DANGER,
    ))
    kb.row(btn("🔍 Другой клиент", "pn:users"))
    kb.row(btn("‹ В панель", "pn:home"))
    return kb.as_markup()


async def show_user(call: CallbackQuery, conn: aiosqlite.Connection, user_id: int) -> None:
    user = await db.get_user(conn, user_id)
    if user is None:
        await safe_edit(call, "Клиент не найден.", back_kb("pn:users", "🔍 Искать"))
        return
    stats = await db.user_order_stats(conn, user_id)
    history = await db.list_adjustments(conn, user_id=user_id)
    spent, last = await db.user_spent(conn, user_id)
    await safe_edit(call, substitute(user_card(user, stats, history, spent, last)), user_kb(user))


@router.callback_query(F.data == "pn:users")
async def cb_users(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.set_state(Panel.user_search)
    recent = await db.list_adjustments(conn, limit=5)
    tail = ""
    if recent:
        rows = "\n".join(
            f"├ <code>{a.user_id}</code>: "
            f"{'+' if a.amount > 0 else '−'}{fmt(abs(a.amount))}"
            + (f" — <i>{a.reason}</i>" if a.reason else "")
            for a in recent
        )
        tail = f"\n\n✍️ <b>Последние правки</b>\n{rows}"

    kb = InlineKeyboardBuilder()
    kb.row(btn("📥 Перенести балансы со старого бота", "pn:transfer"))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))

    await safe_edit(
        call,
        "👥 <b>Клиенты</b>\n\n"
        "<blockquote>Пришлите <b>имя</b>, <b>@ник</b> или <b>ID</b> клиента — покажу карточку: "
        "баланс, все пополнения и покупки, кнопки «Начислить», «Списать», «Заблокировать».</blockquote>" + tail,
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:admins")
async def cb_admins(call: CallbackQuery, state: FSMContext,
                    conn: aiosqlite.Connection) -> None:
    """Кому открыта панель. Раздаёт доступ только владелец."""
    await state.clear()
    me = call.from_user.id
    owner = access.is_owner(me)

    lines = []
    for uid in access.owners():
        user = await db.get_user(conn, uid)
        who = f" · @{user.username}" if user and user.username else ""
        lines.append(f"👑 <code>{uid}</code>{who} <i>— владелец</i>")
    for uid in access.extra():
        user = await db.get_user(conn, uid)
        who = f" · @{user.username}" if user and user.username else ""
        lines.append(f"👮 <code>{uid}</code>{who}")

    kb = InlineKeyboardBuilder()
    if owner:
        kb.row(btn("➕ Дать доступ по ID", "pn:admin_add", style=SUCCESS))
        for uid in access.extra():
            kb.row(InlineKeyboardButton(
                text=f"🗑 Забрать у {uid}", callback_data=f"pn:admin_del:{uid}"))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))

    tail = (
        "<blockquote>👑 <b>Владелец</b> — тот, чей ID стоит в настройках "
        "на сервере. Его нельзя снять отсюда: иначе бота можно потерять "
        "целиком, ошибившись человеком один раз.\n\n"
        "👮 <b>Админ</b> — может всё то же самое: заявки, заказы, цены, "
        "балансы клиентов, рассылка. Не может одного — раздавать "
        "доступ.</blockquote>"
    )
    if not owner:
        tail += ("\n\n<i>Менять список может только владелец. Свой ID "
                 "владельца задают на сервере: "
                 "<code>sudo stars-bot telegram</code>.</i>")

    await safe_edit(
        call,
        "👮 <b>Доступ к панели</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + "\n".join(lines) + "\n\n" + tail,
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:admin_add")
async def cb_admin_add(call: CallbackQuery, state: FSMContext) -> None:
    if not access.is_owner(call.from_user.id):
        await call.answer("Раздавать доступ может только владелец.",
                          show_alert=True)
        return
    await state.set_state(Panel.admin_add)
    await safe_edit(
        call,
        "➕ <b>Дать доступ к панели</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<blockquote>Пришлите <b>ID</b> человека — только цифры.\n\n"
        "Свой ID он узнает у @userinfobot: перешлёт туда любое своё "
        "сообщение или просто нажмёт «старт».\n\n"
        "[[warn]] Он получит полный доступ: заявки, заказы, цены, "
        "балансы клиентов и рассылка. Давайте только тому, кому "
        "доверяете деньги.</blockquote>",
        back_kb("pn:admins", "❌ Отмена"),
    )
    await call.answer()


@router.message(Panel.admin_add, F.text)
async def on_admin_add(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    if not access.is_owner(message.from_user.id):
        await state.clear()
        await message.answer("Раздавать доступ может только владелец.")
        return

    raw = (message.text or "").strip().lstrip("@")
    if not raw.isdigit() or not 5 <= len(raw) <= 15:
        await message.answer(
            "❌ Нужен <b>числовой ID</b>, например <code>123456789</code>.\n\n"
            "<blockquote>По юзернейму доступ дать нельзя: его можно "
            "сменить, и тогда доступ уедет к чужому человеку. ID не "
            "меняется никогда.</blockquote>"
        )
        return

    user_id = int(raw)
    if not await access.grant(conn, user_id):
        await state.clear()
        await message.answer(
            f"У <code>{user_id}</code> доступ уже есть.",
            reply_markup=back_kb("pn:admins", "‹ К списку"),
        )
        return

    await state.clear()
    user = await db.get_user(conn, user_id)
    who = f" (@{user.username})" if user and user.username else ""
    await message.answer(
        f"✅ <b>Доступ выдан</b>\n\n"
        f"👮 <code>{user_id}</code>{who}\n\n"
        "<blockquote>Пусть откроет бота и отправит <code>/panel</code>.\n\n"
        "Забрать доступ можно там же, где выдали.</blockquote>",
        reply_markup=back_kb("pn:admins", "‹ К списку"),
    )

    # Команда /panel должна появиться у него в меню сейчас, а не после
    # ближайшего перезапуска бота.
    await access.apply_menu(bot, user_id)

    # Человек должен узнать, что доступ у него есть, — иначе он о нём
    # просто не догадается.
    try:
        await bot.send_message(
            user_id,
            "👮 <b>Вам открыт доступ к админ-панели</b>\n\n"
            "<blockquote>Откройте её командой <code>/panel</code>.\n\n"
            "Там заявки на пополнение, заказы, цены и балансы клиентов — "
            "будьте внимательны, это живые деньги.</blockquote>",
        )
    except TelegramAPIError as exc:
        log.info("Доступ: не смог написать %s — %s", user_id, exc)
        await message.answer(
            "<i>Написать ему не удалось — он ещё не открывал бота. "
            "Доступ всё равно выдан.</i>"
        )


@router.callback_query(F.data.startswith("pn:admin_del:"))
async def cb_admin_del(call: CallbackQuery, state: FSMContext,
                       conn: aiosqlite.Connection) -> None:
    if not access.is_owner(call.from_user.id):
        await call.answer("Забирать доступ может только владелец.",
                          show_alert=True)
        return

    user_id = int(call.data.split(":", 2)[2])
    if await access.revoke(conn, user_id):
        # Убрать /panel из его меню команд: Telegram держит список у
        # себя, и без этого команда осталась бы у снятого админа.
        await access.apply_menu(call.bot, user_id)
        await call.answer(f"Доступ у {user_id} забран")
    else:
        await call.answer("У этого человека доступ снять нельзя.",
                          show_alert=True)
    await cb_admins(call, state, conn)


@router.callback_query(F.data == "pn:transfer")
async def cb_transfer(call: CallbackQuery, state: FSMContext) -> None:
    """Перенос балансов: принимаем список в любом виде."""
    await state.set_state(Panel.transfer)
    await safe_edit(
        call,
        "📥 <b>Перенос балансов</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<blockquote>Пришлите <b>файл</b> со списком — Excel "
        "(<code>.xlsx</code>) или <code>.csv</code>.\n\n"
        "В нём должны быть колонка с <b>ID</b> и колонка с "
        "<b>остатком</b> — по заголовкам я их найду сам. Колонку "
        "«потрачено» не трону.\n\n"
        "Можно и просто вставить список текстом, если файла нет.\n\n"
        "Сразу ничего не применю — сначала покажу, что понял.</blockquote>",
        back_kb("pn:users", "❌ Отмена"),
    )
    await call.answer()


@router.message(Panel.transfer, F.document)
async def on_transfer_file(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    """Список файлом: Excel или CSV."""
    from app.services import importer, sheets

    document = message.document
    if document.file_size and document.file_size > sheets.MAX_BYTES:
        await message.answer("❌ Файл слишком большой.")
        return

    notice = await message.answer("📥 Читаю файл…")
    try:
        buffer = await bot.download(document)
        rows = sheets.read(buffer.read(), document.file_name or "")
    except sheets.SheetError as exc:
        await notice.edit_text(
            f"❌ <b>Файл не прочитался</b> — {exc}.\n\n"
            "<blockquote>Подойдёт Excel (<code>.xlsx</code>) или "
            "<code>.csv</code>. Файл <code>.xls</code> старого образца "
            "не читается — пересохраните его как <code>.xlsx</code>."
            "</blockquote>"
        )
        return
    except Exception as exc:  # noqa: BLE001 — показать владельцу причину
        log.warning("Перенос: файл не прочитался — %s", exc)
        await notice.edit_text(f"❌ Файл не прочитался: <code>{str(exc)[:150]}</code>")
        return

    pairs, skipped = importer.parse_rows(rows)
    await _transfer_preview(notice, state, pairs, skipped,
                            source=document.file_name or "файл", edit=True)


@router.message(Panel.transfer, F.text)
async def on_transfer_list(message: Message, state: FSMContext) -> None:
    from app.services import importer

    pairs, skipped = importer.parse_balances(message.text or "")
    await _transfer_preview(message, state, pairs, skipped, source="список")


async def _transfer_preview(
    message: Message, state: FSMContext, pairs: list, skipped: list,
    source: str = "", edit: bool = False,
) -> None:
    """Показать, что понято, и спросить подтверждения. Ничего не меняет.

    edit — поправить своё же сообщение «Читаю файл…» вместо нового:
    после файла ответ должен встать на место ожидания, а не под ним.
    """
    from app.services import importer

    say = message.edit_text if edit else message.answer

    if not pairs:
        await say(
            "❌ <b>Ни одной записи не понял</b>\n\n"
            "<blockquote>Нужна колонка с ID (от пяти цифр) и колонка "
            "с остатком. В тексте — ID и сумма в строке:\n"
            "<code>123456789 100</code></blockquote>"
        )
        return

    rich = importer.with_money(pairs)
    total = sum(row["amount"] for row in pairs)
    rows = "\n".join(
        f"├ <code>{row['id']}</code> → <b>{fmt(row['amount'])}</b>"
        + (f"  @{row['username']}" if row["username"] else "")
        for row in importer.preview(pairs)
    )
    more = (f"\n└ <i>…и ещё {len(pairs) - 5}</i>") if len(pairs) > 5 else ""
    lost = ""
    if skipped:
        sample = "\n".join(f"├ {line[:60]}" for line in skipped[:3])
        lost = (f"\n\n⚠️ <b>Не понял строк: {len(skipped)}</b>\n{sample}"
                + ("\n└ <i>…</i>" if len(skipped) > 3 else ""))

    # Список держим в состоянии: применять будем по кнопке, а не сразу.
    await state.update_data(transfer=pairs)

    kb = InlineKeyboardBuilder()
    if rich and len(rich) != len(pairs):
        kb.row(btn(f"💰 Только с деньгами — {len(rich)}",
                   "pn:transfer_go:rich", style=SUCCESS))
        kb.row(btn(f"👥 Всех — {len(pairs)}", "pn:transfer_go:all"))
    else:
        kb.row(btn(f"✅ Записать {len(pairs)}", "pn:transfer_go:all",
                   style=SUCCESS))
    kb.row(btn("❌ Отмена", "pn:users", style=DANGER))

    zero = len(pairs) - len(rich)
    await say(
        "📥 <b>Проверьте перед записью</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + (f"<i>Из: {source}</i>\n\n" if source else "")
        + f"├ Всего строк: <b>{len(pairs)}</b>\n"
        f"├ С деньгами: <b>{len(rich)}</b>\n"
        f"├ С нулём: <b>{zero}</b>\n"
        f"└ Всего денег: <b>{fmt(total)}</b>\n\n"
        f"<b>Самые крупные:</b>\n{rows}{more}{lost}\n\n"
        "<blockquote>Сверьте суммы с выгрузкой — это главное. Если "
        "колонка взята не та, скажите, и я поправлю разбор.\n\n"
        "Баланс <b>устанавливается</b>, а не прибавляется: запустите "
        "перенос дважды — деньги не удвоятся.\n\n"
        "Кого ещё нет в базе, заведу: он нажмёт «старт» и сразу увидит "
        "свой баланс.</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:transfer_go"))
async def cb_transfer_go(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    from app.services import importer

    data = await state.get_data()
    rows = data.get("transfer") or []
    await state.clear()

    if not rows:
        await call.answer("Список потерялся, пришлите заново.", show_alert=True)
        return

    only_rich = call.data.endswith(":rich")
    if only_rich:
        rows = importer.with_money(rows)

    await call.answer("Записываю…")
    created = 0
    for row in rows:
        if await db.import_balance(conn, int(row["id"]), int(row["amount"]),
                                   username=row.get("username", "")):
            created += 1

    total = sum(row["amount"] for row in rows)
    await safe_edit(
        call,
        "✅ <b>Балансы перенесены</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Записей: <b>{len(rows)}</b>"
        + (" <i>(только с деньгами)</i>" if only_rich else "") + "\n"
        f"├ Новых клиентов: <b>{created}</b>\n"
        f"└ Всего денег: <b>{fmt(total)}</b>\n\n"
        "<blockquote>Клиенты увидят баланс сразу, как откроют бота. "
        "Сообщать им бот ничего не станет — если нужно, отправьте "
        "рассылку.</blockquote>",
        back_kb("pn:users", "‹ К клиентам"),
    )


@router.message(Panel.user_search, F.text)
async def on_user_search(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    found = await db.search_users(conn, message.text or "")
    if not found:
        await message.answer(
            "❌ Никого не нашёл. Пришлите имя, @ник или ID ещё раз.\n\n"
            "<blockquote>Клиент появляется в базе после того, как хотя бы "
            "раз напишет боту.</blockquote>"
        )
        return
    if len(found) > 1:
        kb = InlineKeyboardBuilder()
        for u in found:
            name = f"@{u.username}" if u.username else (u.first_name or "без имени")
            kb.row(InlineKeyboardButton(text=f"👤 {name} · {u.id} · {fmt(u.balance)}",
                                        callback_data=f"pn:user:{u.id}"))
        kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
        await message.answer(f"👥 Нашёл: <b>{len(found)}</b>. Выберите клиента "
                             "(или пришлите запрос точнее):", reply_markup=kb.as_markup())
        return

    user = found[0]
    await state.clear()
    stats = await db.user_order_stats(conn, user.id)
    history = await db.list_adjustments(conn, user_id=user.id)
    spent, last = await db.user_spent(conn, user.id)
    await message.answer(
        substitute(user_card(user, stats, history, spent, last)), reply_markup=user_kb(user)
    )


def _when(stamp: str) -> str:
    """2026-09-25T14:03:11 → 25.09 14:03 (время по Душанбе)."""
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone(timedelta(hours=5)))).strftime("%d.%m.%y %H:%M")


HIST_PAGE = 10
DEP_STATUS = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
ORD_STATUS = {"delivered": "✅", "delivering": "⏳", "failed": "🔍", "refunded": "↩️"}


def _hist_line(row: dict) -> str:
    when = _when(row["created_at"])
    if row["kind"] == "deposit":
        return (f"{DEP_STATUS.get(row['status'], '•')} <b>+{fmt(row['amount'])}</b> пополнение"
                f" · {row['what']}\n    <i>{when}</i>")
    if row["kind"] == "adjust":
        sign = "+" if row["amount"] > 0 else "−"
        return (f"✍️ <b>{sign}{fmt(abs(row['amount']))}</b> вручную"
                + (f" · {row['what']}" if row["what"] else "") + f"\n    <i>{when}</i>")
    recipient, _, qty = (row["extra"] or "").rpartition("|")
    if row["what"] == "stars":
        title = f"⭐ {qty} звёзд"
    elif row["what"] == "premium":
        title = f"👑 Premium {qty} мес."
    elif row["what"] == "steam":
        title = f"🎮 Steam {qty}"
    else:
        title = db.product_title(row["what"]) + (f" × {qty}" if qty and qty != "1" else "")
    return (f"{ORD_STATUS.get(row['status'], '•')} <b>−{fmt(row['amount'])}</b> {title}"
            + (f" → <code>{recipient[:40]}</code>" if recipient else "") + f"\n    <i>{when}</i>")


@router.callback_query(F.data.startswith("pn:uhist:"))
async def cb_user_history(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    """Вся история клиента по страницам: пополнения, покупки, ручные правки — новое сверху."""
    await state.clear()
    _, _, raw_id, raw_page = call.data.split(":")
    user_id, page = int(raw_id), max(int(raw_page), 0)
    user = await db.get_user(conn, user_id)
    if user is None:
        await call.answer("Клиент не найден.", show_alert=True)
        return
    rows, total = await db.user_timeline(conn, user_id, HIST_PAGE, page * HIST_PAGE)
    pages = max(1, -(-total // HIST_PAGE))
    name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    body = "\n".join(_hist_line(r) for r in rows) if rows else "<i>Пока ничего: ни пополнений, ни покупок.</i>"
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ Новее", callback_data=f"pn:uhist:{user_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Старее ›", callback_data=f"pn:uhist:{user_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(btn("👤 Карточка клиента", f"pn:user:{user_id}"))
    await safe_edit(call, f"📜 <b>История {name}</b> · {total} записей · стр. {page + 1}/{pages}\n"
                          f"<code>{texts.LINE}</code>\n\n{body}\n\n"
                          "<i>✅ выполнено · ⏳ в процессе · ❌ отклонено · ↩️ деньги возвращены</i>",
                    kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith(("pn:give:", "pn:take:")))
async def cb_adjust_start(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    kind, user_id = call.data.split(":")[1], int(call.data.rsplit(":", 1)[1])
    user = await db.get_user(conn, user_id)
    if user is None:
        await call.answer("Клиент не найден.", show_alert=True)
        return

    await state.set_state(Panel.adjust)
    await state.update_data(adjust_user=user_id, adjust_kind=kind)

    if kind == "give":
        head = "➕ <b>Начислить на баланс</b>"
        note = "Деньги появятся у клиента сразу, он получит уведомление."
    else:
        head = "➖ <b>Списать с баланса</b>"
        note = ("Списать больше, чем есть, нельзя — баланс не уйдёт в минус.")

    await safe_edit(
        call,
        f"{head}\n\n"
        f"├ Клиент: <code>{user_id}</code>\n"
        f"└ Сейчас на балансе: <b>{fmt(user.balance)}</b>\n\n"
        f"<blockquote>{note}</blockquote>\n\n"
        "Пришлите сумму в сомони. Можно с причиной через пробел:\n"
        "<code>50</code>  или  <code>50 бонус за отзыв</code>",
        back_kb(f"pn:user:{user_id}", "✖️ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:user:"))
async def cb_user_card(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_user(call, conn, int(call.data.rsplit(":", 1)[1]))
    await call.answer()


@router.message(Panel.adjust, F.text)
async def on_adjust_amount(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, bot: Bot
) -> None:
    data = await state.get_data()
    user_id, kind = data.get("adjust_user"), data.get("adjust_kind")
    if not user_id:
        await state.clear()
        await message.answer("Не понял, кому. Откройте /panel заново.")
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    amount = parse(parts[0]) if parts else None
    reason = parts[1].strip() if len(parts) > 1 else ""

    if amount is None or amount <= 0:
        await message.answer(
            "❌ Введите сумму числом: <code>50</code> или <code>50.50</code>.\n"
            "Причину можно дописать через пробел."
        )
        return

    user = await db.get_user(conn, user_id)
    if user is None:
        await state.clear()
        await message.answer("Клиент пропал из базы.")
        return

    if kind == "give":
        await db.credit(conn, user_id, amount)
        signed = amount
        await delivery.notify(
            bot, user_id,
            substitute(
                f"[[money]] <b>Вам начислено {fmt(amount)}</b>"
                + (f"\n\n<blockquote>{reason}</blockquote>" if reason else "")
            ),
        )
    else:
        if not await db.charge(conn, user_id, amount):
            await message.answer(
                f"❌ Не хватает средств: на балансе <b>{fmt(user.balance)}</b>.\n\n"
                "<blockquote>Баланс не уводится в минус — иначе клиент ушёл бы "
                "в долг, которого он не брал.</blockquote>"
            )
            return
        signed = -amount
        await delivery.notify(
            bot, user_id,
            substitute(
                f"[[refund]] <b>С баланса списано {fmt(amount)}</b>"
                + (f"\n\n<blockquote>{reason}</blockquote>" if reason else "")
            ),
        )

    await db.add_adjustment(
        conn, user_id=user_id, admin_id=message.from_user.id,
        amount=signed, reason=reason,
    )
    await state.clear()

    fresh = await db.get_user(conn, user_id)
    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Карточка клиента", f"pn:user:{user_id}"))
    kb.row(btn("‹ В панель", "pn:home"))
    await message.answer(
        f"✅ <b>{'Начислено' if kind == 'give' else 'Списано'} {fmt(amount)}</b>\n\n"
        f"├ Клиент: <code>{user_id}</code>\n"
        f"└ Баланс теперь: <b>{fmt(fresh.balance if fresh else 0)}</b>"
        + (f"\n\n<i>Причина: {reason}</i>" if reason else ""),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:ban:"))
async def cb_ban_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    user_id = int(call.data.rsplit(":", 1)[1])
    user = await db.get_user(conn, user_id)
    if user is None:
        await call.answer("Клиент не найден.", show_alert=True)
        return
    await db.set_banned(conn, user_id, not user.is_banned)
    await call.answer("Разблокирован" if user.is_banned else "Заблокирован")
    await show_user(call, conn, user_id)


# ═════════════════════════════════════════════════════════ Deep Links


def links_text(rows: list[tuple[db.Link, dict]]) -> str:
    if not rows:
        body = (
            "<blockquote>Пока ни одной. Нажмите «Создать ссылку» и придумайте "
            "название — например <code>instagram</code>, <code>reklama1</code> "
            "или <code>partner_1</code>.</blockquote>"
        )
    else:
        body = "\n".join(
            f"├ <code>{link.code}</code> — переходов <b>{stats['hits']}</b>, "
            f"новых <b>{stats['fresh']}</b>"
            for link, stats in rows
        )
    return (
        "🔗 <b>Рекламные ссылки</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{body}\n\n"
        "<blockquote>Каждой площадке — своя ссылка. Бот сам запомнит, откуда "
        "пришёл клиент, и покажет, какая реклама приносит покупателей.</blockquote>"
    )


def links_kb(rows: list[tuple[db.Link, dict]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("➕ Создать ссылку", "pn:link:new", style=SUCCESS))
    for link, _ in rows[:20]:
        kb.row(InlineKeyboardButton(
            text=f"🔗 {link.code}", callback_data=f"pn:link:{link.id}",
        ))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


def link_card(link: db.Link, stats: dict, url: str) -> str:
    conversion = ""
    if stats["people"]:
        share = round(stats["buyers"] * 100 / stats["people"])
        conversion = f"\n└ Из перешедших купили: <b>{share}%</b>"

    return (
        f"🔗 <b>{link.code}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"<code>{url}</code>\n\n"
        "📊 <b>Переходы</b>\n"
        f"├ Всего запусков: <b>{stats['hits']}</b>\n"
        f"├ Уникальных людей: <b>{stats['people']}</b>\n"
        f"└ Новых пользователей: <b>{stats['fresh']}</b>\n\n"
        "[[money]] <b>Отдача</b>\n"
        f"├ Покупателей: <b>{stats['buyers']}</b>\n"
        f"├ Куплено на: <b>{fmt(stats['revenue'])}</b>"
        f"{conversion}\n\n"
        f"📅 Создана {link.created_at[:10]}\n\n"
        "<blockquote>Telegram сообщает боту не о самом клике, а о запуске: "
        "человек открыл ссылку и нажал «Запустить».</blockquote>"
    )


def link_kb(link: db.Link, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="📋 Копировать ссылку", copy_text=CopyTextButton(text=url),
    ))
    kb.row(btn("🗑 Удалить", f"pn:link:del:{link.id}", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ К ссылкам", callback_data="pn:links"))
    return kb.as_markup()


async def show_links(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    rows = await db.list_links(conn)
    await safe_edit(call, links_text(rows), links_kb(rows))


async def show_link(call: CallbackQuery, conn: aiosqlite.Connection, link_id: int) -> bool:
    link = await db.get_link(conn, link_id)
    if link is None:
        await call.answer("Ссылка удалена.", show_alert=True)
        await show_links(call, conn)
        return False
    stats = await db.link_stats(conn, link.id)
    url = links.build(await links.bot_username(call.bot), link.code)
    await safe_edit(call, substitute(link_card(link, stats, url)), link_kb(link, url))
    return True


@router.callback_query(F.data == "pn:links")
async def cb_links(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_links(call, conn)
    await call.answer()


@router.callback_query(F.data == "pn:link:new")
async def cb_link_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Panel.link)
    await safe_edit(
        call,
        "➕ <b>Новая ссылка</b>\n\n"
        "<blockquote>Пришлите название — оно встанет в конец ссылки.\n\n"
        "Например <code>instagram</code>, <code>reklama1</code>, "
        "<code>partner_1</code>.\n\n"
        "Только латиница, цифры, дефис и подчёркивание.</blockquote>",
        back_kb("pn:links", "‹ Отмена"),
    )
    await call.answer()


@router.message(Panel.link, F.text)
async def on_link_name(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    code = (message.text or "").strip().lstrip("@")
    problem = links.check(code)
    if problem:
        await message.answer(f"❌ {problem}\n\n<i>Попробуйте другое название.</i>")
        return

    link = await db.create_link(conn, code)
    if link is None:
        await message.answer(
            f"❌ Ссылка <code>{code}</code> уже есть.\n\n"
            "<i>Придумайте другое название.</i>"
        )
        return

    await state.clear()
    url = links.build(await links.bot_username(message.bot), code)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="📋 Копировать ссылку", copy_text=CopyTextButton(text=url),
    ))
    kb.row(InlineKeyboardButton(text="📊 Статистика", callback_data=f"pn:link:{link.id}"))
    kb.row(InlineKeyboardButton(text="‹ К ссылкам", callback_data="pn:links"))
    await message.answer(
        f"✅ <b>Ссылка готова</b>\n\n"
        f"<code>{url}</code>\n\n"
        "<blockquote>Ставьте её в рекламу. Каждый, кто запустит бота по этой "
        "ссылке, попадёт в её статистику.</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:link:del:"))
async def cb_link_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Первое нажатие — предупреждение, второе — удаление."""
    link_id = int(call.data.rsplit(":", 1)[1])
    link = await db.get_link(conn, link_id)
    if link is None:
        await call.answer("Уже удалена.", show_alert=True)
        await show_links(call, conn)
        return

    stats = await db.link_stats(conn, link_id)
    kb = InlineKeyboardBuilder()
    kb.row(btn("🗑 Да, удалить", f"pn:link:kill:{link_id}", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ Отмена", callback_data=f"pn:link:{link_id}"))
    await safe_edit(
        call,
        f"🗑 <b>Удалить ссылку {link.code}?</b>\n\n"
        f"<blockquote>Пропадёт её статистика: <b>{stats['hits']}</b> переходов "
        f"и <b>{stats['fresh']}</b> новых пользователей. Сами клиенты и их "
        "заказы останутся на месте.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:link:kill:"))
async def cb_link_kill(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    link_id = int(call.data.rsplit(":", 1)[1])
    await db.delete_link(conn, link_id)
    await call.answer("Удалена")
    await show_links(call, conn)


@router.callback_query(F.data.startswith("pn:link:"))
async def cb_link_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    await show_link(call, conn, int(call.data.rsplit(":", 1)[1]))
    await call.answer()


# ═════════════════════════════════════════════════════════════ отзывы


def reviews_text(stats: dict, pending: list, waiting: int = 0) -> str:
    channel = reviews_service.channel() or "не задан"
    average = (stats["rating_sum"] / stats["total"]) if stats["total"] else 0

    tail = ""
    if waiting:
        tail = (f"\n\n📣 <b>Ещё не спрашивали: {waiting}</b>\n"
                "└ <i>Это те, кто покупал до появления отзывов</i>")

    body = ""
    if pending:
        rows = "\n".join(
            f"├ {review.stars} — <i>{(review.text or 'без текста')[:60]}</i>"
            for review in pending[:5]
        )
        body = f"\n\n⏳ <b>Ждут проверки: {len(pending)}</b>\n{rows}"

    return (
        "⭐️ <b>Отзывы</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{'✅' if runtime.get_bool('reviews_on') else '🚫'} Спрашивать после заказа\n"
        f"📣 Канал: <b>{channel}</b>\n\n"
        f"├ Опубликовано: <b>{stats['published']}</b>\n"
        + (f"└ Средняя оценка: <b>{average:.1f}</b> из 5"
           if stats["total"] else "└ Оценок пока нет")
        + body
        + tail
        + "\n\n<blockquote>Отзыв приходит вам на проверку и попадает в канал "
          "только после кнопки «Опубликовать».</blockquote>"
    )


def reviews_kb(pending: list, waiting: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    on = runtime.get_bool("reviews_on")
    kb.row(btn("🚫 Не спрашивать отзывы" if on else "✅ Спрашивать отзывы",
               "pn:rev_toggle", style=DANGER if on else SUCCESS))
    kb.row(InlineKeyboardButton(text="📣 Канал отзывов",
                                callback_data="pn:set:reviews_channel"))
    if pending:
        kb.row(btn(f"⏳ Показать на проверке ({len(pending)})",
                   "pn:rev_pending", style=PRIMARY))
    if waiting:
        kb.row(btn(f"📣 Спросить у прошлых покупателей ({waiting})",
                   "pn:rev_ask", style=PRIMARY))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


async def show_reviews(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    stats = await db.review_stats(conn)
    pending = await db.list_reviews(conn, status=db.REVIEW_PENDING)
    waiting = len(await db.review_targets(conn))
    await safe_edit(call, reviews_text(stats, pending, waiting),
                    reviews_kb(pending, waiting))


# ═══════════════════════════════════════ обязательная подписка на канал


def sponsor_text() -> str:
    from app.services import sponsor

    names = sponsor.channels()
    if not names:
        where = "Канал не задан — проверка не работает, даже если включена."
    else:
        where = "Каналы:\n" + "\n".join(f"├ <code>{n}</code>" for n in names)

    return (
        "📢 <b>Обязательная подписка</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"Состояние: <b>{'🟢 включена' if sponsor.on() else '⚪️ выключена'}</b>\n\n"
        f"{where}\n\n"
        "<blockquote>Пока клиент не подписан, бот его не обслуживает: "
        "вместо меню он видит кнопку «Подписаться».\n\n"
        "Бот должен быть администратором канала — иначе проверить "
        "подписку он не сможет. Если проверка не удаётся, клиентов "
        "пропускаем: одна опечатка в настройке не должна закрывать "
        "магазин для всех.</blockquote>"
    )


def sponsor_kb() -> InlineKeyboardMarkup:
    from app.services import sponsor

    on = sponsor.on()
    kb = InlineKeyboardBuilder()
    kb.row(btn(("🚫 Выключить проверку" if on else "✅ Включить проверку"),
               "pn:sponsor_on", style=DANGER if on else SUCCESS))
    kb.row(InlineKeyboardButton(text="📢 Задать канал",
                                callback_data="pn:set:sponsor_channel"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:sponsor")
async def cb_sponsor(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call, sponsor_text(), sponsor_kb())
    await call.answer()


@router.callback_query(F.data == "pn:sponsor_on")
async def cb_sponsor_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    from app.services import sponsor

    if not runtime.get_bool("sponsor_on") and not sponsor.channels():
        await call.answer("Сначала задайте канал.", show_alert=True)
        return

    was = runtime.get_bool("sponsor_on")
    await runtime.set_value(conn, "sponsor_on", "0" if was else "1")
    # Память о подписках держится несколько минут. После правки настроек
    # она врёт, поэтому забываем всё: лучше лишний запрос в Telegram,
    # чем клиент, которого пускают по старой памяти.
    sponsor.forget()
    await call.answer("Проверка выключена" if was else "Проверка включена")
    await safe_edit(call, sponsor_text(), sponsor_kb())


@router.callback_query(F.data == "pn:reviews")
async def cb_reviews(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_reviews(call, conn)
    await call.answer()


@router.callback_query(F.data == "pn:rev_toggle")
async def cb_reviews_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    on = runtime.get_bool("reviews_on")
    await runtime.set_value(conn, "reviews_on", "0" if on else "1")
    await call.answer("Не спрашиваем" if on else "Спрашиваем")
    await show_reviews(call, conn)


@router.callback_query(F.data == "pn:rev_pending")
async def cb_reviews_pending(call: CallbackQuery, conn: aiosqlite.Connection, bot: Bot) -> None:
    """Переслать себе всё, что ждёт проверки, — с кнопками решения."""
    pending = await db.list_reviews(conn, status=db.REVIEW_PENDING)
    if not pending:
        await call.answer("Всё разобрано.", show_alert=True)
        return
    for review in pending[:10]:
        await reviews_service.to_moderation(bot, conn, review)
    await call.answer(f"Прислал {min(len(pending), 10)} шт.")



@router.callback_query(F.data == "pn:rev_ask")
async def cb_reviews_ask(call: CallbackQuery, conn: aiosqlite.Connection, bot: Bot) -> None:
    """Разослать просьбу об отзыве тем, кто покупал раньше."""
    waiting = await db.review_targets(conn)
    if not waiting:
        await call.answer("Спрашивать некого — всех уже спросили.", show_alert=True)
        return

    await call.answer("Рассылаю…")
    await safe_edit(
        call,
        f"📣 <b>Спрашиваю отзывы</b>\n\n"
        f"<blockquote>Пишу <b>{len(waiting)}</b> покупателям. "
        "Займёт около минуты.</blockquote>",
        back_kb("pn:reviews", "‹ К отзывам"),
    )

    report = await reviews_service.ask_past_buyers(bot, conn)
    await safe_edit(
        call,
        "📣 <b>Просьбы разосланы</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Написали: <b>{report['sent']}</b>\n"
        f"├ Заблокировали бота: <b>{report['blocked']}</b>\n"
        f"└ Не доставлено: <b>{report['failed']}</b>\n\n"
        "<blockquote>Отзывы будут приходить сюда на проверку по мере того, "
        "как люди их оставят. Повторно этих же людей бот не побеспокоит.</blockquote>",
        back_kb("pn:reviews", "‹ К отзывам"),
    )


# ═════════════════════════════════════════════════════════════════ Steam


def steam_text() -> str:
    price, cost = runtime.steam_price_e4(), runtime.steam_cost_e4()
    currency = runtime.steam_currency()
    packs = runtime.steam_packs()

    profit = ""
    if price > 0 and cost > 0:
        profit = (f"\n└ Прибыль с единицы: <b>{fmt4(price - cost)}</b>"
                  f" ({round((price - cost) * 100 / cost)}%)")

    rows = "\n".join(
        f"├ {amount} {currency} — <b>{fmt(steam_cost(amount))}</b>"
        for amount in packs
    ) or "<i>суммы не заданы</i>"

    return (
        "🎮 <b>Пополнение Steam</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{'✅' if runtime.steam_on() else '🚫'} Раздел в меню клиента\n"
        f"💱 Валюта кошелька: <b>{currency}</b>\n"
        f"├ Цена продажи: <b>{fmt4(price)}</b> за 1 {currency}\n"
        + (f"├ Себестоимость: <b>{fmt4(cost)}</b>" if cost else
           "├ <i>себестоимость не задана</i>")
        + profit
        + f"\n\n<b>Суммы кнопками</b>\n{rows}\n\n"
        "<blockquote>Логин Steam бот проверяет до оплаты и без "
        "подтверждения заказ не создаёт: вернуть деньги с чужого "
        "аккаунта нельзя.</blockquote>"
    )


def steam_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    on = runtime.get_bool("steam_enabled")
    kb.row(btn("🚫 Выключить раздел" if on else "✅ Включить раздел",
               "pn:steam_toggle", style=DANGER if on else SUCCESS))
    kb.row(btn("📡 Узнать курс Steam", "pn:steam_rate", style=PRIMARY))
    kb.row(
        InlineKeyboardButton(text="🏷 Цена продажи",
                             callback_data="pn:set:steam_price_e4"),
        InlineKeyboardButton(text="💲 Себестоимость",
                             callback_data="pn:set:steam_cost_e4"),
    )
    kb.row(
        InlineKeyboardButton(text="💱 Валюта", callback_data="pn:set:steam_currency"),
        InlineKeyboardButton(text="🔢 Суммы", callback_data="pn:set:steam_packs"),
    )
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:steam")
async def cb_steam(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call, steam_text(), steam_kb())
    await call.answer()


@router.callback_query(F.data == "pn:steam_toggle")
async def cb_steam_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    on = runtime.get_bool("steam_enabled")
    if not on and runtime.steam_price_e4() <= 0:
        await call.answer("Сначала задайте цену продажи.", show_alert=True)
        return
    await runtime.set_value(conn, "steam_enabled", "0" if on else "1")
    await call.answer("Выключен" if on else "Включён")
    await safe_edit(call, steam_text(), steam_kb())


@router.callback_query(F.data == "pn:steam_rate")
async def cb_steam_rate(call: CallbackQuery, conn: aiosqlite.Connection, provider) -> None:
    """Спросить у сервиса, во что обходится единица пополнения Steam."""
    await safe_edit(call, "📡 Спрашиваю курс Steam…", back_kb("pn:steam", "‹ Назад"))
    await call.answer()

    getter = getattr(provider, "steam_rate", None)
    if getter is None:
        await safe_edit(
            call,
            "📡 <b>Курс Steam недоступен</b>\n\n"
            "<blockquote>Текущий режим выдачи не отдаёт цены Steam. "
            "Задайте себестоимость вручную.</blockquote>",
            back_kb("pn:steam", "‹ Назад"),
        )
        return

    try:
        rate, currency = await getter()
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        await safe_edit(
            call,
            "📡 <b>Курс Steam не пришёл</b>\n\n"
            f"<blockquote expandable>{exc}</blockquote>\n\n"
            "<blockquote>Задайте себестоимость вручную — раздел будет "
            "работать и так.</blockquote>",
            back_kb("pn:steam", "‹ Назад"),
        )
        return

    usd = runtime.usd_rate()
    saved = ""
    if usd > 0:
        cost_e4 = int(rate * usd * 100)
        await runtime.set_value(conn, "steam_cost_e4", str(cost_e4))
        await runtime.set_value(conn, "steam_currency", currency)
        saved = f"\n\n✅ Себестоимость сохранена: <b>{fmt4(cost_e4)}</b> за 1 {currency}"

    await safe_edit(
        call,
        "📡 <b>Курс Steam</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Валюта кошелька: <b>{currency}</b>\n"
        f"└ Цена сервиса: <b>{rate}</b> USD за 1 {currency}"
        + (saved if saved else
           "\n\n<blockquote>Курс доллара не задан — пересчитать в сомони "
           "не могу. Задайте его в разделе «Цены».</blockquote>"),
        back_kb("pn:steam", "‹ Назад"),
    )


# ═══════════════════════════════════════════════════════════ партнёры


def partner_earnings(
    partner: db.Partner, sales: list[dict], owners: dict[str, int], free_profit: int,
) -> dict[str, int]:
    """Что заработал партнёр: прибыль его товаров плюс доля от нераспределённого."""
    mine = [row for row in sales if owners.get(row["product_type"]) == partner.id]
    own = sum(row["profit"] for row in mine)
    share = free_profit * partner.share // 100
    return {
        "own": own, "share": share, "earned": own + share,
        "revenue": sum(row["revenue"] for row in mine),
        "orders": sum(row["orders"] for row in mine),
    }


def partner_line(
    partner: db.Partner, sales: list[dict], owners: dict[str, int],
    free_profit: int, totals: dict,
) -> str:
    """Строка партнёра: его товары, заработок, взносы и что на руках."""
    money = partner_earnings(partner, sales, owners, free_profit)
    balance = money["earned"] + totals["put_in"] - totals["took_out"]

    mine = [row for row in sales if owners.get(row["product_type"]) == partner.id]
    goods = "\n".join(
        f"├ {row['title']}: <b>{row['orders']}</b> шт. на <b>{fmt(row['revenue'])}</b>"
        f" · прибыль <b>{fmt(row['profit'])}</b>"
        for row in mine
    )
    if not mine:
        goods = "├ <i>товары не закреплены</i>"

    share_line = ""
    if money["share"]:
        share_line = (f"├ Доля от общего ({partner.share}%): "
                      f"<b>{fmt(money['share'])}</b>\n")

    return (
        f"👤 <b>{partner.name}</b>\n"
        f"{goods}\n"
        f"├ Заработал: <b>{fmt(money['earned'])}</b>\n"
        f"{share_line}"
        f"├ Внёс в оборот: <b>{fmt(totals['put_in'])}</b>\n"
        f"├ Забрал себе: <b>{fmt(totals['took_out'])}</b>\n"
        f"└ На руках: <b>{fmt(balance)}</b>"
    )


async def partners_text(conn: aiosqlite.Connection) -> str:
    partners = await db.list_partners(conn)
    sales = await db.sales_by_product(conn)
    owners = await db.product_owners(conn)
    money = await db.total_profit(conn)

    free = [row for row in sales if row["product_type"] not in owners]
    free_profit = sum(row["profit"] for row in free)

    if not partners:
        body = (
            "<blockquote>Партнёров пока нет. Добавьте себя и напарника, "
            "закрепите за каждым его товары — и бот начнёт считать, "
            "кто сколько продал.</blockquote>"
        )
    else:
        rows = []
        for partner in partners:
            totals = await db.partner_totals(conn, partner.id)
            rows.append(partner_line(partner, sales, owners, free_profit, totals))
        body = "\n\n".join(rows)

    tail = ""
    if free:
        goods = "\n".join(
            f"├ {row['title']}: <b>{row['orders']}</b> шт. на "
            f"<b>{fmt(row['revenue'])}</b> · прибыль <b>{fmt(row['profit'])}</b>"
            for row in free
        )
        split = ("делится по долям" if any(p.share for p in partners)
                 else "<b>ничей</b> — закрепите за партнёром или поставьте доли")
        tail = f"\n\n📦 <b>Не закреплено</b>\n{goods}\n└ <i>{split}</i>"

    deposits = await db.deposits_total(conn)
    tail += (
        f"\n\n[[deposit]] <b>Пришло на реквизиты</b>\n"
        f"├ Пополнений: <b>{deposits['count']}</b>\n"
        f"└ Всего: <b>{fmt(deposits['total'])}</b>"
    )

    return (
        "🤝 <b>Партнёры</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"[[money]] <b>Общая прибыль: {fmt(money['profit'])}</b>\n"
        f"├ Продано на: <b>{fmt(money['revenue'])}</b>\n"
        f"└ Себестоимость: <b>{fmt(money['cost'])}</b>\n\n"
        f"{body}{tail}\n\n"
        "<blockquote>Прибыль с товара идёт его владельцу. Взносы и выплаты "
        "отмечаете сами — тогда видно, кто сколько вложил и забрал.</blockquote>"
    )


async def partners_kb(conn: aiosqlite.Connection) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("➕ Добавить партнёра", "pn:pt_new", style=SUCCESS))
    for partner in await db.list_partners(conn):
        kb.row(InlineKeyboardButton(
            text=f"👤 {partner.name}", callback_data=f"pn:pt:{partner.id}",
        ))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


async def show_partners(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    await safe_edit(call, substitute(await partners_text(conn)),
                    await partners_kb(conn))


@router.callback_query(F.data == "pn:partners")
async def cb_partners(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_partners(call, conn)
    await call.answer()


# ------------------------------------------------------- карточка партнёра


async def partner_card(conn: aiosqlite.Connection, partner: db.Partner) -> str:
    sales = await db.sales_by_product(conn)
    owners = await db.product_owners(conn)
    totals = await db.partner_totals(conn, partner.id)
    free_profit = sum(row["profit"] for row in sales
                      if row["product_type"] not in owners)
    moves = await db.partner_moves(conn, partner.id)

    history = ""
    if moves:
        rows = "\n".join(
            f"├ {'+' if m['amount'] > 0 else '−'}{fmt(abs(m['amount']))}"
            + (f" — <i>{m['note']}</i>" if m["note"] else "")
            for m in moves[:8]
        )
        history = f"\n\n📜 <b>Движение денег</b>\n{rows}"

    return (
        partner_line(partner, sales, owners, free_profit, totals)
        + f"\n\n<i>С нами с {partner.created_at[:10]}</i>"
        + history
    )


def partner_kb(partner: db.Partner) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("➕ Внёс в оборот", f"pn:pt_in:{partner.id}", style=SUCCESS),
        btn("➖ Забрал себе", f"pn:pt_out:{partner.id}", style=DANGER),
    )
    kb.row(InlineKeyboardButton(text="📦 Товары партнёра",
                                callback_data=f"pn:pt_goods:{partner.id}"))
    kb.row(InlineKeyboardButton(text="📊 Доля от нераспределённого",
                                callback_data=f"pn:pt_share:{partner.id}"))
    kb.row(btn("🗑 Удалить партнёра", f"pn:pt_del:{partner.id}", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ К партнёрам", callback_data="pn:partners"))
    return kb.as_markup()


@router.callback_query(F.data.startswith("pn:pt:"))
async def cb_partner_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    partner = await db.get_partner(conn, int(call.data.rsplit(":", 1)[1]))
    if partner is None:
        await call.answer("Партнёр не найден.", show_alert=True)
        await show_partners(call, conn)
        return
    await safe_edit(call, await partner_card(conn, partner), partner_kb(partner))
    await call.answer()


# ------------------------------------------------------------ добавление


@router.callback_query(F.data == "pn:pt_new")
async def cb_partner_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PartnerNew.name)
    await safe_edit(
        call,
        "🤝 <b>Новый партнёр</b>\n\n"
        "<blockquote>Как его записать? Пришлите имя — например "
        "<code>Алиджон</code>.</blockquote>",
        back_kb("pn:partners", "❌ Отмена"),
    )
    await call.answer()


@router.message(PartnerNew.name, F.text)
async def on_partner_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 40:
        await message.answer("❌ Имя от 2 до 40 символов.")
        return
    await state.update_data(partner_name=name)
    await state.set_state(PartnerNew.share)
    await message.answer(
        f"🤝 <b>{name}</b>\n\n"
        "<blockquote>Какая у него доля в процентах? Например "
        "<code>50</code>.</blockquote>",
        reply_markup=back_kb("pn:partners", "❌ Отмена"),
    )


@router.message(PartnerNew.share, F.text)
async def on_partner_share(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    raw = (message.text or "").strip().rstrip("%").strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 100:
        await message.answer("❌ Введите число от 0 до 100.")
        return

    data = await state.get_data()

    # Тот же шаг используется и для правки доли у существующего партнёра.
    edit_id = data.get("edit_id")
    if edit_id:
        await db.update_partner(conn, edit_id, share=int(raw))
        await state.clear()
        partner = await db.get_partner(conn, edit_id)
        kb = InlineKeyboardBuilder()
        kb.row(btn("👤 Карточка", f"pn:pt:{edit_id}"))
        kb.row(btn("‹ К партнёрам", "pn:partners"))
        await message.answer(
            f"✅ Доля <b>{partner.name if partner else ''}</b> теперь <b>{raw}%</b>",
            reply_markup=kb.as_markup(),
        )
        return

    name = data.get("partner_name", "").strip()
    if not name:
        await state.clear()
        await message.answer("Не понял, кого добавляем. Откройте /panel заново.")
        return

    partner = await db.create_partner(conn, name, int(raw))
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Карточка", f"pn:pt:{partner.id}"))
    kb.row(btn("‹ К партнёрам", "pn:partners"))
    await message.answer(
        f"✅ <b>{name}</b> добавлен, доля <b>{raw}%</b>",
        reply_markup=kb.as_markup(),
    )


# ------------------------------------------------------- взносы и выплаты


@router.callback_query(F.data.startswith("pn:pt_in:"))
async def cb_partner_in(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await _ask_partner_amount(call, state, conn, "in")


@router.callback_query(F.data.startswith("pn:pt_out:"))
async def cb_partner_out(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await _ask_partner_amount(call, state, conn, "out")


async def _ask_partner_amount(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection, kind: str,
) -> None:
    partner = await db.get_partner(conn, int(call.data.rsplit(":", 1)[1]))
    if partner is None:
        await call.answer("Партнёр не найден.", show_alert=True)
        return

    await state.set_state(PartnerMove.amount)
    await state.update_data(partner_id=partner.id, move_kind=kind)
    title = "внёс в оборот" if kind == "in" else "забрал себе"
    await safe_edit(
        call,
        f"🤝 <b>{partner.name} {title}</b>\n\n"
        "<blockquote>Пришлите сумму в сомони. Можно с примечанием через "
        "пробел: <code>500 пополнил FazerCards</code></blockquote>",
        back_kb(f"pn:pt:{partner.id}", "❌ Отмена"),
    )
    await call.answer()


@router.message(PartnerMove.amount, F.text)
async def on_partner_amount(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    partner = await db.get_partner(conn, data.get("partner_id", 0))
    if partner is None:
        await state.clear()
        await message.answer("Не понял, кому записать. Откройте /panel заново.")
        return

    raw, _, note = (message.text or "").strip().partition(" ")
    amount = parse(raw)
    if amount is None or amount <= 0:
        await message.answer(
            "❌ Введите сумму числом, например <code>500</code> "
            "или <code>500 пополнил FazerCards</code>."
        )
        return

    kind = data.get("move_kind", "in")
    signed = amount if kind == "in" else -amount
    await db.add_partner_move(
        conn, partner_id=partner.id, amount=signed,
        admin_id=message.from_user.id, note=note.strip() or None,
    )
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Карточка", f"pn:pt:{partner.id}"))
    kb.row(btn("‹ К партнёрам", "pn:partners"))
    await message.answer(
        f"✅ <b>{partner.name}</b> — "
        f"{'внесено' if kind == 'in' else 'выплачено'} <b>{fmt(amount)}</b>\n\n"
        + await partner_card(conn, partner),
        reply_markup=kb.as_markup(),
    )


# ------------------------------------------------------------- доля и удаление


@router.callback_query(F.data.startswith("pn:pt_share:"))
async def cb_partner_share(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    partner = await db.get_partner(conn, int(call.data.rsplit(":", 1)[1]))
    if partner is None:
        await call.answer("Партнёр не найден.", show_alert=True)
        return
    await state.set_state(PartnerNew.share)
    await state.update_data(partner_name="", edit_id=partner.id)
    await safe_edit(
        call,
        f"📊 <b>Доля: {partner.name}</b>\n\n"
        f"Сейчас: <b>{partner.share}%</b>\n\n"
        "<blockquote>Пришлите новый процент, например <code>50</code>."
        "</blockquote>",
        back_kb(f"pn:pt:{partner.id}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:pt_del:"))
async def cb_partner_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Убираем из списка, но движение денег храним: это история расчётов."""
    partner_id = int(call.data.rsplit(":", 1)[1])
    await db.update_partner(conn, partner_id, active=0)
    await call.answer("Удалён")
    await show_partners(call, conn)


@router.callback_query(F.data.startswith("pn:pt_goods:"))
async def cb_partner_goods(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Какие товары закреплены за партнёром."""
    partner = await db.get_partner(conn, int(call.data.rsplit(":", 1)[1]))
    if partner is None:
        await call.answer("Партнёр не найден.", show_alert=True)
        return
    await safe_edit(call, await goods_text(conn, partner),
                    await goods_kb(conn, partner))
    await call.answer()


async def goods_text(conn: aiosqlite.Connection, partner: db.Partner) -> str:
    owners = await db.product_owners(conn)
    partners = {p.id: p.name for p in await db.list_partners(conn)}

    rows = []
    for code, title in db.PRODUCT_TITLES.items():
        owner = owners.get(code)
        if owner == partner.id:
            mark = "✅ <b>ваш</b>"
        elif owner:
            mark = f"👤 <i>{partners.get(owner, 'другой партнёр')}</i>"
        else:
            mark = "<i>ничей</i>"
        rows.append(f"├ {title} — {mark}")

    return (
        f"📦 <b>Товары: {partner.name}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + "\n".join(rows)
        + "\n\n<blockquote>Нажмите на товар, чтобы закрепить его за "
          "этим партнёром или снять. Вся прибыль с товара идёт его "
          "владельцу.</blockquote>"
    )


async def goods_kb(
    conn: aiosqlite.Connection, partner: db.Partner
) -> InlineKeyboardMarkup:
    owners = await db.product_owners(conn)
    kb = InlineKeyboardBuilder()
    for code, title in db.PRODUCT_TITLES.items():
        mine = owners.get(code) == partner.id
        kb.row(btn(("✅ " if mine else "") + title,
                   f"pn:pt_take:{partner.id}:{code}",
                   style=SUCCESS if mine else None))
    kb.row(InlineKeyboardButton(text="‹ К партнёру",
                                callback_data=f"pn:pt:{partner.id}"))
    return kb.as_markup()


@router.callback_query(F.data.startswith("pn:pt_take:"))
async def cb_partner_take(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    _, _, raw_id, code = call.data.split(":", 3)
    partner = await db.get_partner(conn, int(raw_id))
    if partner is None or code not in db.PRODUCT_TITLES:
        await call.answer("Не найдено.", show_alert=True)
        return

    owners = await db.product_owners(conn)
    if owners.get(code) == partner.id:
        await db.set_product_owner(conn, code, None)
        await call.answer("Снято")
    else:
        await db.set_product_owner(conn, code, partner.id)
        await call.answer(f"Закреплено за {partner.name}")

    await safe_edit(call, await goods_text(conn, partner),
                    await goods_kb(conn, partner))


# ═════════════════════════════════════════════════════════════════ игры


async def games_text(conn: aiosqlite.Connection) -> str:
    from app.services import games as gsvc

    items = await db.list_games(conn)
    if not items:
        body = (
            "<blockquote>Игр пока нет. Нажмите «Найти игру по названию» — например "
            "<code>free fire</code> — или «Добавить все игры сразу».</blockquote>"
        )
    else:
        # Коротко: список всех игр поставщика не влезает в одно сообщение Telegram
        on = [g for g in items if g.enabled]
        off = len(items) - len(on)
        shown = on[:25]
        body = (f"В меню: <b>{len(on)}</b>" + (f" · скрыто: <b>{off}</b>" if off else "")
                + f" · общая наценка <b>{runtime.margin_percent()}%</b>\n\n"
                + ("\n".join(f"✅ {g.title}" + (f" · <i>{g.margin}%</i>" if g.margin else "") for g in shown)
                   if on else "<i>В меню пока ни одной игры — включите нужные в «Скрытые игры».</i>")
                + (f"\n<i>…и ещё {len(on) - len(shown)}</i>" if len(on) > len(shown) else ""))

    rate = runtime.usd_rate()
    warn = ""
    if rate <= 0:
        warn = ("\n\n[[warn]] <b>Курс доллара не задан</b> — цены пакетов "
                "посчитать не из чего, раздел работать не будет.")

    return (
        "🕹 <b>Игры</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{body}{warn}\n\n"
        "<blockquote>Цена пакета считается сама: себестоимость поставщика "
        "в долларах по курсу плюс наценка. Заказы почти всегда уходят "
        "«в обработку», бот следит за ними и ждёт ответа поставщика: отменил — "
        "деньги клиенту возвращаются сами, выполнил — заказ закрывается. Если ответа нет "
        f"дольше {gsvc.timeout_minutes()} мин., бот предупредит вас (Standoff 2 и Clash of Clans — "
        "через сутки: они выдаются до 90 минут, ночные — утром).</blockquote>"
    )


async def games_kb(conn: aiosqlite.Connection) -> InlineKeyboardMarkup:
    from app.services import games as gsvc

    from app.handlers import donatix

    kb = InlineKeyboardBuilder()
    if donatix.enabled():
        # Бот из конструктора: найти игру по названию или добавить все — без кодов поставщика
        kb.row(btn("🔎 Найти и добавить игру", "pn:game_find", style=SUCCESS))
        kb.row(btn("⚡ Добавить все игры сразу", "pn:game_add_all", style=PRIMARY))
    else:
        kb.row(btn("➕ Добавить игру", "pn:game_new", style=SUCCESS))
        kb.row(btn("⚡ Добавить все игры сразу", "pn:game_add_all", style=SUCCESS))
        kb.row(btn("🔎 Найти игру по названию", "pn:game_find", style=PRIMARY))
        kb.row(InlineKeyboardButton(text="📚 Взять из каталога поставщика",
                                    callback_data="pn:game_pick"))
        kb.row(btn("🪪 Проверка ID игрока", "pn:checker", style=PRIMARY))
        kb.row(InlineKeyboardButton(text="📋 Все категории поставщика",
                                    callback_data="pn:game_codes"))
    if not donatix.enabled():  # у бота из конструктора статусы приходят от Donatix
        kb.row(InlineKeyboardButton(text="🔔 Мгновенные отчёты (вебхук)",
                                    callback_data="pn:hook"))
    games_all = await db.list_games(conn)
    for game in [g for g in games_all if g.enabled][:40]:
        kb.row(InlineKeyboardButton(text="✅ " + game.title, callback_data=f"pn:game:{game.category_id}"))
    off = sum(1 for g in games_all if not g.enabled)
    if off:
        kb.row(InlineKeyboardButton(text=f"🚫 Скрытые игры · {off}", callback_data="pn:games_off:0"))
    if donatix.enabled():
        # Бот из конструктора: ключи поставщика и проверку связи ведёт сам Donatix
        kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
        return kb.as_markup()
    kb.row(btn("🔌 Проверить поставщика игр", "pn:games_check", style=PRIMARY))
    kb.row(InlineKeyboardButton(
        text=f"⏱ Ожидание выдачи · {gsvc.timeout_minutes()} мин",
        callback_data="pn:set:games_timeout_min",
    ))
    kb.row(InlineKeyboardButton(text="🔑 Ключ поставщика для игр",
                                callback_data="pn:set:fazer_games_key"))
    kb.row(btn("🔑 Ключ для ников Free Fire", "pn:set:gameskinbo_key"))
    kb.row(InlineKeyboardButton(text="🔑 Второй справочник ников",
                                callback_data="pn:set:ff_community_key"))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


@router.callback_query(F.data == "pn:games")
async def cb_games(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await safe_edit(call, substitute(await games_text(conn)), await games_kb(conn))
    await call.answer()


GAMES_PAGE = 20


@router.callback_query(F.data.startswith("pn:games_off:"))
async def cb_games_off(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    """Скрытые игры — по страницам: их у поставщика сотни, в один экран не влезают."""
    await state.clear()
    raw = call.data.rsplit(":", 1)[1]
    page = int(raw) if raw.isdigit() else 0
    off = [g for g in await db.list_games(conn) if not g.enabled]
    pages = max(1, -(-len(off) // GAMES_PAGE))
    page = min(page, pages - 1)
    kb = InlineKeyboardBuilder()
    for game in off[page * GAMES_PAGE:(page + 1) * GAMES_PAGE]:
        kb.row(InlineKeyboardButton(text="🚫 " + game.title, callback_data=f"pn:game:{game.category_id}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"pn:games_off:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="pn:games_off:" + str(page)))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"pn:games_off:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(btn("🔎 Найти игру по названию", "pn:game_find", style=PRIMARY))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))
    await safe_edit(call, f"🚫 <b>Скрытые игры</b> · {len(off)}\n\n"
                          "Покупатели их не видят. Откройте игру и нажмите «Показать в меню».",
                    kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "pn:game_new")
async def cb_game_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GameNew.data)
    await safe_edit(
        call,
        "🕹 <b>Новая игра</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<blockquote>Пришлите одной строкой: <b>код название</b>\n\n"
        "Например:\n"
        "<code>free_fire_br 🔥 Free Fire</code>\n"
        "<code>pubg_mobile 🎯 PUBG Mobile</code>\n\n"
        "Код берётся у поставщика — это его <code>category_id</code>."
        "</blockquote>",
        back_kb("pn:games", "❌ Отмена"),
    )
    await call.answer()


@router.message(GameNew.data, F.text)
async def on_game_new(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, provider
) -> None:
    code, _, title = (message.text or "").strip().partition(" ")
    code, title = code.strip(), title.strip()
    if not re.fullmatch(r"[a-z0-9_\-]{2,40}", code) or len(title) < 2:
        await message.answer(
            "❌ Формат: <code>код название</code>, например\n"
            "<code>free_fire_br 🔥 Free Fire</code>"
        )
        return

    from app.services import regions as reg

    region = reg.nick_region(code)

    # Поле с ID у каждой игры своё: спрашиваем у поставщика, а не гадаем.
    from app.services import games as gsvc
    from app.services import suppliers

    found = await gsvc.detect_fields(suppliers.for_games(provider), code)
    field = ",".join(found) or "user_id"
    game = await db.add_game(conn, category_id=code, title=title,
                             field=field, region=region)
    await db.load_game_titles(conn)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.row(btn("🕹 Открыть игру", f"pn:game:{game.category_id}"))
    kb.row(btn("‹ К играм", "pn:games"))
    await message.answer(
        f"✅ <b>{title}</b> добавлена\n\n"
        f"├ Поля для ID: <code>{field}</code>\n"
        "└ <i>определено у поставщика</i>\n\n"
        "<blockquote>Проверьте пакеты кнопкой «Проверить пакеты», "
        "поставьте наценку и включите игру.</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "pn:game_pick")
async def cb_game_pick(call: CallbackQuery, conn: aiosqlite.Connection, provider) -> None:
    """Список игр поставщика кнопками: коды и регионы не надо переписывать руками.

    Ошибка в коде категории стоит дорого: игра добавляется, а пакеты к ней
    не приходят. Поэтому берём коды у самого поставщика.
    """
    await safe_edit(call, "📚 Спрашиваю каталог поставщика…",
                    back_kb("pn:games", "‹ Назад"))
    await call.answer()

    from app.services import regions as reg
    from app.services import suppliers

    from app.services import games as gsvc

    client = suppliers.for_games(provider)
    try:
        catalog = await gsvc.full_catalog(client)
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        await safe_edit(
            call,
            "❌ <b>Каталог не пришёл</b>\n\n"
            f"<blockquote expandable>{str(exc)[:400]}</blockquote>\n\n"
            "<i>Проверьте ключ поставщика для игр.</i>",
            back_kb("pn:games", "‹ К играм"),
        )
        return

    have = {game.category_id for game in await db.list_games(conn)}
    groups: dict[str, list[dict]] = {}
    for item in catalog:
        groups.setdefault(
            reg.family_of(item["category_id"], item.get("name", "")), []
        ).append(item)

    if not groups:
        await safe_edit(call, "📚 Поставщик не назвал ни одной игры.",
                        back_kb("pn:games", "‹ К играм"))
        return

    kb = InlineKeyboardBuilder()
    for family, items in sorted(groups.items()):
        items.sort(key=lambda i: reg.sort_key(i["category_id"], i.get("name", "")))
        added = sum(1 for i in items if i["category_id"] in have)
        mark = "✅" if added == len(items) else ("◻️" if not added else "▫️")
        name = reg.clean_title(items[0]["category_id"], items[0].get("name", "")) or family
        kb.row(InlineKeyboardButton(
            text=f"{mark} {name} · регионов: {len(items)}",
            callback_data=f"pn:game_add:{family}",
        ))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))

    await safe_edit(
        call,
        "📚 <b>Каталог поставщика</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"Игр: <b>{len(groups)}</b>\n\n"
        "<blockquote>Нажмите игру — добавлю сразу все её регионы. Клиент "
        "выберет игру, а потом регион своего аккаунта.\n\n"
        "Регион важен: на чужом сервере ID игрока не находится, и "
        "пополнение не доходит.</blockquote>",
        kb.as_markup(),
    )


@router.callback_query(F.data == "pn:hook")
async def cb_hook(call: CallbackQuery, state: FSMContext,
                  conn: aiosqlite.Connection) -> None:
    """Вебхук: поставщик сам сообщает о выдаче, клиент узнаёт за секунду."""
    await state.clear()
    from app.services import webhook as hook

    port = runtime.get_int("webhook_port") or 0
    key = hook.secret()
    url = hook.public_url()
    seen, last = await db.events_seen(conn)

    if not key:
        status = "🔴 <b>Выключен</b> — не задан секрет"
    elif port <= 0:
        status = "🔴 <b>Выключен</b> — не задан порт"
    elif not url:
        status = "🟠 <b>Слушает</b>, но адрес для кабинета не задан"
    else:
        status = "🟢 <b>Включён</b>"

    body = (
        "🔔 <b>Мгновенные отчёты</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{status}\n\n"
        f"├ Секрет: <b>{'задан' if key else 'нет'}</b>\n"
        f"├ Порт: <b>{port or 'нет'}</b>\n"
        f"├ Адрес: <code>{url or 'не задан'}</code>\n"
        f"└ Принято отчётов: <b>{seen}</b>"
        + (f" · последний {last[:16]}" if last else "") + "\n\n"
        "<blockquote>Сейчас бот сам спрашивает поставщика, выполнен ли "
        "заказ. Это занимает от нескольких секунд до нескольких минут.\n\n"
        "С вебхуком поставщик сообщает сам, и клиент узнаёт о пополнении "
        "сразу. Опрос при этом остаётся: отчёт может не дойти, и тогда "
        "заказ всё равно закроется.</blockquote>\n\n"
        "<blockquote expandable>Как включить:\n"
        "1. Задайте секрет — это строка <code>whsec_…</code> из кабинета "
        "поставщика.\n"
        "2. Задайте порт, который открыт на сервере (например 8081).\n"
        "3. Задайте адрес бота вида <code>https://bot.example.com</code>.\n"
        "4. В кабинете поставщика впишите адрес, который бот покажет "
        "здесь.\n\n"
        "Без https поставщик отчёты слать не станет.</blockquote>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(btn("🔑 Секрет вебхука", "pn:set:fazer_webhook_secret",
               style=PRIMARY))
    kb.row(InlineKeyboardButton(text="🔌 Порт",
                                callback_data="pn:set:webhook_port"),
           InlineKeyboardButton(text="🌐 Адрес бота",
                                callback_data="pn:set:webhook_public_url"))
    if url:
        kb.row(InlineKeyboardButton(
            text="📋 Скопировать адрес для кабинета",
            copy_text=CopyTextButton(text=url),
        ))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))

    await safe_edit(call, body, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "pn:checker")
async def cb_checker(call: CallbackQuery, state: FSMContext,
                     conn: aiosqlite.Connection) -> None:
    """Проверка ID игрока: ключ, остаток лимита и привязка игр."""
    await state.clear()
    from app.services import volsever

    key = runtime.get("volsever_key") or settings.volsever_key
    games = await db.list_games(conn)
    linked = [g for g in games if g.checker]

    if key:
        head = "🟢 <b>Ключ задан</b>"
        left = await volsever.usage(key)
        if left:
            head += f"\n<code>{str(left)[:200]}</code>"
    else:
        head = ("🔴 <b>Ключ не задан</b> — ID не проверяется, "
                "ник клиенту не показывается")

    body = "\n".join(
        f"{'✅' if g.checker else '➖'} <b>{g.title}</b>"
        + (f"\n   <code>{g.checker}</code>" if g.checker
           else "\n   <i>проверка не настроена</i>")
        for g in games
    ) or "<i>Игр пока нет.</i>"

    kb = InlineKeyboardBuilder()
    kb.row(btn("🔑 Ключ Volsever", "pn:set:volsever_key", style=PRIMARY))
    if key:
        kb.row(btn("🔗 Привязать игры к проверке", "pn:checker_link",
                   style=SUCCESS))
    for game in games:
        kb.row(InlineKeyboardButton(
            text=("✅ " if game.checker else "➖ ") + game.title,
            callback_data=f"pn:checker_set:{game.category_id}",
        ))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))

    await safe_edit(
        call,
        "🪪 <b>Проверка ID игрока</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"{head}\n\n{body}\n\n"
        f"Привязано игр: <b>{len(linked)}</b> из <b>{len(games)}</b>\n\n"
        "<blockquote>Перед покупкой бот спрашивает у сервиса, есть ли "
        "такой аккаунт, и показывает клиенту его ник. Это отсекает "
        "опечатки в ID до оплаты, а не после.\n\n"
        "У каждой игры свой код у сервиса проверки — он не совпадает с "
        "кодом поставщика выдачи, поэтому игры привязываются "
        "отдельно.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:checker_link")
async def cb_checker_link(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Подобрать код проверки каждой игре автоматически, по названию."""
    from app.services import volsever

    key = runtime.get("volsever_key") or settings.volsever_key
    await safe_edit(call, "🔗 Спрашиваю список игр…", back_kb("pn:checker", "‹ Назад"))
    await call.answer()

    catalog = await volsever.games(key)
    if not catalog:
        await safe_edit(
            call,
            "❌ <b>Список игр не пришёл</b>\n\n"
            "<blockquote>Проверьте ключ. Если он верный — возможно, у "
            "сервиса другой адрес списка; привяжите игры вручную, нажав "
            "игру в списке.</blockquote>",
            back_kb("pn:checker", "‹ Назад"),
        )
        return

    lines = []
    for game in await db.list_games(conn):
        if game.checker:
            continue
        match = _best_match(game, catalog)
        if match:
            await db.update_game(conn, game.category_id, checker=match["code"])
            lines.append(f"✅ {game.title} → <code>{match['code']}</code>")
        else:
            lines.append(f"➖ {game.title} — <i>похожего не нашлось</i>")

    await safe_edit(
        call,
        f"🔗 <b>Привязка к проверке</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"Игр у сервиса: <b>{len(catalog)}</b>\n\n"
        + ("\n".join(lines) or "<i>Все игры уже привязаны.</i>")
        + "\n\n<blockquote>Проверьте подбор: коды подбирались по "
        "названию, а названия у сервисов расходятся. Неверную привязку "
        "поправьте, нажав игру.</blockquote>",
        back_kb("pn:checker", "‹ К проверке"),
    )


def _best_match(game: db.Game, catalog: list[dict]) -> dict | None:
    """Код проверки, больше всего похожий на нашу игру.

    Считаем по словам названия: «free fire» роднит free-fire-asia и
    «🔥 Free Fire», а длина совпадения ничего не значит.
    """
    from app.services import regions as reg

    mine = {w for w in re.split(r"[^a-zа-я0-9]+", game.title.lower()) if len(w) > 2}
    mine |= {w for w in re.split(r"[^a-z0-9]+", game.category_id.lower()) if w}
    want = reg.suffix_of(game)

    best, score = None, 0
    for item in catalog:
        theirs = {w for w in re.split(r"[^a-z0-9]+",
                                      f"{item['code']} {item['name']}".lower()) if w}
        common = len(mine & theirs)
        if not common:
            continue
        # Совпавший регион — сильный довод: у Free Fire коды разложены
        # по серверам, и привязать не тот значит проверять не там.
        if want and want in theirs:
            common += 2
        if common > score:
            best, score = item, common
    return best


@router.callback_query(F.data.startswith("pn:checker_set:"))
async def cb_checker_set(call: CallbackQuery, state: FSMContext,
                         conn: aiosqlite.Connection) -> None:
    """Задать код проверки вручную."""
    code = call.data.split(":", 2)[2]
    game = await db.get_game(conn, code)
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return

    await state.set_state(Panel.value)
    await state.update_data(field=f"game_checker:{code}")
    await safe_edit(
        call,
        f"🪪 <b>Проверка ID: {game.title}</b>\n\n"
        f"Сейчас: <code>{game.checker or 'не задана'}</code>\n\n"
        "<blockquote>Пришлите код этой игры у сервиса проверки — "
        "например <code>free-fire-asia</code>.\n\n"
        "Чтобы выключить проверку для этой игры, пришлите "
        "<code>-</code>.</blockquote>",
        back_kb("pn:checker", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data == "pn:game_find")
async def cb_game_find(call: CallbackQuery, state: FSMContext) -> None:
    """Поиск по каталогу: у поставщика две сотни игр, листать их нельзя."""
    await state.set_state(Panel.game_find)
    await safe_edit(
        call,
        "🔎 <b>Найти игру у поставщика</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "<blockquote>Пришлите название или его часть — покажу все "
        "подходящие коды.\n\n"
        "Например: <code>pubg</code>, <code>free fire</code>, "
        "<code>mobile legends</code>.\n\n"
        "Искать можно и по коду: <code>pubgm</code>.</blockquote>",
        back_kb("pn:games", "❌ Отмена"),
    )
    await call.answer()


@router.message(Panel.game_find, F.text)
async def on_game_find(
    message: Message, state: FSMContext, conn: aiosqlite.Connection, provider
) -> None:
    from app.services import games as gsvc
    from app.services import regions as reg
    from app.services import suppliers

    query = (message.text or "").strip().lower()
    if len(query) < 2:
        await message.answer("❌ Слишком короткий запрос — нужно хотя бы две буквы.")
        return

    notice = await message.answer("🔎 Ищу в каталоге…")
    try:
        catalog = await gsvc.full_catalog(suppliers.for_games(provider),
                                          cached=True)
    except Exception as exc:  # noqa: BLE001 — показать владельцу причину
        await notice.edit_text(
            "❌ <b>Каталог не пришёл</b>\n\n"
            f"<blockquote expandable>{str(exc)[:300]}</blockquote>"
        )
        return

    # Ищем по словам: «mobile legends» должно находить и там, где в
    # названии между ними стоит что-то ещё. Русские названия игр («пабг»,
    # «фри фаер») и регион по-русски («Индонезии») тоже находят.
    from app.services import autogames

    words = [w for w in re.split(r"[^a-zа-яё0-9]+", autogames.expand(query)) if w]
    found = []
    for item in catalog:
        region = reg.title_of(item["category_id"], item.get("name", ""))
        haystack = f"{item['category_id']} {item.get('name', '')} {region}".lower()
        if autogames.matches(words, haystack):
            found.append(item)

    if not found:
        await notice.edit_text(
            f"😔 <b>По запросу «{query}» ничего нет</b>\n\n"
            "<blockquote>Попробуйте короче — например одно слово. "
            "Поставщик пишет названия по-английски: "
            "<code>pubg</code>, а не «пабг».</blockquote>",
            reply_markup=back_kb("pn:game_find", "🔎 Искать снова"),
        )
        return

    have = {game.category_id for game in await db.list_games(conn)}
    found.sort(key=lambda i: reg.sort_key(i["category_id"], i.get("name", "")))

    rows, kb = [], InlineKeyboardBuilder()
    families: dict[str, dict] = {}
    for item in found[:30]:
        code, name = item["category_id"], item.get("name", "")
        mark = "✅" if code in have else "◻️"
        region = reg.title_of(code, name)
        rows.append(f"{mark} <code>{code}</code>\n   {name}"
                    + (f" · {region}" if region else ""))
        families.setdefault(reg.family_of(code, name), item)

    for family, item in list(families.items())[:8]:
        name = reg.clean_title(item["category_id"], item.get("name", ""))
        regions_found = [i for i in found if reg.family_of(i["category_id"], i.get("name", "")) == family]
        if all(i["category_id"] in have for i in regions_found):
            # уже добавлена — сразу кнопки регионов: открыть и править пакеты
            for i in regions_found[:12]:
                region = reg.title_of(i["category_id"], i.get("name", "")) or "без региона"
                kb.row(InlineKeyboardButton(text=f"🕹 {name} · {region}"[:60],
                                            callback_data=f"pn:game:{i['category_id']}"))
            continue
        kb.row(btn(f"➕ Добавить {name} · регионов: {len(regions_found)}"[:60],
                   f"pn:game_add:{family}", style=SUCCESS))
    kb.row(InlineKeyboardButton(text="🔎 Искать снова",
                                callback_data="pn:game_find"))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))

    await state.clear()
    text = (f"🔎 <b>Найдено: {len(found)}</b>\n"
            f"<code>{texts.LINE}</code>\n\n" + "\n".join(rows)
            + ("\n\n<i>…показаны первые 30</i>" if len(found) > 30 else "")
            + "\n\n<blockquote>✅ — уже добавлена, ◻️ — ещё нет.\n\n"
            "Кнопка добавляет игру сразу со всеми её регионами.</blockquote>")
    await notice.edit_text(text[:4000], reply_markup=kb.as_markup())


@router.callback_query(F.data == "pn:game_codes")
async def cb_game_codes(call: CallbackQuery, provider) -> None:
    """Сырой список категорий поставщика: код, название, распознанный регион.

    Нужен, когда непонятно, есть ли у игры нужный сервер: гадать по
    названию в меню бесполезно, а здесь видно всё как есть.
    """
    await safe_edit(call, "📋 Спрашиваю каталог…", back_kb("pn:games", "‹ Назад"))
    await call.answer()

    from app.services import regions as reg
    from app.services import suppliers

    from app.services import games as gsvc

    try:
        catalog = await gsvc.full_catalog(suppliers.for_games(provider))
    except Exception as exc:  # noqa: BLE001
        await safe_edit(
            call,
            "❌ <b>Каталог не пришёл</b>\n\n"
            f"<blockquote expandable>{str(exc)[:400]}</blockquote>",
            back_kb("pn:games", "‹ К играм"),
        )
        return

    if not catalog:
        await safe_edit(call, "📋 Поставщик не назвал ни одной категории.",
                        back_kb("pn:games", "‹ К играм"))
        return

    catalog.sort(key=lambda i: (reg.family_of(i["category_id"], i.get("name", "")),
                                reg.sort_key(i["category_id"], i.get("name", ""))))
    rows = []
    for item in catalog:
        code, name = item["category_id"], item.get("name", "")
        mark = reg.title_of(code, name) or "<i>регион не распознан</i>"
        if not item.get("checkable", True):
            mark += " · <i>без проверки ID</i>"
        fields = ", ".join(
            str(spec.get("name") if isinstance(spec, dict) else spec)
            for spec in (item.get("fields") or [])
        )
        rows.append(f"├ <code>{code}</code>\n"
                    f"│  {name} · {mark}"
                    + (f"\n│  поля: <code>{fields}</code>" if fields else ""))

    text = ("📋 <b>Категории поставщика</b>\n"
            f"<code>{texts.LINE}</code>\n\n"
            f"Всего: <b>{len(catalog)}</b>\n\n" + "\n".join(rows))
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>…список обрезан</i>"

    await safe_edit(call, text, back_kb("pn:games", "‹ К играм"))


@router.callback_query(F.data == "pn:game_add_all")
async def cb_game_add_all(call: CallbackQuery, conn: aiosqlite.Connection, provider) -> None:
    """Все игры поставщика — в меню одним нажатием, со всеми регионами."""
    await call.answer("Добавляю все игры…")
    from app.services import autogames
    from app.services import games as gsvc
    from app.services import suppliers

    try:
        catalog = await gsvc.full_catalog(suppliers.for_games(provider))
    except Exception as exc:  # noqa: BLE001
        await call.answer(f"Каталог не пришёл: {str(exc)[:120]}", show_alert=True)
        return
    added, enabled = await autogames.import_all(conn, catalog)
    note = (f"✅ Добавлено игр: <b>{added}</b>, включено в меню: <b>{enabled}</b>."
            if added else "Все игры поставщика уже добавлены.")
    if runtime.usd_rate() <= 0:
        note += "\n\n⚠️ Курс доллара не задан — игры добавлены, но скрыты. Задайте курс в «Цены»."
    await safe_edit(call, substitute(await games_text(conn)) + "\n\n" + note, await games_kb(conn))


@router.callback_query(F.data.startswith("pn:game_add:"))
async def cb_game_add_family(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Добавить все регионы одной игры разом."""
    family = call.data.split(":", 2)[2]
    await call.answer("Добавляю…")

    from app.services import games as gsvc
    from app.services import regions as reg
    from app.services import suppliers

    client = suppliers.for_games(provider)
    try:
        catalog = await gsvc.full_catalog(client)
    except Exception as exc:  # noqa: BLE001
        await call.answer(f"Каталог не пришёл: {str(exc)[:120]}", show_alert=True)
        return

    items = [i for i in catalog
             if reg.family_of(i["category_id"], i.get("name", "")) == family]
    if not items:
        await call.answer("Такой игры у поставщика больше нет.", show_alert=True)
        return

    # Название берём то, что уже стоит у нас: владелец мог поставить
    # своё с эмодзи, и терять его при добавлении регионов незачем.
    mine = {g.category_id: g.title for g in await db.list_games(conn)}

    items.sort(key=lambda i: reg.sort_key(i["category_id"], i.get("name", "")))
    lines = []
    for item in items:
        code = item["category_id"]
        names = [str(spec.get("name") if isinstance(spec, dict) else spec)
                 for spec in item.get("fields") or []
                 if (spec.get("name") if isinstance(spec, dict) else spec)]
        field = ",".join(names) or await gsvc.detect_field(client, code) or "user_id"
        # У каждого региона своё имя от поставщика; регион из него потом
        # вычитается. Уже стоящее у нас имя не трогаем.
        await db.add_game(
            conn, category_id=code,
            title=mine.get(code) or item["name"] or code,
            field=field, region=reg.nick_region(code, item.get("name", "")),
        )
        lines.append(f"├ {reg.title_of(code, item.get('name', '')) or 'без региона'} — "
                     f"<code>{code}</code> · поле <code>{field}</code>")
    await db.load_game_titles(conn)

    kb = InlineKeyboardBuilder()
    kb.row(btn(f"✅ Показать все {len(items)} региона в меню",
               f"pn:game_all_on:{family}", style=SUCCESS))
    for item in items[:12]:
        region = reg.title_of(item["category_id"], item.get("name", "")) or "без региона"
        kb.row(InlineKeyboardButton(text=f"🕹 {region} — пакеты и цены"[:60],
                                    callback_data=f"pn:game:{item['category_id']}"))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))

    await safe_edit(
        call,
        f"✅ <b>{reg.clean_title(items[0]['category_id'], items[0]['name'])}</b>"
        " — добавлено регионов: "
        f"<b>{len(items)}</b>\n"
        f"<code>{texts.LINE}</code>\n\n" + "\n".join(lines) + "\n\n"
        "<blockquote>Пока регионы скрыты от клиентов. Включите их кнопкой "
        "ниже — тогда при покупке этой игры появится выбор региона.\n\n"
        "Отдельный регион можно выключить в его карточке.</blockquote>",
        kb.as_markup(),
    )


@router.callback_query(F.data.startswith("pn:game_all_on:"))
async def cb_game_family_on(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Включить в меню сразу все регионы игры."""
    if runtime.usd_rate() <= 0:
        await call.answer("Сначала задайте курс доллара.", show_alert=True)
        return

    from app.services import regions as reg

    family = call.data.split(":", 2)[2]
    turned = 0
    for game in await db.list_games(conn):
        if reg.family_of(game) == family and not game.enabled:
            await db.update_game(conn, game.category_id, enabled=1)
            turned += 1
    await call.answer(f"Включено: {turned}" if turned else "Уже включены.")
    await safe_edit(call, substitute(await games_text(conn)), await games_kb(conn))


@router.callback_query(F.data.startswith("pn:game:"))
async def cb_game_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    game = await db.get_game(conn, call.data.split(":", 2)[2])
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        await safe_edit(call, substitute(await games_text(conn)), await games_kb(conn))
        return
    await safe_edit(call, game_card(game), game_kb(game))
    await call.answer()


def _fields_asked(game: db.Game) -> str:
    """Что бот спросит у клиента, человеческими словами."""
    from app.services import games as gsvc

    return ", ".join(gsvc.field_label(name) for name in game.field_names)


def _region_label(game: db.Game) -> str:
    """Регион в карточке: название с флагом, если он читается из кода."""
    from app.services import regions as reg

    title = reg.title_of(game)
    if title:
        return f"{title} ({reg.suffix_of(game).upper()})"
    return game.region or "один на все"


def game_card(game: db.Game) -> str:
    return (
        f"🕹 <b>{game.title}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Код: <code>{game.category_id}</code>\n"
        f"├ Поля для ID: <code>{game.field}</code>\n"
        f"│  <i>спрашиваем: {_fields_asked(game)}</i>\n"
        f"├ Регион: <b>{_region_label(game)}</b>\n"
        + (f"├ Значок: <code>{game.emoji}</code>\n" if game.emoji else "")
        + f"├ Наценка: <b>{game.margin or runtime.margin_percent()}%</b>"
        + ("" if game.margin else " <i>(общая)</i>")
        + f"\n└ В меню: <b>{'да' if game.enabled else 'нет'}</b>"
    )


def game_kb(game: db.Game) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("🚫 Убрать из меню" if game.enabled else "✅ Показать в меню",
               f"pn:game_on:{game.category_id}",
               style=DANGER if game.enabled else SUCCESS))
    kb.row(btn("📦 Пакеты: цены и названия", f"pn:game_packs:{game.category_id}",
               style=PRIMARY))
    kb.row(InlineKeyboardButton(text="✏️ Название игры",
                                callback_data=f"pn:game_rename:{game.category_id}"))
    kb.row(
        InlineKeyboardButton(text="🔤 Поля для ID",
                             callback_data=f"pn:game_field:{game.category_id}"),
        InlineKeyboardButton(text="📈 Своя наценка",
                             callback_data=f"pn:game_margin:{game.category_id}"),
    )
    kb.row(InlineKeyboardButton(text="😀 Значок игры",
                                callback_data=f"pn:game_emoji:{game.category_id}"))
    kb.row(btn("🗑 Удалить игру", f"pn:game_del:{game.category_id}", style=DANGER))
    kb.row(InlineKeyboardButton(text="‹ К играм", callback_data="pn:games"))
    return kb.as_markup()


@router.callback_query(F.data.startswith("pn:game_rename:"))
async def cb_game_rename(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    code = call.data.split(":", 2)[2]
    game = await db.get_game(conn, code)
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    await state.set_state(Panel.value)
    await state.update_data(field=f"game_title:{code}")
    await safe_edit(
        call,
        f"✏️ <b>Название игры</b>\n<code>{texts.LINE}</code>\n\n"
        f"Сейчас: <b>{game.title}</b>\n\n"
        "<blockquote>Пришлите новое — так игру увидят покупатели в меню.\n"
        "Например: <code>🔥 Free Fire Индонезия</code></blockquote>",
        back_kb(f"pn:game:{code}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:game_on:"))
async def cb_game_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    game = await db.get_game(conn, call.data.split(":", 2)[2])
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    if not game.enabled and runtime.usd_rate() <= 0:
        await call.answer("Сначала задайте курс доллара.", show_alert=True)
        return
    await db.update_game(conn, game.category_id, enabled=0 if game.enabled else 1)
    await call.answer("Убрана" if game.enabled else "Показана")
    fresh = await db.get_game(conn, game.category_id)
    await safe_edit(call, game_card(fresh), game_kb(fresh))


@router.callback_query(F.data.startswith("pn:game_packs:"))
async def cb_game_offers(call: CallbackQuery, conn: aiosqlite.Connection, provider) -> None:
    """Пакеты игры: цена, себестоимость и кнопка на каждый — поправить цену."""
    code, _, raw_page = call.data.split(":", 2)[2].partition(":")
    game = await db.get_game(conn, code)
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return

    page = int(raw_page) if raw_page.isdigit() else 0
    if not page:
        await safe_edit(call, "📦 Спрашиваю пакеты…",
                        back_kb("pn:games", "‹ Назад"))
    await call.answer()
    await show_packs(call, conn, game, provider, page)


async def _similar_codes(provider, game: db.Game, limit: int = 6) -> list[dict]:
    """Коды из каталога, похожие на наш. Ищем по словам, не по буквам:
    free_fire_br и free_fire_cis роднит «free», а не длина совпадения."""
    from app.services import games as gsvc
    from app.services import suppliers

    try:
        catalog = await gsvc.full_catalog(suppliers.for_games(provider))
    except Exception as exc:  # noqa: BLE001 — подсказка не обязана работать
        log.info("Игры: каталог для подсказки не пришёл — %s", exc)
        return []

    words = {w for w in re.split(r"[^a-z0-9]+", game.category_id.lower()) if w}
    words |= {w for w in re.split(r"[^a-z0-9]+", game.title.lower()) if len(w) > 2}
    scored = []
    for item in catalog:
        theirs = {w for w in re.split(r"[^a-z0-9]+", item["category_id"].lower()) if w}
        theirs |= {w for w in re.split(r"[^a-z0-9]+",
                                       str(item.get("name", "")).lower()) if len(w) > 2}
        common = len(words & theirs)
        if common:
            scored.append((common, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored[:limit]]


def _codes_kb(game: db.Game, near: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for item in near:
        kb.row(InlineKeyboardButton(
            text=f"↪️ {item['category_id']} · {item.get('name', '')}"[:60],
            callback_data=f"pn:game_code:{game.category_id}:{item['category_id']}",
        ))
    if near:
        kb.row(InlineKeyboardButton(text="📋 Все категории поставщика",
                                    callback_data="pn:game_codes"))
    return kb


@router.callback_query(F.data.startswith("pn:game_code:"))
async def cb_game_recode(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Переставить игру на другой код категории, не теряя настроек.

    Наценка, своя цена и «показана в меню» привязаны к коду, поэтому
    удалить и добавить заново — значит потерять их.
    """
    _, _, old_code, new_code = call.data.split(":", 3)
    game = await db.get_game(conn, old_code)
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    if await db.get_game(conn, new_code) is not None:
        await call.answer("Игра с таким кодом уже есть.", show_alert=True)
        return

    from app.services import games as gsvc
    from app.services import suppliers

    found = await gsvc.detect_fields(suppliers.for_games(provider), new_code)
    await db.move_game(conn, old_code, new_code,
                       field=",".join(found) or game.field)
    await db.load_game_titles(conn)
    await call.answer("Код заменён")

    fresh = await db.get_game(conn, new_code)
    await safe_edit(
        call,
        f"✅ Код заменён: <code>{old_code}</code> → <code>{new_code}</code>\n\n"
        f"{game_card(fresh)}",
        game_kb(fresh),
    )


def _pack_mark(offer: dict) -> str:
    """Значок пакета: сразу видно, что в нём поменяли."""
    if offer.get("hidden"):
        return "🚫"
    if offer.get("manual") and offer.get("renamed"):
        return "✏️"
    if offer.get("manual"):
        return "💵"
    if offer.get("renamed"):
        return "🏷"
    return "├"


#: Сколько пакетов помещается на экран. У PUBG их за тридцать —
#: одним списком кнопки не влезают, Telegram обрезает разметку.
PACK_PAGE = 20


async def show_packs(
    call: CallbackQuery, conn: aiosqlite.Connection, game: db.Game, provider,
    page: int = 0,
) -> None:
    from app.handlers.games import offers_of
    from app.services import suppliers

    try:
        offers = await offers_of(suppliers.for_games(provider), game, conn,
                                 for_owner=True)
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        # «Неизвестная категория» — не поломка связи, а неверный код игры.
        # Гадать его владельцу не надо: подбираем похожие по каталогу.
        kb = InlineKeyboardBuilder()
        hint = ("<blockquote>Проверьте код игры — это "
                "<code>category_id</code> у поставщика.</blockquote>")
        if "category" in str(exc).lower():
            near = await _similar_codes(provider, game)
            kb = _codes_kb(game, near)
            hint = (
                "<blockquote>Такого кода у поставщика нет. Это не поломка: "
                "код просто другой.\n\n"
                + ("Похожие коды из каталога — ниже, нажмите нужный.\n\n"
                   if near else
                   "Похожих кодов в каталоге не нашлось.\n\n")
                + "Весь список: «📋 Все категории поставщика».</blockquote>"
            )
        kb.row(InlineKeyboardButton(
            text="‹ Назад", callback_data=f"pn:game:{game.category_id}"))
        await safe_edit(
            call,
            f"📦 <b>Пакеты {game.title} не пришли</b>\n\n"
            f"<blockquote expandable>{exc}</blockquote>\n\n{hint}",
            kb.as_markup(),
        )
        return

    if not offers:
        await safe_edit(
            call, f"📦 У <b>{game.title}</b> нет пакетов в продаже.",
            back_kb(f"pn:game:{game.category_id}", "‹ Назад"),
        )
        return

    total = len(offers)
    pages = max(1, -(-total // PACK_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * PACK_PAGE
    shown = offers[start:start + PACK_PAGE]

    rows = "\n".join(
        f"{_pack_mark(o)} {o['name']} — <b>{fmt(o['price'])}</b>"
        f" <i>(себест. {fmt(o['cost'])})</i>"
        for o in shown
    )
    kb = InlineKeyboardBuilder()
    for shift, offer in enumerate(shown):
        index = start + shift
        kb.row(InlineKeyboardButton(
            text=f"{_pack_mark(offer)} {offer['name']} — {fmt(offer['price'])}",
            callback_data=f"pn:pk:{game.category_id}:{index}",
        ))
    if pages > 1:
        flip = []
        if page:
            flip.append(InlineKeyboardButton(
                text="‹ Раньше",
                callback_data=f"pn:game_packs:{game.category_id}:{page - 1}"))
        if page < pages - 1:
            flip.append(InlineKeyboardButton(
                text="Дальше ›",
                callback_data=f"pn:game_packs:{game.category_id}:{page + 1}"))
        kb.row(*flip)
    kb.row(btn("📋 Прайс списком", f"pn:price_list:{game.category_id}",
               style=PRIMARY))
    kb.row(InlineKeyboardButton(text="‹ К игре",
                                callback_data=f"pn:game:{game.category_id}"))

    counter = (f"\n\n<i>Пакеты {start + 1}–{start + len(shown)} из {total}</i>"
               if pages > 1 else "")

    await safe_edit(
        call,
        f"📦 <b>Пакеты: {game.title}</b>\n"
        f"<code>{texts.LINE}</code>\n\n{rows}{counter}\n\n"
        f"<blockquote>Наценка {game.margin or runtime.margin_percent()}%, "
        f"курс {fmt(runtime.usd_rate())} за доллар. ✏️ — цена задана руками.\n\n"
        "Нажмите на пакет, чтобы поставить свою цену, или пришлите "
        "весь прайс сразу — «📋 Прайс списком».</blockquote>",
        kb.as_markup(),
    )


def pack_of(category_id: str, index: int) -> dict | None:
    """Пакет из последнего показанного списка."""
    from app.handlers.games import _offers

    offers = _offers.get(category_id) or []
    return offers[index] if 0 <= index < len(offers) else None


@router.callback_query(F.data.startswith("pn:pk:"))
async def cb_pack_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    _, _, category_id, raw_index = call.data.split(":", 3)
    game = await db.get_game(conn, category_id)
    offer = pack_of(category_id, int(raw_index) if raw_index.isdigit() else -1)
    if game is None or offer is None:
        await call.answer("Список устарел, откройте пакеты заново.", show_alert=True)
        return

    profit = offer["price"] - offer["cost"]
    percent = round(profit * 100 / offer["cost"]) if offer["cost"] else 0

    kb = InlineKeyboardBuilder()
    kb.row(btn("💵 Своя цена", f"pn:pkset:{category_id}:{raw_index}",
               style=PRIMARY))
    kb.row(btn("🏷 Переименовать", f"pn:pkname:{category_id}:{raw_index}"))
    if offer["hidden"]:
        kb.row(btn("✅ Вернуть в продажу",
                   f"pn:pkshow:{category_id}:{raw_index}", style=SUCCESS))
    else:
        kb.row(btn("🚫 Убрать из продажи",
                   f"pn:pkhide:{category_id}:{raw_index}", style=DANGER))
    if offer["manual"] or offer["renamed"]:
        kb.row(btn("♻️ Вернуть как у поставщика",
                   f"pn:pkauto:{category_id}:{raw_index}"))
    kb.row(InlineKeyboardButton(text="‹ К пакетам",
                                callback_data=f"pn:game_packs:{category_id}"))

    hidden_note = ""
    if offer["hidden"]:
        hidden_note = ("\n\n🚫 <b>Пакет убран из продажи</b> — клиенты его "
                       "не видят.")

    await safe_edit(
        call,
        f"📦 <b>{offer['name']}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Игра: <b>{game.title}</b>\n"
        + (f"├ У поставщика: <i>{offer['supplier_name']}</i>\n"
           if offer["renamed"] else "")
        + f"├ Себестоимость: <b>{fmt(offer['cost'])}</b> (${offer['usd']})\n"
        f"├ Цена продажи: <b>{fmt(offer['price'])}</b>"
        + (" <i>(своя)</i>" if offer["manual"] else " <i>(по наценке)</i>")
        + (f"\n├ По наценке было бы: <b>{fmt(offer['auto'])}</b>"
           if offer["manual"] else "")
        + f"\n└ Прибыль: <b>{fmt(profit)}</b> ({percent}%)"
        + hidden_note + "\n\n"
        "<blockquote>Своя цена и своё название не пересчитываются и не "
        "сбрасываются: держатся, пока вы их не вернёте.\n\n"
        "Убранный пакет остаётся у поставщика — мы просто не показываем "
        "его клиентам.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:pkname:"))
async def cb_pack_rename(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Своё название пакета: у поставщика они часто безликие."""
    _, _, category_id, raw_index = call.data.split(":", 3)
    offer = pack_of(category_id, int(raw_index) if raw_index.isdigit() else -1)
    if offer is None:
        await call.answer("Список устарел, откройте пакеты заново.",
                          show_alert=True)
        return

    await state.set_state(Panel.value)
    await state.update_data(field=f"pack_name:{category_id}:{offer['offer_id']}")
    await safe_edit(
        call,
        f"🏷 <b>Название пакета</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Сейчас: <b>{offer['name']}</b>\n"
        f"└ У поставщика: <i>{offer['supplier_name']}</i>\n\n"
        "<blockquote>Пришлите новое название — его увидят клиенты.\n\n"
        "Например: <code>💎 100 алмазов</code>\n\n"
        "Чтобы вернуть название поставщика, пришлите <code>-</code>."
        "</blockquote>",
        back_kb(f"pn:pk:{category_id}:{raw_index}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith(("pn:pkhide:", "pn:pkshow:")))
async def cb_pack_visibility(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Убрать пакет из продажи или вернуть обратно."""
    action, _, category_id, raw_index = call.data.split(":", 3)
    offer = pack_of(category_id, int(raw_index) if raw_index.isdigit() else -1)
    game = await db.get_game(conn, category_id)
    if offer is None or game is None:
        await call.answer("Список устарел, откройте пакеты заново.",
                          show_alert=True)
        return

    hide = call.data.startswith("pn:pkhide:")
    await db.set_game_offer_hidden(conn, category_id, offer["offer_id"], hide)
    await call.answer("Убран из продажи" if hide else "Вернули в продажу")
    await show_packs(call, conn, game, provider)


@router.callback_query(F.data.startswith("pn:pkauto:"))
async def cb_pack_reset(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Вернуть пакету и цену, и название поставщика."""
    _, _, category_id, raw_index = call.data.split(":", 3)
    offer = pack_of(category_id, int(raw_index) if raw_index.isdigit() else -1)
    game = await db.get_game(conn, category_id)
    if offer is None or game is None:
        await call.answer("Список устарел, откройте пакеты заново.",
                          show_alert=True)
        return

    await db.set_game_price(conn, category_id, offer["offer_id"], None)
    await db.set_game_offer_title(conn, category_id, offer["offer_id"], "")
    await call.answer("Вернули как у поставщика")
    await show_packs(call, conn, game, provider)


@router.callback_query(F.data.startswith("pn:pkset:"))
async def cb_pack_price(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    _, _, category_id, raw_index = call.data.split(":", 3)
    offer = pack_of(category_id, int(raw_index) if raw_index.isdigit() else -1)
    if offer is None:
        await call.answer("Список устарел, откройте пакеты заново.", show_alert=True)
        return

    await state.set_state(Panel.value)
    await state.update_data(field=f"game_price:{category_id}:{offer['offer_id']}")
    await safe_edit(
        call,
        f"✏️ <b>Цена: {offer['name']}</b>\n\n"
        f"├ Сейчас: <b>{fmt(offer['price'])}</b>\n"
        f"├ Себестоимость: <b>{fmt(offer['cost'])}</b>\n"
        f"└ По наценке: <b>{fmt(offer['auto'])}</b>\n\n"
        "<blockquote>Пришлите цену в сомони, например <code>25</code> "
        "или <code>24.50</code>.</blockquote>",
        back_kb(f"pn:game_packs:{category_id}", "❌ Отмена"),
    )
    await call.answer()


#: Разобранный прайс до подтверждения: игра -> план. Держим в памяти,
#: а не в базе: это черновик на минуту, и переживать перезапуск ему
#: незачем — владелец просто пришлёт список заново.
_plans: dict[str, object] = {}


@router.callback_query(F.data.startswith("pn:price_list:"))
async def cb_price_list(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Прайс списком: вбивать тридцать цен по одной — полчаса тыканья."""
    code = call.data.split(":", 2)[2]
    game = await db.get_game(conn, code)
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return

    await state.set_state(Panel.value)
    await state.update_data(field=f"price_list:{code}")
    await safe_edit(
        call,
        f"📋 <b>Прайс списком: {game.title}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        "Пришлите весь прайс одним сообщением — по строке на пакет:\n\n"
        "<blockquote><code>60 UC - 10\n"
        "120 UC - 21\n"
        "325 UC - 45\n"
        "660 UC - 89</code></blockquote>\n"
        "<blockquote>Цена в сомони. Слово «сомонӣ» или «с.» в конце "
        "писать можно — я его пойму.\n\n"
        "Пакеты, которых нет в списке, останутся как были. Прежде чем "
        "поставить цены, покажу, что получилось.</blockquote>",
        back_kb(f"pn:game_packs:{code}", "❌ Отмена"),
    )
    await call.answer()


def _price_preview(game: db.Game, plan) -> str:
    """Что получится из прайса — до того, как цены встанут."""
    lines = []
    for row in plan.matched[:25]:
        was = row.offer["price"]
        mark = "✅" if was != row.price else "▫️"
        lines.append(
            f"{mark} {esc(row.offer['name'])} — <b>{fmt(row.price)}</b>"
            + (f" <s>{fmt(was)}</s>" if was != row.price else " <i>(так и было)</i>")
        )
    if len(plan.matched) > 25:
        lines.append(f"<i>…и ещё {len(plan.matched) - 25}</i>")

    text = (
        f"📋 <b>Прайс: {game.title}</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + ("\n".join(lines) if lines else "<i>Ни одна строка не легла на пакет.</i>")
    )

    if plan.lost:
        misses = "\n".join(
            f"├ <code>{esc(row.line)}</code> — <i>{row.why}</i>"
            for row in plan.lost[:8]
        )
        text += (
            f"\n\n⚠️ <b>Не нашёл пакет — {len(plan.lost)}</b>\n{misses}"
            + (f"\n└ <i>…и ещё {len(plan.lost) - 8}</i>" if len(plan.lost) > 8 else "")
        )
    if plan.bad:
        skipped = "\n".join(f"├ <code>{esc(line)}</code>" for line in plan.bad[:5])
        text += (
            f"\n\n🤷 <b>Не понял строку — {len(plan.bad)}</b>\n{skipped}"
            + (f"\n└ <i>…и ещё {len(plan.bad) - 5}</i>" if len(plan.bad) > 5 else "")
        )

    text += (
        "\n\n<blockquote>Поменяются "
        f"<b>{len(plan.changed)}</b> из {len(plan.matched)} найденных. "
        "Остальные пакеты останутся как были.\n\n"
        "Эти цены держатся сами: смена курса и наценки их не трогает. "
        "Вернуть пакету цену по наценке — «♻️ Вернуть как у поставщика» "
        "в его карточке.\n\n"
        "«🏷 Цены и названия» заодно переименует пакеты так, как они "
        "написаны в прайсе — клиенты увидят ваши названия, а не "
        "длинные поставщика.</blockquote>"
    )
    return text


@router.callback_query(F.data.startswith(("pn:price_go:", "pn:price_name:")))
async def cb_price_apply(
    call: CallbackQuery, conn: aiosqlite.Connection, provider
) -> None:
    """Поставить цены из разобранного прайса.

    Вторая кнопка забирает из прайса и названия: у поставщика пакет
    зовётся «PUBG Mobile 60 UC», и на кнопке от этого остаётся огрызок.
    В прайсе владелец уже написал, как надо — «60 UC».
    """
    code = call.data.split(":", 2)[2]
    plan = _plans.pop(code, None)
    game = await db.get_game(conn, code)
    if plan is None or game is None:
        await call.answer("Прайс устарел, пришлите его заново.", show_alert=True)
        return

    rename = call.data.startswith("pn:price_name:")
    for row in plan.matched:
        await db.set_game_price(conn, code, row.offer["offer_id"], row.price)
        if rename:
            await db.set_game_offer_title(conn, code, row.offer["offer_id"],
                                          row.name[:64])

    await call.answer(
        (f"Цены и названия: {len(plan.matched)}" if rename
         else f"Цены поставлены: {len(plan.matched)}")
    )
    await show_packs(call, conn, game, provider)


@router.callback_query(F.data.startswith("pn:game_margin:"))
async def cb_game_margin(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Наценка всей игры. Пакеты со своей ценой она не трогает."""
    game = await db.get_game(conn, call.data.split(":", 2)[2])
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    await state.set_state(Panel.value)
    await state.update_data(field=f"game_margin:{game.category_id}")
    await safe_edit(
        call,
        f"📈 <b>Наценка: {game.title}</b>\n\n"
        f"Сейчас: <b>{game.margin or runtime.margin_percent()}%</b>"
        + ("" if game.margin else " <i>(общая)</i>")
        + "\n\n<blockquote>Пришлите процент только для этой игры, "
          "или <code>0</code> — брать общую.\n\nПакеты со своей ценой "
          "наценка не трогает.</blockquote>",
        back_kb(f"pn:game:{game.category_id}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:game_field:"))
async def cb_game_field(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection, provider
) -> None:
    """Как поставщик называет поле с ID игрока: user_id, player_id, uid…"""
    game = await db.get_game(conn, call.data.split(":", 2)[2])
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return

    from app.services import games as gsvc
    from app.services import suppliers

    guess = await gsvc.detect_fields(suppliers.for_games(provider),
                                     game.category_id)
    hint = ""
    if guess and guess != game.field_names:
        wanted = ",".join(guess)
        asked = ", ".join(gsvc.field_label(name) for name in guess)
        hint = (f"\n\n<blockquote>Поставщик ждёт <code>{wanted}</code> "
                f"({asked}) — пришлите это.</blockquote>")
    elif guess:
        hint = "\n\n<blockquote>Поставщик подтверждает текущее.</blockquote>"

    await state.set_state(Panel.value)
    await state.update_data(field=f"game_field:{game.category_id}")
    await safe_edit(
        call,
        f"🔤 <b>Поля для ID: {game.title}</b>\n\n"
        f"Сейчас: <code>{game.field}</code>\n"
        f"<i>спрашиваем: {_fields_asked(game)}</i>{hint}\n\n"
        "<blockquote>Обычно это <code>user_id</code>, <code>player_id</code> "
        "или <code>uid</code>. Части игр нужна пара — ID игрока и номер "
        "сервера: пришлите оба через запятую.\n\n"
        "<code>player_id, server_id</code>\n\n"
        "Точное имя пишет сам поставщик в отказе вида "
        "«Field ... is required».</blockquote>",
        back_kb(f"pn:game:{game.category_id}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:game_emoji:"))
async def cb_game_emoji(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    """Свой премиум-значок на кнопке игры."""
    game = await db.get_game(conn, call.data.split(":", 2)[2])
    if game is None:
        await call.answer("Игра не найдена.", show_alert=True)
        return

    from app.emoji import GAME_EMOJI, custom_id, game_key, premium_on

    guess = game_key(game.category_id)
    now = game.emoji or (custom_id(guess) if guess else "")
    source = ("<i>задан вручную</i>" if game.emoji
              else ("<i>подобран по коду игры</i>" if now else ""))

    warn = ""
    if not premium_on():
        warn = ("\n\n[[warn]] Премиум-эмодзи сейчас выключены — значок "
                "не появится, пока вы не включите их в разделе "
                "«🎨 Оформление».")

    await state.set_state(Panel.value)
    await state.update_data(field=f"game_emoji:{game.category_id}")
    await safe_edit(
        call,
        substitute(
            f"😀 <b>Значок: {game.title}</b>\n"
            f"<code>{texts.LINE}</code>\n\n"
            f"└ Сейчас: <code>{now or 'обычный из названия'}</code> {source}"
            f"{warn}\n\n"
            "<blockquote>Пришлите <b>ID премиум-эмодзи</b> — только цифры.\n\n"
            "Где его взять: отправьте себе премиум-эмодзи, перешлите "
            "его боту @idstickerbot — он пришлёт ID.\n\n"
            "Чтобы убрать свой значок, пришлите <code>-</code>.\n\n"
            "На кнопке помещается ровно один значок — это ограничение "
            "Telegram, а не бота.</blockquote>"
        ),
        back_kb(f"pn:game:{game.category_id}", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:game_del:"))
async def cb_game_delete(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    code = call.data.split(":", 2)[2]
    await db.delete_game(conn, code)
    await db.load_game_titles(conn)
    await call.answer("Удалена")
    await safe_edit(call, substitute(await games_text(conn)), await games_kb(conn))


# ═══════════════════════════════════════════════════════ балансы ключей


@router.callback_query(F.data == "pn:keys")
async def cb_keys(call: CallbackQuery, provider) -> None:
    """Сколько осталось на каждом ключе — поставщик и сервис ников."""
    await safe_edit(call, "💳 Смотрю балансы…", back_kb("pn:home", "‹ В панель"))
    await call.answer()

    lines: list[str] = []

    # ---- счета поставщика: с них идёт выдача
    from app.services import suppliers

    accounts = [("Звёзды и Premium", provider)]
    if suppliers.has_own_games_key():
        accounts.append(("Игры", suppliers.for_games(provider)))
    else:
        accounts[0] = ("Все товары", provider)

    for title, client in accounts:
        lines.append(await _balance_line(title, client))

    # ---- сервис ников: лимит бесплатного плана
    key = runtime.get("gameskinbo_key") or settings.gameskinbo_key
    if not key:
        lines.append("⚪️ <b>Ники Free Fire</b>\n└ ключ не задан — "
                     "работает запасной источник")
    else:
        data = await nicknames.usage(key)
        if data is None:
            lines.append("🔴 <b>Ники Free Fire</b>\n└ сервис не ответил")
        else:
            left = data.get("remaining")
            lines.append(
                f"{'🟢' if (left or 0) > 10 else '🟠'} <b>Ники Free Fire</b>\n"
                f"├ Использовано: <b>{data.get('used', '?')}</b> "
                f"из <b>{data.get('limit', '?')}</b>\n"
                f"└ Осталось: <b>{left}</b> · план {data.get('plan', '—')}"
            )

    await safe_edit(
        call,
        "💳 <b>Балансы ключей</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        + "\n\n".join(lines)
        + "\n\n<blockquote>FazerCards — деньги, с которых идёт выдача. "
          "Когда у игр свой ключ, они списываются с его счёта — так "
          "расходы партнёров не смешиваются.\n\nНики Free Fire тратят "
          "лимит только на новые ID: повторы полчаса берутся из памяти."
          "</blockquote>\n\n"
        + _rate_line(),
        keys_kb(),
    )


def _rate_line() -> str:
    """Темп обращений к поставщику: сколько разрешено и сколько занято."""
    from app.services import ratelimit

    limit = ratelimit.limit_now()
    used = max((r.used for r in ratelimit._by_key.values()), default=0)
    return (f"⏱ <b>Темп к поставщику</b>\n"
            f"├ Разрешено: <b>{limit}</b> запросов в минуту\n"
            f"└ Занято сейчас: <b>{used}</b>\n\n"
            "<blockquote>Лишние запросы не теряются — они ждут очереди и "
            "уходят следующей минутой. Заказы клиентов идут вперёд "
            "проверок статуса.</blockquote>")


def keys_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏱ Запросов в минуту",
                                callback_data="pn:set:supplier_rate_per_min"))
    kb.row(InlineKeyboardButton(text="‹ В панель", callback_data="pn:home"))
    return kb.as_markup()


async def _balance_line(title: str, client) -> str:
    """Строка счёта поставщика с пересчётом в сомони."""
    try:
        balance = await client.get_balance()
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        return (f"🔴 <b>FazerCards · {title}</b>\n└ не ответил: "
                f"<code>{str(exc)[:200]}</code>")

    somoni = ""
    rate = runtime.usd_rate()
    number = _usd_number(balance)
    if rate > 0 and number is not None:
        somoni = f"\n└ Это примерно <b>{fmt(int(number * rate))}</b>"
    return f"🟢 <b>FazerCards · {title}</b>\n├ Баланс: <b>{balance}</b>{somoni}"


def _usd_number(balance: str) -> float | None:
    """Вытащить число из строки вида «119.40 USD»."""
    import re as _re

    found = _re.search(r"-?\d+(?:\.\d+)?", str(balance))
    return float(found.group()) if found else None


@router.callback_query(F.data == "pn:games_check")
async def cb_games_check(call: CallbackQuery, conn: aiosqlite.Connection, provider) -> None:
    """Полная проверка поставщика игр: счёт, каталог, пакеты, последние заказы.

    Нужна, когда заказы «не доходят»: показывает, на каком именно шаге
    всё останавливается, вместо догадок.
    """
    await safe_edit(call, "🔌 Проверяю поставщика игр…", back_kb("pn:games", "‹ Назад"))
    await call.answer()

    from app.handlers.games import offers_of
    from app.services import games as gsvc
    from app.services import suppliers

    client = suppliers.for_games(provider)
    own = suppliers.has_own_games_key()
    lines: list[str] = [
        "🔑 Ключ: " + ("<b>отдельный для игр</b>" if own else
                       "<i>общий со звёздами</i>")
    ]

    # ---- 1. деньги на счету, с которого идёт выдача
    try:
        balance = await client.get_balance()
    except Exception as exc:  # noqa: BLE001 — показать админу любую поломку
        lines.append(f"💳 Баланс: ❌ <code>{str(exc)[:160]}</code>")
    else:
        lines.append(f"💳 Баланс: <b>{balance}</b>")
        if _usd_number(balance) == 0:
            lines.append("   ❗️ <b>Счёт пуст — заказы не пройдут</b>")

    # ---- 2. какие игры вообще знает поставщик
    try:
        catalog = await client.game_catalog()
    except Exception as exc:  # noqa: BLE001
        catalog = []
        lines.append(f"📚 Каталог игр: ❌ <code>{str(exc)[:160]}</code>")
    else:
        lines.append(f"📚 Каталог игр: <b>{len(catalog)}</b>")

    known = {item["category_id"] for item in catalog}

    # ---- 3. каждая наша игра: есть ли она у поставщика и есть ли пакеты
    for game in await db.list_games(conn):
        mark = "✅" if game.category_id in known else "⚠️"
        note = "" if game.category_id in known else " <i>— нет в каталоге</i>"
        try:
            # Владельцу — живьём: он и смотрит затем, чтобы увидеть,
            # что у поставщика прямо сейчас.
            offers = await offers_of(client, game, conn)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{mark} <b>{game.title}</b>{note}\n"
                         f"   ❌ пакеты: <code>{str(exc)[:140]}</code>")
            continue
        found = await gsvc.detect_fields(client, game.category_id)
        field_note = ""
        if found and found != game.field_names:
            field_note = (f"\n   ⚠️ поля <code>{game.field}</code>, "
                          f"а нужно <code>{','.join(found)}</code>")
        lines.append(
            f"{mark} <b>{game.title}</b>{note}\n"
            f"   пакетов: <b>{len(offers)}</b> · поля: <code>{game.field}</code>"
            f"{field_note}"
        )

    # ---- 3.5 мгновенные отчёты: с ними выдача видна сразу
    from app.services import webhook as hookmod

    seen, _ = await db.events_seen(conn)
    if not hookmod.secret() or (runtime.get_int("webhook_port") or 0) <= 0:
        lines.append("🔔 Мгновенные отчёты: <i>выключены</i>\n"
                     "   <i>бот сам опрашивает поставщика — это дольше</i>")
    else:
        lines.append(f"🔔 Мгновенные отчёты: <b>включены</b> · "
                     f"принято: <b>{seen}</b>"
                     + ("" if seen else "\n   ⚠️ <i>ни одного отчёта ещё не "
                        "пришло — проверьте адрес в кабинете поставщика</i>"))

    lines.append(f"⏱ Предупредить, если нет ответа поставщика: <b>{gsvc.timeout_minutes()} мин</b> "
                 "(деньги возвращаются только если поставщик отменил)")

    # ---- 4. чем кончились последние заказы
    recent = await db.last_game_orders(conn)
    if recent:
        rows = []
        for order in recent:
            external = order.fragment_order_id or "<i>номера нет</i>"
            rows.append(f"├ №{order.id} {order.status_title} · {external}")
        lines.append("📦 <b>Последние заказы</b>\n" + "\n".join(rows))

    await safe_edit(
        call,
        "🔌 <b>Проверка поставщика игр</b>\n"
        f"<code>{texts.LINE}</code>\n\n" + "\n\n".join(lines),
        back_kb("pn:games", "‹ К играм"),
    )


# ══════════════════════════════════════════════════ API для разработчиков


@router.callback_query(F.data == "pn:api")
async def cb_api(call: CallbackQuery, state: FSMContext,
                 conn: aiosqlite.Connection) -> None:
    """Общий экран API: включён ли, кто им пользуется, что происходит."""
    from app.api import server as api_server
    from app.services import webhook as hook_service

    await state.clear()
    on = api_server.enabled()
    keys = await db.all_api_keys(conn, limit=200)
    owners = {key.user_id for key in keys}
    live = sum(1 for key in keys if key.live)
    url = api_server.base_url()
    port = runtime.get_int("webhook_port") or settings.webhook_port

    async with conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(status >= 400), 0) AS bad "
        "FROM api_requests"
    ) as cur:
        req = dict(await cur.fetchone())
    async with conn.execute(
        """SELECT COUNT(*) AS cnt, COALESCE(SUM(o.price), 0) AS sold
           FROM api_orders a JOIN orders o ON o.id = a.order_id
           WHERE o.status = ?""", (db.ORDER_DELIVERED,)
    ) as cur:
        sales = dict(await cur.fetchone())

    waiting = len(await db.refund_requests(conn, "pending", limit=99))

    kb = InlineKeyboardBuilder()
    kb.row(btn("🚫 Выключить API" if on else "✅ Включить API", "pn:api_toggle",
               style=DANGER if on else SUCCESS))
    kb.row(btn("👥 Клиенты API", "pn:api_clients"),
           btn("📋 Запросы", "pn:api_log"))
    kb.row(btn("⚡️ Лимит запросов", "pn:api_rate"),
           btn("💰 Наценка", "pn:set:api_margin"))
    kb.row(btn("↩️ Заявки на возврат" + (f" ({waiting})" if waiting else ""),
               "pn:refunds", style=DANGER if waiting else None))
    kb.row(btn("🎮 Каталог игр: "
               + ("весь у поставщика" if runtime.get_bool("api_all_games")
                  else "как в боте"),
               "pn:api_allgames"))
    kb.row(btn("🌐 За обратным прокси: "
               + ("да" if runtime.get_bool("api_behind_proxy") else "нет"),
               "pn:api_proxy"))
    if url:
        kb.row(InlineKeyboardButton(text="🖥 Кабинет", url=f"{url}/cabinet"),
               InlineKeyboardButton(text="📖 Документация", url=f"{url}/docs"))
    kb.row(btn(labeled("back", "Назад"), "pn:home"))

    # Наценка для разработчиков: 0 — продаём им по витрине бота.
    margin = runtime.api_margin_percent()
    pricing_note = (f"себестоимость + {margin}%" if margin else "как в боте")

    trouble = ""
    if on and port <= 0:
        trouble = ("\n\n⚠️ <b>Порт не задан</b> — сервер не поднимется. "
                   "Задайте <code>webhook_port</code> в .env.")
    elif on and not hook_service.public_url():
        trouble = ("\n\n⚠️ <b>Публичный адрес не задан</b> — разработчикам "
                   "нечего дать. Задайте <code>webhook_public_url</code>.")

    await safe_edit(
        call,
        f"🧩 <b>API для разработчиков</b>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"Состояние: <b>{'🟢 включён' if on else '⚪️ выключен'}</b>\n"
        f"Адрес: <code>{url or '—'}</code>\n"
        f"Цены разработчикам: <b>{pricing_note}</b>\n\n"
        f"👥 <b>Клиенты</b>\n"
        f"├ Разработчиков: <b>{len(owners)}</b>\n"
        f"└ Живых ключей: <b>{live}</b> из {len(keys)}\n\n"
        f"🔁 <b>Запросы</b>\n"
        f"├ Всего: <b>{req['total']}</b>\n"
        f"└ С ошибкой: <b>{req['bad']}</b>\n\n"
        f"📦 <b>Продажи через API</b>\n"
        f"├ Заказов: <b>{sales['cnt']}</b>\n"
        f"└ На сумму: <b>{fmt(sales['sold'])}</b>"
        + trouble
        + "\n\n<blockquote>Разработчик заходит в бота, жмёт /api и сам "
          "создаёт ключ. Покупает он со своего баланса — того же, что "
          "и в боте.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:api_toggle")
async def cb_api_toggle(call: CallbackQuery, state: FSMContext,
                        conn: aiosqlite.Connection) -> None:
    from app.api import server as api_server

    on = not api_server.enabled()
    await runtime.set_value(conn, "api_enabled", "1" if on else "0")
    await call.answer(
        "API включён — перезапустите бота, чтобы поднялся сервер" if on
        else "API выключен: запросы сразу получают отказ"
    )
    await cb_api(call, state, conn)


@router.callback_query(F.data == "pn:ru_photo")
async def cb_ru_photo(call: CallbackQuery, state: FSMContext) -> None:
    """Картинка-пример: где в чеке Сбербанка искать сумму в сомони."""
    await state.set_state(Panel.ru_photo)
    has = runtime.get("ru_example_photo")
    await safe_edit(
        call,
        f"🖼 <b>Пример чека из России</b>\n<code>{texts.LINE}</code>\n\n"
        + ("Сейчас картинка задана — клиент видит её, когда бот просит "
           "сумму.\n\n" if has else "Сейчас картинки нет.\n\n")
        + "<blockquote>Пришлите скриншот чека Сбербанка, на котором "
          "видно строку «Зачислено получателю» и сумму в TJS. Обведите "
          "её — так человек не перепутает с рублями и комиссией.\n\n"
          "Показать короче, чем объяснить: рядом в чеке стоят три разные "
          "суммы, и ошибиться в них легко.</blockquote>\n\n"
        + ("<i>Пришлите</i> <code>-</code><i>, чтобы убрать картинку.</i>"
           if has else "<i>Пришлите картинку сообщением.</i>"),
        back_kb("pn:pay", "❌ Отмена"),
    )
    await call.answer()


@router.message(Panel.ru_photo, F.photo)
async def on_ru_photo(message: Message, state: FSMContext,
                      conn: aiosqlite.Connection) -> None:
    await runtime.set_value(conn, "ru_example_photo",
                            message.photo[-1].file_id)
    await state.clear()
    await message.answer(
        "✅ <b>Картинка сохранена</b>\n\n"
        "Теперь она показывается клиенту вместе с вопросом о сумме.",
        reply_markup=back_kb("pn:pay", "‹ К оплате"),
    )


@router.message(Panel.ru_photo, F.text)
async def on_ru_photo_text(message: Message, state: FSMContext,
                           conn: aiosqlite.Connection) -> None:
    if message.text.strip() != "-":
        await message.answer("❌ Нужна картинка сообщением "
                             "или <code>-</code>, чтобы убрать.")
        return
    await runtime.set_value(conn, "ru_example_photo", "")
    await state.clear()
    await message.answer("✅ Картинка убрана.",
                         reply_markup=back_kb("pn:pay", "‹ К оплате"))


@router.callback_query(F.data == "pn:refunds")
async def cb_refunds(call: CallbackQuery, state: FSMContext,
                     conn: aiosqlite.Connection) -> None:
    """Просьбы разработчиков вернуть деньги за выполненный заказ."""
    await state.clear()
    rows = await db.refund_requests(conn, "pending", limit=20)

    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(btn(f"↩️ {row['ref']} — {fmt(row['price'])}",
                   f"pn:refund:{row['ref']}"))
    kb.row(btn(labeled("back", "Назад"), "pn:api"))

    body = "\n\n".join(
        f"<code>{row['ref']}</code> — <b>{fmt(row['price'])}</b>\n"
        f"<i>{esc(row['product_id'])}</i>\n"
        f"<blockquote expandable>{esc(row['reason'])}</blockquote>"
        for row in rows
    ) or "<i>Заявок нет.</i>"

    await safe_edit(
        call,
        f"↩️ <b>Заявки на возврат</b>\n<code>{texts.LINE}</code>\n\n{body}\n\n"
        "<blockquote>Это просьбы разработчиков вернуть деньги за уже "
        "выполненный заказ. Товар поставщику мы оплатили, поэтому решение "
        "ваше: «Вернуть» отдаёт деньги из вашего кармана.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:refund:"))
async def cb_refund_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    ref = call.data.split(":", 2)[2]
    ask = await db.refund_request(conn, ref)
    if not ask:
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    row = await db.api_order_by_ref(conn, ref)
    order = await db.get_order(conn, row["order_id"]) if row else None

    kb = InlineKeyboardBuilder()
    kb.row(btn("✅ Вернуть деньги", f"pn:refund_ok:{ref}", style=SUCCESS),
           btn("🚫 Отказать", f"pn:refund_no:{ref}", style=DANGER))
    kb.row(btn(labeled("back", "К заявкам"), "pn:refunds"))

    await safe_edit(
        call,
        f"↩️ <b>Заявка {esc(ref)}</b>\n<code>{texts.LINE}</code>\n\n"
        f"├ Товар: <code>{esc(row['product_id'] if row else '—')}</code>\n"
        f"├ Кому ушло: <code>{esc(row['customer'] if row else '—')}</code>\n"
        f"├ Сумма: <b>{fmt(order.price if order else 0)}</b>\n"
        f"└ Статус заказа: <b>{db.ORDER_TITLES.get(order.status if order else '', '—')}</b>\n\n"
        f"<b>Причина:</b>\n<blockquote expandable>{esc(ask['reason'])}</blockquote>\n\n"
        "<blockquote>Проверьте у поставщика, дошёл ли товар. Если дошёл — "
        "возврат это подарок за ваш счёт.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith(("pn:refund_ok:", "pn:refund_no:")))
async def cb_refund_decide(call: CallbackQuery, state: FSMContext,
                           conn: aiosqlite.Connection, bot=None) -> None:
    action, ref = call.data.rsplit(":", 1)
    approved = action.endswith("refund_ok")

    if not await db.decide_refund(conn, ref, approved=approved):
        await call.answer("Заявку уже закрыли.", show_alert=True)
        await cb_refunds(call, state, conn)
        return

    if approved:
        from app.services import delivery

        row = await db.api_order_by_ref(conn, ref)
        order = await db.get_order(conn, row["order_id"]) if row else None
        # Возврат идёт общей дорогой: статус меняется под условием, деньги
        # пишутся в выписку, вебхук уходит сам. Свой отдельный возврат
        # здесь разошёлся бы с остальными пятью путями.
        if order and await db.transition_order(
            conn, order.id, expected=order.status, new=db.ORDER_REFUNDED,
            error="возврат по заявке разработчика",
        ):
            await delivery._give_back(conn,
                                      await db.get_order(conn, order.id) or order)

    await cb_refunds(call, state, conn)
    await call.answer("Деньги возвращены" if approved else "Отказано",
                      show_alert=True)


@router.callback_query(F.data == "pn:api_allgames")
async def cb_api_all_games(call: CallbackQuery, state: FSMContext,
                           conn: aiosqlite.Connection) -> None:
    """Отдавать разработчикам весь каталог поставщика или только своё."""
    from app.api import catalog as api_catalog

    on = runtime.get_bool("api_all_games")
    await runtime.set_value(conn, "api_all_games", "0" if on else "1")
    api_catalog.forget_full()
    await cb_api(call, state, conn)
    await call.answer("Теперь как в боте" if on else "Теперь весь каталог")


@router.callback_query(F.data == "pn:api_proxy")
async def cb_api_proxy(call: CallbackQuery, state: FSMContext,
                       conn: aiosqlite.Connection) -> None:
    """Верить ли X-Forwarded-For.

    Включать только если перед ботом правда стоит nginx: иначе любой
    сможет подделать заголовок и обойти защиту от подбора ключа.
    """
    on = not runtime.get_bool("api_behind_proxy")
    await runtime.set_value(conn, "api_behind_proxy", "1" if on else "0")
    await call.answer(
        "Адрес берём из X-Forwarded-For" if on
        else "Адрес берём из самого соединения"
    )
    await cb_api(call, state, conn)


@router.callback_query(F.data == "pn:api_rate")
async def cb_api_rate(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Panel.value)
    await state.update_data(field="api_rate_per_min")
    await safe_edit(
        call,
        f"⚡️ <b>Лимит запросов</b>\n<code>{texts.LINE}</code>\n\n"
        f"Сейчас: <b>{runtime.get_int('api_rate_per_min', 60)}</b> "
        "запросов в минуту на один ключ.\n\n"
        "<blockquote>Пришлите число. Лимит считается «дырявым ведром»: "
        "ровная нагрузка проходит вся, а короткий всплеск — пока есть "
        "запас.</blockquote>",
        back_kb("pn:api", "❌ Отмена"),
    )
    await call.answer()


@router.callback_query(F.data == "pn:api_clients")
async def cb_api_clients(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    keys = await db.all_api_keys(conn, limit=30)
    kb = InlineKeyboardBuilder()
    rows = []
    for key in keys:
        user = await db.get_user(conn, key.user_id)
        who = f"@{user.username}" if user and user.username else str(key.user_id)
        rows.append(
            f"{'🟢' if key.live else '⚪️'} <code>{key.masked}</code> — {who}\n"
            f"   <i>баланс {fmt(user.balance if user else 0)}, "
            f"запросов {key.requests}</i>"
        )
        kb.row(btn(f"{'🟢' if key.live else '⚪️'} {who} — {key.label or key.masked}",
                   f"pn:api_key:{key.id}"))
    kb.row(btn(labeled("back", "Назад"), "pn:api"))

    await safe_edit(
        call,
        f"👥 <b>Клиенты API</b>\n<code>{texts.LINE}</code>\n\n"
        + ("\n\n".join(rows) if rows else "<i>Ключей ещё никто не создал.</i>"),
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:api_key:"))
async def cb_api_key(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Карточка чужого ключа: что с ним происходит и как его закрыть."""
    from app.api import guard
    from app.api import keys as apikeys

    raw = call.data.rsplit(":", 1)[1]
    key = await db.get_api_key(conn, int(raw)) if raw.isdigit() else None
    if key is None:
        await call.answer("Ключ не найден.", show_alert=True)
        return

    user = await db.get_user(conn, key.user_id)
    orders = await db.api_orders_of(conn, key.user_id, limit=500)
    done = [row for row in orders if row["status"] == db.ORDER_DELIVERED]
    stats = await db.api_stats(conn, key.user_id)

    kb = InlineKeyboardBuilder()
    kb.row(btn("⏸ Выключить ключ" if key.enabled else "▶️ Включить ключ",
               f"pn:api_kt:{key.id}",
               style=DANGER if key.enabled else SUCCESS))
    kb.row(btn("🗑 Отозвать навсегда", f"pn:api_kill:{key.id}", style=DANGER))
    if user:
        kb.row(btn("🚫 Заблокировать клиента" if not user.is_banned
                   else "✅ Разблокировать клиента",
                   f"pn:ban:{user.id}", style=DANGER if not user.is_banned else SUCCESS))
        kb.row(btn("👤 Карточка клиента", f"pn:user:{user.id}"))
    kb.row(btn(labeled("back", "Назад"), "pn:api_clients"))

    await safe_edit(
        call,
        f"🔑 <b>{key.label or 'Ключ'}</b> <code>{key.masked}</code>\n"
        f"<code>{texts.LINE}</code>\n\n"
        f"├ Владелец: <code>{key.user_id}</code>"
        + (f" @{user.username}" if user and user.username else "") + "\n"
        f"├ Баланс: <b>{fmt(user.balance if user else 0)}</b>\n"
        f"├ Состояние: <b>{'🟢 работает' if key.live else '⚪️ не работает'}</b>\n"
        f"├ Создан: <i>{key.created_at[:16].replace('T', ' ')}</i>\n"
        f"├ Последний запрос: <i>"
        + ((key.last_used_at or "")[:16].replace("T", " ") or "не было") + "</i>\n"
        f"├ Запросов ключом: <b>{key.requests}</b>\n"
        f"├ Всего по клиенту: <b>{stats['total']}</b> "
        f"<i>(ошибок {stats['errors']})</i>\n"
        f"├ Заказов: <b>{len(orders)}</b>, выполнено <b>{len(done)}</b>\n"
        f"└ Продано на: <b>{fmt(sum(row['price'] for row in done))}</b>"
        + ("\n\n🚫 <b>Клиент заблокирован</b> — все его ключи отказывают."
           if user and user.is_banned else ""),
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith(("pn:api_kt:", "pn:api_kill:")))
async def cb_api_key_act(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    from app.api import guard
    from app.api import keys as apikeys

    raw = call.data.rsplit(":", 1)[1]
    key = await db.get_api_key(conn, int(raw)) if raw.isdigit() else None
    if key is None:
        await call.answer("Ключ не найден.", show_alert=True)
        return

    if call.data.startswith("pn:api_kill:"):
        await db.revoke_api_key(conn, key.id)
        note = "Ключ отозван"
    else:
        await db.set_api_key_enabled(conn, key.id, not key.enabled)
        note = "Ключ выключен" if key.enabled else "Ключ включён"

    # Кеш проверенных ключей сбрасываем сразу: выключают ключ как раз
    # тогда, когда он утёк, и лишняя минута жизни тут лишняя.
    apikeys.forget(key.id)
    guard.forget_rate(key.id)
    await call.answer(note)
    call.data = f"pn:api_key:{key.id}"
    await cb_api_key(call, conn)


@router.callback_query(F.data == "pn:api_log")
async def cb_api_log(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Последние запросы. Ни ключа, ни тела — только кто, куда и чем кончилось."""
    async with conn.execute(
        "SELECT * FROM api_requests ORDER BY id DESC LIMIT 15"
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]

    body = "\n".join(
        f"<code>{row['created_at'][11:19]}</code> "
        f"{row['method']} {esc(row['path'])} — <b>{row['status']}</b>"
        + (f" <i>{esc(row['error'])}</i>" if row["error"] else "")
        + f" <i>{row['ms']} мс</i>\n"
        f"   <i>{esc(row['ip'] or '—')} · "
        f"{esc((row['user_agent'] or '—')[:40])}</i>"
        for row in rows
    ) or "<i>Запросов ещё не было.</i>"

    await safe_edit(
        call,
        f"📋 <b>Последние запросы</b>\n<code>{texts.LINE}</code>\n\n{body}\n\n"
        "<blockquote>Ключей и тел запросов в журнале нет и не будет: "
        "ключ в логах — это ключ, доступный всем, у кого есть логи."
        "</blockquote>",
        back_kb("pn:api", "‹ Назад"),
    )
    await call.answer()


# ══════════════════════════════════════════ зачисления от банка


#: Какие платежи ждут решения владельца.
BANK_OPEN = (db.BANK_HOLD, db.BANK_AMBIGUOUS, db.BANK_UNKNOWN,
             db.BANK_FAILED)


@router.callback_query(F.data == "pn:bank")
async def cb_bank(call: CallbackQuery, state: FSMContext,
                  conn: aiosqlite.Connection) -> None:
    """Что пришло от банка и что юзербот с этим сделал."""
    await state.clear()
    stats = await db.bank_payment_stats(conn)
    rows = await db.list_bank_payments(conn, limit=10)
    open_count = sum(stats.get(key, 0) for key in BANK_OPEN)

    senders = await db.list_senders(conn, limit=200)

    kb = InlineKeyboardBuilder()
    if open_count:
        kb.row(btn(f"⚠️ Требуют проверки ({open_count})", "pn:bank_open",
                   style=DANGER))
    if senders:
        kb.row(btn(f"🪪 Знакомые плательщики ({len(senders)})",
                   "pn:bank_who"))
    kb.row(btn("📥 К заявкам", "pn:deposits", style=PRIMARY))
    kb.row(btn(labeled("back", "Назад"), "pn:home"))

    body = "\n".join(_bank_line(row) for row in rows) or (
        "<i>Уведомлений от банка ещё не приходило.</i>\n\n"
        "Юзербот запускается отдельно: <code>stars-bot userbot</code>"
    )

    await safe_edit(
        call,
        f"🏦 <b>Оплаты от банка</b>\n<code>{texts.LINE}</code>\n\n"
        f"✅ Зачислено само: <b>{stats.get(db.BANK_MATCHED, 0)}</b>\n"
        f"📸 Ждут чек: <b>{stats.get(db.BANK_HOLD, 0)}</b>\n"
        f"⚠️ Несколько заявок: <b>{stats.get(db.BANK_AMBIGUOUS, 0)}</b>\n"
        f"❔ Без заявки: <b>{stats.get(db.BANK_UNKNOWN, 0)}</b>\n"
        f"🚫 Не разобрал: <b>{stats.get(db.BANK_FAILED, 0)}</b>\n\n"
        f"<b>Последние</b>\n{body}\n\n"
        "<blockquote>Деньги зачисляются сами, когда сошлось всё сразу: "
        "заявка на эту сумму ровно одна и клиент прислал чек. Иначе "
        "платёж ждёт вас — выбрать наугад значило бы отдать чужие "
        "деньги.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


def _bank_line(row: db.BankPayment) -> str:
    mark = db.BANK_TITLES.get(row.status, row.status)[:2]
    when = (row.bank_time or row.seen_at[11:16]).strip()
    text = f"{mark} <b>{fmt(row.amount)}</b> · <i>{esc(when)}</i>"
    if row.sender:
        text += f" · <code>{esc(row.sender)}</code>"
    if row.deposit_id:
        text += f" → заявка <code>№{row.deposit_id}</code>"
    elif row.note:
        text += f"\n   <i>{esc(row.note)}</i>"
    return text


@router.callback_query(F.data == "pn:bank_open")
async def cb_bank_open(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Платежи, с которыми юзербот не справился сам."""
    rows: list[db.BankPayment] = []
    for status in BANK_OPEN:
        rows += await db.list_bank_payments(conn, status=status, limit=8)
    rows.sort(key=lambda row: row.id, reverse=True)

    kb = InlineKeyboardBuilder()
    kb.row(btn("📥 К заявкам", "pn:deposits", style=PRIMARY))
    kb.row(btn(labeled("back", "Назад"), "pn:bank"))

    if not rows:
        body = "<i>Всё разобрано — ждать нечего.</i>"
    else:
        body = "\n\n".join(
            f"{db.BANK_TITLES.get(row.status, row.status)}\n"
            f"├ Сумма: <b>{fmt(row.amount)}</b>\n"
            + (f"├ Отправитель: <code>{esc(row.sender)}</code>\n"
               if row.sender else "")
            + (f"├ Карта: <code>••{esc(row.card_tail)}</code>\n"
               if row.card_tail else "")
            + (f"├ Код банка: <code>{esc(row.op_code)}</code>\n"
               if row.op_code else "")
            + f"└ <i>{esc(row.note or row.bank_time or '—')}</i>"
            for row in rows[:10]
        )

    await safe_edit(
        call,
        f"⚠️ <b>Платежи на проверке</b>\n<code>{texts.LINE}</code>\n\n{body}\n\n"
        "<blockquote>Сверьте отправителя с заявкой и подтвердите нужную "
        "в разделе «📥 Заявки» — или начислите вручную в «👥 Клиенты», "
        "если заявки не было вовсе.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "pn:bank_who")
async def cb_bank_senders(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    """Чьи счета мы уже узнаём.

    Каждая строка — клиент, которому больше не нужен чек: его счёт банк
    называет в каждом уведомлении, и мы его помним.
    """
    senders = await db.list_senders(conn, limit=25)

    rows = []
    kb = InlineKeyboardBuilder()
    for item in senders:
        user = await db.get_user(conn, item.user_id)
        who = f"@{user.username}" if user and user.username else str(item.user_id)
        rows.append(
            f"├ <code>{esc(item.sender)}</code> → {esc(who)}\n"
            f"│  <i>платежей: {item.payments}, последний "
            f"{(item.last_at or '')[:10]}</i>"
        )
        kb.row(btn(f"🗑 {item.sender} — отвязать",
                   f"pn:bank_cut:{item.sender}"))
    kb.row(btn(labeled("back", "Назад"), "pn:bank"))

    await safe_edit(
        call,
        f"🪪 <b>Знакомые плательщики</b>\n<code>{texts.LINE}</code>\n\n"
        + ("\n".join(rows) if rows else "<i>Пока никого.</i>")
        + "\n\n<blockquote>Эти клиенты платят без чека: их счёт банк "
          "называет в каждом уведомлении, и мы его узнаём.\n\n"
          "Отвязывайте, если клиент сменил карту или счёт попал не тому "
          "— тогда он снова пришлёт чек один раз.</blockquote>",
        kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pn:bank_cut:"))
async def cb_bank_unbind(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    await db.unbind_sender(conn, call.data.split(":", 2)[2])
    await call.answer("Отвязано — в следующий раз попросим чек")
    await cb_bank_senders(call, conn)
