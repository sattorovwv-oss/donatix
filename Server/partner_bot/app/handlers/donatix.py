"""Счёт у поставщика Donatix — прямо в боте, для админов.

Бот, запущенный через конструктор Donatix, покупает товары с баланса владельца
в Donatix. Здесь владелец видит этот баланс и пополняет его: выбирает способ
оплаты, вводит сумму в сомони, получает реквизиты, жмёт «Я оплатил» и
присылает чек. Чек уходит администратору Donatix, после подтверждения баланс
зачисляется сам.

Раздел включается, только когда задан DONATIX_URL (его ставит конструктор).
"""
from __future__ import annotations

import logging
import os
from html import escape as esc

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.services import access

log = logging.getLogger(__name__)
router = Router(name="donatix")
router.message.filter(F.from_user.func(lambda u: access.is_admin(u.id)))
router.callback_query.filter(F.from_user.func(lambda u: access.is_admin(u.id)))

TIMEOUT = aiohttp.ClientTimeout(total=30, connect=8)
MAX_RECEIPT = 10 * 1024 * 1024


class TopUp(StatesGroup):
    amount = State()    # ждём сумму в сомони
    receipt = State()   # ждём фото или PDF чека


def enabled() -> bool:
    return bool(os.environ.get("DONATIX_URL") and settings.fazer_api_key)


def _base() -> str:
    return os.environ.get("DONATIX_URL", "").rstrip("/")


class DonatixError(Exception):
    pass


async def _call(method: str, path: str, *, json: dict | None = None, data: aiohttp.FormData | None = None) -> dict:
    headers = {"X-API-Key": settings.fazer_api_key, "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers=headers) as s:
            async with s.request(method, _base() + path, json=json, data=data) as r:
                try:
                    body = await r.json(content_type=None)
                except ValueError:
                    body = {}
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise DonatixError("Donatix сейчас не отвечает, попробуйте через минуту.") from exc
    if not isinstance(body, dict) or r.status >= 400 or body.get("ok") is False:
        raise DonatixError(str((body or {}).get("error") or f"Ошибка {r.status}"))
    return body


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return kb.as_markup()


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Нажатие кнопки правит тот же экран; новое сообщение — только если править нечего."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return  # экран и так такой — второе сообщение не шлём
            if "no text" not in str(exc) and "can't be edited" not in str(exc):
                raise
        target = target.message  # у экрана с фото нет текста — пришлём новый
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


STATUS = {"pending": "⏳ проверяется", "paid": "✅ зачислено", "rejected": "❌ отклонено", "cancelled": "отменено"}


# ── Главный экран ────────────────────────────────────────────


@router.message(Command("donatix"))
@router.callback_query(F.data == "dx:home")
async def home(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not enabled():
        await _show(event, "Счёт Donatix не подключён.")
        return
    try:
        bal = await _call("GET", "/api/v2/balance")
        pays = await _call("GET", "/api/v1/payments")
    except DonatixError as exc:
        await _show(event, f"🏦 <b>Счёт Donatix</b>\n\n{esc(str(exc))}", _kb([("🔄 Обновить", "dx:home")]))
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    lines = [f"🏦 <b>Счёт Donatix</b>\n\nБаланс: <b>${esc(str(bal.get('balance')))}</b>",
             "С него оплачиваются заказы ваших клиентов. Когда баланс кончится, заказы остановятся."]
    recent = pays.get("items", [])[:5]
    if recent:
        lines.append("\n<b>Последние пополнения</b>")
        for p in recent:
            lines.append(f"#{p['id']} · {esc(p['pay_amount'])} {esc(p['pay_currency'])} · {STATUS.get(p['status'], p['status'])}")
    await _show(event, "\n".join(lines), _kb([("💳 Пополнить счёт", "dx:methods")], [("🔄 Обновить", "dx:home")],
                                              [("‹ В панель", "pn:home")]))
    if isinstance(event, CallbackQuery):
        await event.answer()


# ── Пополнение ───────────────────────────────────────────────


def _method_label(m: dict) -> str:
    net = m.get("network") or ""
    return f"{m['title']} · {m['currency']}" + (f" · {net}" if net and net not in m["title"] else "")


@router.callback_query(F.data == "dx:methods")
async def methods(call: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await _call("GET", "/api/v1/payments/methods")
    except DonatixError as exc:
        await call.answer(str(exc)[:190], show_alert=True)
        return
    ms = data.get("methods") or []
    if not ms:
        await _show(call, "Способы оплаты ещё не настроены. Напишите администратору Donatix.",
                    _kb([("‹ Назад", "dx:home")]))
        await call.answer()
        return
    await state.update_data(dx_min=data.get("min_tjs"), dx_rate=data.get("tjs_rate"))
    rows = [[(_method_label(m), f"dx:m:{m['code']}")] for m in ms]
    rows.append([("‹ Назад", "dx:home")])
    await _show(call, f"💳 <b>Пополнение счёта Donatix</b>\n\nВыберите способ оплаты.\n"
                      + (f"Минимум — <b>{esc(str(data.get('min_tjs')))} сомони</b>."
                         if float(data.get("min_tjs") or 0) > 0 else "Можно любую сумму."), _kb(*rows))
    await call.answer()


@router.callback_query(F.data.startswith("dx:m:"))
async def pick_method(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(dx_method=call.data[5:])
    await state.set_state(TopUp.amount)
    data = await state.get_data()
    minimum = float(data.get("dx_min") or 0)
    await _show(call, "Сколько сомони хотите пополнить?\nНапишите число"
                      + (f", минимум <b>{esc(str(data.get('dx_min')))}</b>." if minimum > 0 else " — любая сумма."),
                _kb([("‹ Отмена", "dx:home")]))
    await call.answer()


@router.message(TopUp.amount, F.text)
async def got_amount(message: Message, state: FSMContext) -> None:
    amount = message.text.replace(",", ".").replace(" ", "").strip()
    data = await state.get_data()
    try:
        res = await _call("POST", "/api/v1/payments", json={"method": data.get("dx_method", ""), "amount_tjs": amount})
    except DonatixError as exc:
        if "на проверке" in str(exc) or "ждёт оплаты" in str(exc):   # одна заявка за раз — ждём решения админа
            await state.set_state(None)
            await message.answer(f"⏳ {esc(str(exc))}", reply_markup=_kb([("🏦 Счёт Donatix", "dx:home")]))
            return
        await message.answer(f"⚠️ {esc(str(exc))}\n\nВведите сумму ещё раз.", reply_markup=_kb([("‹ Отмена", "dx:home")]))
        return
    p = res["payment"]
    await state.update_data(dx_payment=p["id"])
    await state.set_state(None)
    if p.get("auto"):
        await message.answer(_auto_text(p), reply_markup=_auto_kb(p), disable_web_page_preview=True)
        return
    await message.answer(
        f"🧾 <b>Заявка #{p['id']}</b> · {esc(p['method_title'])}\n\n"
        f"Переведите ровно <b>{esc(p['pay_amount'])} {esc(p['pay_currency'])}</b>\n"
        f"Будет зачислено: <b>${esc(p['amount_usd'])}</b>\n\n"
        + (f"Сеть: <b>{esc(p['network'])}</b>\n" if p.get("network") else "")
        + f"<b>Реквизиты:</b>\n<code>{esc(p['details'])}</code>\n\n"
        + (f"⚠️ {esc(p['network_note'])}\n\n" if p.get("network_note") else "")
        + "После перевода нажмите «Я оплатил» и пришлите чек.",
        reply_markup=_kb([("✅ Я оплатил", f"dx:paid:{p['id']}")], [("‹ Отмена", "dx:home")]),
    )


def _auto_text(p: dict) -> str:
    """Автоплатёж (USDT TRC20 по блокчейну или Binance Pay): чек не нужен, зачислится само."""
    head = (f"⚡️ <b>Заявка #{p['id']}</b> · {esc(p['method_title'])}\n\n"
            f"К оплате: <b>{esc(p['pay_amount'])} {esc(p['pay_currency'])}</b>\n"
            f"Будет зачислено: <b>${esc(p['amount_usd'])}</b>\n\n")
    if p["auto"] == "trc20":
        head += (f"<b>Адрес USDT · сеть TRC20:</b>\n<code>{esc(p.get('address', ''))}</code>\n\n"
                 f"⚠️ Переведите <b>ровно {esc(p['pay_amount'])} USDT</b> — по последним цифрам суммы "
                 "мы узнаём ваш перевод. Другая сумма или сеть сама не зачислится.\n\n")
    elif p["auto"] == "bybit":
        head += (f"<b>Bybit UID получателя:</b> <code>{esc(p.get('address', ''))}</code>\n\n"
                 f"В Bybit: Активы → Перевод → <b>По UID</b>, USDT, ровно <b>{esc(p['pay_amount'])}</b> — "
                 "по последним цифрам суммы мы узнаём ваш перевод.\n\n")
    return head + "Баланс пополнится сам за 1–3 минуты — чек присылать не нужно."


def _auto_kb(p: dict):
    kb = InlineKeyboardBuilder()
    if p.get("pay_url"):
        kb.row(InlineKeyboardButton(text="💳 Оплатить в Binance", url=p["pay_url"]))
    kb.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"dx:chk:{p['id']}"))
    kb.row(InlineKeyboardButton(text="‹ Счёт Donatix", callback_data="dx:home"))
    return kb.as_markup()


@router.callback_query(F.data.startswith("dx:chk:"))
async def check_auto(call: CallbackQuery) -> None:
    pid = call.data.split(":")[2]
    try:
        p = (await _call("GET", f"/api/v1/payments/{pid}"))["payment"]
    except DonatixError as exc:
        await call.answer(str(exc)[:190], show_alert=True)
        return
    if p["status"] == "paid":
        await call.answer(f"✅ Оплата получена — зачислено ${p['amount_usd']}", show_alert=True)
        await _show(call, f"✅ <b>Заявка #{p['id']}</b> оплачена — баланс пополнен на <b>${esc(p['amount_usd'])}</b>.",
                    _kb([("🏦 Счёт Donatix", "dx:home")]))
        return
    if p["status"] != "pending":
        await call.answer("Заявка закрыта: " + (p.get("note") or p["status"]), show_alert=True)
        return
    await call.answer("⏳ Пока не пришло. Проверим ещё раз через минуту — или нажмите снова.", show_alert=True)


@router.callback_query(F.data.startswith("dx:paid:"))
async def paid(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(dx_payment=int(call.data.split(":")[2]))
    await state.set_state(TopUp.receipt)
    await _show(call, "📎 Пришлите чек — фото или PDF-файл перевода.", _kb([("‹ Отмена", "dx:home")]))
    await call.answer()


@router.message(TopUp.receipt, F.photo | F.document)
async def got_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    pid = data.get("dx_payment")
    if message.photo:
        file, name, ctype = message.photo[-1], "receipt.jpg", "image/jpeg"
    else:
        doc = message.document
        ctype = doc.mime_type or ""
        if ctype not in ("image/jpeg", "image/png", "image/webp", "application/pdf"):
            await message.answer("Нужен чек фото (JPG/PNG) или PDF.")
            return
        file, name = doc, doc.file_name or "receipt"
    if (file.file_size or 0) > MAX_RECEIPT:
        await message.answer("Файл больше 10 МБ — пришлите фото чека.")
        return
    buf = await bot.download(file)
    form = aiohttp.FormData()
    form.add_field("file", buf.read(), filename=name, content_type=ctype)
    try:
        await _call("POST", f"/api/v1/payments/{pid}/receipt", data=form)
    except DonatixError as exc:
        await message.answer(f"⚠️ Чек не отправлен: {esc(str(exc))}")
        return
    await state.clear()
    await message.answer("✅ Чек отправлен администратору Donatix. Как только оплату подтвердят, "
                         "баланс пополнится — проверить можно в /donatix.",
                         reply_markup=_kb([("🏦 Счёт Donatix", "dx:home")]))


@router.message(TopUp.receipt)
async def receipt_wrong(message: Message) -> None:
    await message.answer("Пришлите чек фото или файлом PDF. Отменить — /donatix.")


RATE_BACKGROUND = 300  # фоном — раз в 5 минут
RATE_PAYMENT = 30      # когда покупатель платит или смотрит цены — не старше 30 секунд
_rate_at = 0.0


async def sync_rate(conn, max_age: float = RATE_BACKGROUND) -> bool:
    """Курс доллара — тот же, что у Donatix: по нему владелец платит за товары.

    Свой поиск курса у бота при этом выключается, иначе два курса спорили бы
    и цены в сомони уходили бы в минус. Donatix не ответил — остаётся прежний.
    """
    import time
    from datetime import datetime, timezone

    from app import runtime
    global _rate_at
    if not enabled() or time.monotonic() - _rate_at < max_age:
        return False
    _rate_at = time.monotonic()
    try:
        rate = (await _call("GET", "/api/v1/payments/methods")).get("tjs_rate")
        diram = round(float(rate) * 100)
    except (DonatixError, TypeError, ValueError) as exc:
        log.info("Donatix: курс не обновлён: %s", exc)
        return False
    if diram <= 0:
        return False
    if runtime.get_bool("usd_auto"):
        await runtime.set_value(conn, "usd_auto", "0")
    if diram == runtime.usd_rate():
        return False
    await runtime.set_value(conn, "usd_rate_diram", str(diram))
    await runtime.set_value(conn, "usd_rate_source", "Donatix")
    await runtime.set_value(conn, "usd_rate_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log.info("Donatix: курс %s сомони", rate)
    return True


async def rate_loop(conn, provider=None) -> None:
    """Раз в 5 минут: курс и цены. Поменял админ Donatix наценку или курс —
    цены в боте догонят сами, наценка владельца бота сохраняется."""
    import asyncio

    from app.services import pricing
    while True:
        await asyncio.sleep(RATE_BACKGROUND)
        try:
            await sync_rate(conn, RATE_BACKGROUND - 5)
            if provider is not None:
                await pricing.apply_margin_now(conn, provider)
        except Exception:  # noqa: BLE001 — фон не должен падать
            log.exception("Donatix: курс и цены")


async def bootstrap(conn, provider) -> None:
    """Первый запуск бота из конструктора: курс доллара и все игры — сами."""
    import asyncio

    from app.services import autogames, suppliers
    from app.services import games as gsvc

    await asyncio.sleep(3)
    try:
        await sync_rate(conn, 0)
        from app import db, runtime
        if runtime.margin_percent() <= 0 and not await db.list_games(conn):
            # Первый запуск: без наценки бот продавал бы по закупке — ставим 10%, владелец поменяет
            await runtime.set_value(conn, "margin_percent", "10")
        if not await db.list_games(conn):
            # Сразу готово: Free Fire (СНГ и Индонезия) и PUBG. Остальное владелец добавит сам
            catalog = await gsvc.full_catalog(suppliers.for_games(provider))
            added, enabled = await autogames.import_starter(conn, catalog)
            log.info("Donatix: стартовые игры — добавлено %s, включено %s", added, enabled)
            await runtime.set_value(conn, "starter_games_v1", "1")
        elif not runtime.get("starter_games_v1"):
            # Бот создан раньше и получил все игры — оставляем в меню стартовые, остальные скрываем
            hidden = await autogames.keep_only_starter(conn)
            await runtime.set_value(conn, "starter_games_v1", "1")
            log.info("Donatix: в меню оставлены Free Fire и PUBG, скрыто игр: %s", hidden)
    except Exception:  # noqa: BLE001 — бот работает и без этого, владелец добавит вручную
        log.exception("Donatix: первичная настройка не удалась")
    await rate_loop(conn, provider)
