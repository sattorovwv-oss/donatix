"""Кабинет разработчика в боте: ключи, баланс, вебхук, статистика.

Ключи выдаются здесь, а не на сайте, и это не упрощение: клиент бота уже
опознан Telegram, у него уже есть баланс и история. Отдельный вход с
логином и паролем означал бы второй список людей и второй баланс,
который разошёлся бы с первым в первый же день.

Смотреть же историю удобнее в браузере — таблицами, за выбранный период.
Поэтому длинные списки живут на странице кабинета (app/api/cabinet.py),
а вход туда — по тому же ключу, без всякой регистрации. Здесь остаётся
короткая сводка и ссылка.
"""
from __future__ import annotations

import logging

import aiosqlite
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import db, runtime, texts
from app.api import guard, hooks
from app.api import keys as apikeys
from app.api import server as api_server
from app.keyboards import DANGER, PRIMARY, SUCCESS, btn, labeled
from app.money import fmt
from app.states import ApiCab

log = logging.getLogger(__name__)
router = Router(name="api_cab")

#: Больше этого ключей одному человеку не нужно, а список перестаёт
#: помещаться на экран.
MAX_KEYS = 10

LINE = texts.LINE


#: Готовая ссылка на кабинет — по человеку. Сам пропуск в базе лежит
#: только хешем, восстановить его оттуда нельзя, поэтому держим строку
#: в памяти процесса: иначе каждый заход на экран выдавал бы новый
#: пропуск и ронял ссылку, открытую на другом устройстве.
_links: dict[int, str] = {}


async def cabinet_link(conn, user_id: int) -> str:
    """Ссылка, которая открывает кабинет без ввода ключа.

    Пропуск уезжает в решётке адреса (после #): такую часть браузер не
    отправляет на сервер и не кладёт в Referer, поэтому она не попадёт
    ни в наш журнал, ни в чужой.
    """
    url = api_server.base_url()
    if not url:
        return ""
    ready = _links.get(user_id)
    if ready:
        return ready

    token = apikeys.generate_pass()
    await db.add_api_pass(conn, user_id=user_id,
                          prefix=apikeys.pass_prefix_of(token),
                          token_hash=apikeys.hash_key(token))
    _links[user_id] = f"{url}/cabinet#t={token}"
    return _links[user_id]


def forget_link(user_id: int) -> None:
    _links.pop(user_id, None)


def _home_kb(has_keys: bool, cabinet: str = "") -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if cabinet:
        # Кабинет первым: там история за периоды, выписка и каталог — всё
        # то, что в переписке Telegram читать неудобно. Ключ для входа не
        # нужен, ссылка узнаёт человека сама.
        kb.row(InlineKeyboardButton(text="🖥 Открыть кабинет", url=cabinet))
    kb.row(btn("🔑 Мои ключи", "api:keys", style=PRIMARY))
    if has_keys:
        kb.row(btn("📦 Заказы", "api:orders"), btn("💳 Транзакции", "api:txs"))
        kb.row(btn("🔔 Вебхук", "api:hook"), btn("📊 Статистика", "api:stats"))
        kb.row(btn("📣 Уведомления", "api:notify"))
    url = api_server.base_url()
    if url:
        kb.row(InlineKeyboardButton(text="📖 Документация", url=f"{url}/docs"))
    if cabinet:
        kb.row(btn("🔒 Закрыть доступ к кабинету", "api:pass_off"))
    kb.row(btn(labeled("back", "В меню"), "m:main"))
    return kb


async def _home_text(conn, user_id: int) -> str:
    user = await db.get_user(conn, user_id)
    keys = await db.api_keys_of(conn, user_id)
    live = sum(1 for key in keys if key.live)
    stats = await db.api_stats(conn, user_id)
    url = api_server.base_url() or "—"

    return (
        f"🧩 <b>API для разработчиков</b>\n"
        f"<code>{LINE}</code>\n\n"
        "Подключите наши товары к своему сайту, боту или приложению: "
        "каталог, покупка и статус заказа — обычными HTTP-запросами.\n\n"
        f"💰 Баланс: <b>{fmt(user.balance if user else 0)}</b>\n"
        f"🔑 Ключей: <b>{live}</b> из {MAX_KEYS}\n"
        f"📊 Запросов: <b>{stats['total']}</b> "
        f"<i>(ошибок {stats['errors']})</i>\n\n"
        f"<blockquote>Адрес API:\n<code>{url}</code>\n\n"
        "Покупки списываются с того же баланса, что и заказы в боте — "
        "пополняется он там же.</blockquote>"
    )


async def show_home(target, conn, edit: bool = True) -> None:
    user_id = target.from_user.id
    keys = await db.api_keys_of(conn, user_id)
    text = await _home_text(conn, user_id)
    markup = _home_kb(bool(keys), await cabinet_link(conn, user_id)).as_markup()
    message = target.message if isinstance(target, CallbackQuery) else target
    if edit and isinstance(target, CallbackQuery):
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("api"))
async def cmd_api(message: Message, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_home(message, conn, edit=False)


@router.callback_query(F.data == "api:home")
async def cb_home(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    await show_home(call, conn)
    await call.answer()


MODE_TITLES = {
    "off": "🔕 Ничего не присылать",
    "problems": "⚠️ Только проблемы",
    "all": "📬 Все заказы",
}


def _notify_kb(prefs: dict) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for mode, title in MODE_TITLES.items():
        mark = "✅ " if prefs["notify"] == mode else ""
        kb.row(btn(mark + title, f"api:notify_set:{mode}",
                   style=SUCCESS if prefs["notify"] == mode else None))
    kb.row(btn(("✅ " if prefs["digest"] else "⬜️ ") + "Сводка раз в сутки",
               "api:digest"))
    kb.row(btn(labeled("back", "Назад"), "api:home"))
    return kb


async def _show_notify(call: CallbackQuery, conn) -> None:
    prefs = await db.api_prefs(conn, call.from_user.id)
    await call.message.edit_text(
        f"📣 <b>Уведомления</b>\n<code>{LINE}</code>\n\n"
        "Это сообщения <b>вам в Telegram</b>. Вашей программе о заказах "
        "сообщает вебхук — он работает отдельно и всегда.\n\n"
        f"Сейчас: <b>{MODE_TITLES[prefs['notify']]}</b>\n"
        f"Сводка за сутки: <b>{'да' if prefs['digest'] else 'нет'}</b>\n\n"
        "<blockquote>«Только проблемы» — возврат и зависший заказ. "
        "Это то, из-за чего теряются деньги.\n\n«Все заказы» на сотне "
        "покупок в день — сотня сообщений: их перестают читать вместе "
        "с важными.</blockquote>",
        reply_markup=_notify_kb(prefs).as_markup(),
    )


@router.callback_query(F.data == "api:notify")
async def cb_notify(call: CallbackQuery, state: FSMContext,
                    conn: aiosqlite.Connection) -> None:
    await state.clear()
    await _show_notify(call, conn)
    await call.answer()


@router.callback_query(F.data.startswith("api:notify_set:"))
async def cb_notify_set(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    mode = call.data.rsplit(":", 1)[1]
    if mode not in db.NOTIFY_MODES:
        await call.answer("Неизвестный режим.", show_alert=True)
        return
    await db.set_api_prefs(conn, call.from_user.id, notify=mode)
    await _show_notify(call, conn)
    await call.answer(MODE_TITLES[mode])


@router.callback_query(F.data == "api:digest")
async def cb_digest(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    prefs = await db.api_prefs(conn, call.from_user.id)
    await db.set_api_prefs(conn, call.from_user.id,
                           digest=0 if prefs["digest"] else 1)
    await _show_notify(call, conn)
    await call.answer("Сводка выключена" if prefs["digest"]
                      else "Сводка будет приходить утром")


@router.callback_query(F.data == "api:pass_off")
async def cb_pass_off(call: CallbackQuery, state: FSMContext,
                      conn: aiosqlite.Connection) -> None:
    """Закрыть кабинет на всех устройствах и выдать новую ссылку."""
    gone = await db.drop_api_passes(conn, call.from_user.id)
    apikeys.forget_passes()
    forget_link(call.from_user.id)
    await state.clear()
    await show_home(call, conn)
    await call.answer(
        f"Старые ссылки больше не работают ({gone}). Новая — в кнопке выше.",
        show_alert=True,
    )


# ───────────────────────────────────────────────────────── ключи


def _key_line(key: db.ApiKey) -> str:
    mark = "🟢" if key.live else "⚪️"
    when = (key.last_used_at or "")[:16].replace("T", " ")
    return (
        f"{mark} <code>{key.masked}</code>"
        + (f" — {key.label}" if key.label else "")
        + f"\n   <i>создан {key.created_at[:10]}, "
        + (f"последний запрос {when}" if when else "ещё не использован")
        + f", всего {key.requests}</i>"
    )


@router.callback_query(F.data == "api:keys")
async def cb_keys(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    await state.clear()
    keys = await db.api_keys_of(conn, call.from_user.id)

    kb = InlineKeyboardBuilder()
    if len(keys) < MAX_KEYS:
        kb.row(btn("➕ Создать ключ", "api:key_new", style=SUCCESS))
    for key in keys:
        kb.row(btn(
            ("🟢 " if key.live else "⚪️ ") + (key.label or key.masked),
            f"api:key:{key.id}",
        ))
    kb.row(btn(labeled("back", "Назад"), "api:home"))

    body = "\n\n".join(_key_line(key) for key in keys) if keys else (
        "<i>Ключей пока нет.</i>"
    )
    await call.message.edit_text(
        f"🔑 <b>Ключи API</b>\n<code>{LINE}</code>\n\n{body}\n\n"
        "<blockquote>Ключ показывается <b>один раз</b> — в момент "
        "создания. Мы храним только его отпечаток и восстановить ключ "
        "не можем: потеряли — создайте новый, старый отзовите."
        "</blockquote>",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "api:key_new")
async def cb_key_new(call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection) -> None:
    if len(await db.api_keys_of(conn, call.from_user.id)) >= MAX_KEYS:
        await call.answer(f"Больше {MAX_KEYS} ключей нельзя.", show_alert=True)
        return
    await state.set_state(ApiCab.key_label)
    await call.message.edit_text(
        f"🔑 <b>Новый ключ</b>\n<code>{LINE}</code>\n\n"
        "Как его назвать? Название видите только вы — оно нужно, чтобы "
        "потом понять, какой ключ где стоит.\n\n"
        "<blockquote>Например: <code>Мой сайт</code>, "
        "<code>Телеграм-бот</code>, <code>Тесты</code>.\n\n"
        "Или пришлите <code>-</code>, чтобы обойтись без названия."
        "</blockquote>",
        reply_markup=_cancel_kb("api:keys"),
    )
    await call.answer()


def _cancel_kb(target: str):
    kb = InlineKeyboardBuilder()
    kb.row(btn("❌ Отмена", target, style=DANGER))
    return kb.as_markup()


@router.message(ApiCab.key_label, F.text)
async def on_key_label(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    await state.clear()
    label = "" if message.text.strip() == "-" else message.text.strip()[:64]

    if len(await db.api_keys_of(conn, message.from_user.id)) >= MAX_KEYS:
        await message.answer(f"❌ Больше {MAX_KEYS} ключей нельзя.")
        return

    raw = apikeys.generate()
    await db.add_api_key(
        conn, user_id=message.from_user.id, label=label,
        prefix=apikeys.prefix_of(raw), tail=apikeys.tail_of(raw),
        key_hash=apikeys.hash_key(raw),
    )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Скопировать ключ",
                                copy_text=CopyTextButton(text=raw)))
    kb.row(btn("🔑 К ключам", "api:keys", style=PRIMARY))

    # Ключ уходит одним сообщением и больше нигде не появляется: ни в
    # журнале, ни в базе, ни в следующем экране.
    await message.answer(
        f"✅ <b>Ключ создан</b>"
        + (f" — {label}" if label else "")
        + f"\n<code>{LINE}</code>\n\n"
        f"<code>{raw}</code>\n\n"
        "<blockquote>⚠️ Этот ключ показывается <b>один раз</b>. "
        "Скопируйте его сейчас — мы храним только отпечаток и показать "
        "ключ заново не сможем.\n\n"
        "Держите его на своём сервере в переменной окружения. Не "
        "кладите в git и не используйте в коде страницы: запрос из "
        "браузера отдаёт ключ каждому посетителю.</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("api:key:"))
async def cb_key_card(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    key = await _own_key(call, conn)
    if key is None:
        return

    kb = InlineKeyboardBuilder()
    if key.enabled:
        kb.row(btn("⏸ Выключить", f"api:key_off:{key.id}"))
    else:
        kb.row(btn("▶️ Включить", f"api:key_on:{key.id}", style=SUCCESS))
    kb.row(btn("🗑 Отозвать навсегда", f"api:key_kill:{key.id}", style=DANGER))
    kb.row(btn(labeled("back", "К ключам"), "api:keys"))

    await call.message.edit_text(
        f"🔑 <b>{key.label or 'Ключ'}</b>\n<code>{LINE}</code>\n\n"
        f"{_key_line(key)}\n\n"
        + ("<blockquote>Ключ работает.</blockquote>" if key.live else
           "<blockquote>⏸ Ключ выключен — запросы с ним не проходят."
           "</blockquote>"),
        reply_markup=kb.as_markup(),
    )
    await call.answer()


async def _own_key(call: CallbackQuery, conn) -> db.ApiKey | None:
    """Ключ, если он и правда принадлежит нажавшему.

    Проверка обязательна: номер ключа виден в callback_data, и подменить
    его в своём клиенте Telegram может кто угодно.
    """
    raw = call.data.rsplit(":", 1)[1]
    key = await db.get_api_key(conn, int(raw)) if raw.isdigit() else None
    if key is None or key.user_id != call.from_user.id:
        await call.answer("Ключ не найден.", show_alert=True)
        return None
    return key


@router.callback_query(F.data.startswith(("api:key_on:", "api:key_off:")))
async def cb_key_toggle(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    key = await _own_key(call, conn)
    if key is None:
        return
    on = call.data.startswith("api:key_on:")
    await db.set_api_key_enabled(conn, key.id, on)
    # Забываем кеш сразу: выключают ключ как раз тогда, когда он утёк.
    apikeys.forget(key.id)
    guard.forget_rate(key.id)
    await call.answer("Включён" if on else "Выключен")
    call.data = f"api:key:{key.id}"
    await cb_key_card(call, conn)


@router.callback_query(F.data.startswith("api:key_kill:"))
async def cb_key_revoke(call: CallbackQuery, state: FSMContext,
                        conn: aiosqlite.Connection) -> None:
    key = await _own_key(call, conn)
    if key is None:
        return
    await db.revoke_api_key(conn, key.id)
    apikeys.forget(key.id)
    guard.forget_rate(key.id)
    await call.answer("Ключ отозван")
    await cb_keys(call, state, conn)


# ───────────────────────────────────────────────────────── заказы и деньги


@router.callback_query(F.data == "api:orders")
async def cb_orders(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    rows = await db.api_orders_of(conn, call.from_user.id, limit=10)
    if not rows:
        body = "<i>Заказов по API ещё не было.</i>"
    else:
        body = "\n".join(
            f"{db.ORDER_TITLES.get(row['status'], row['status'])[:1]} "
            f"<code>{row['ref']}</code> — {row['product_id']} · "
            f"<b>{fmt(row['price'])}</b> · "
            f"<i>{hooks.PUBLIC.get(row['status'], row['status'])}</i>"
            for row in rows
        )
    await call.message.edit_text(
        f"📦 <b>Заказы по API</b>\n<code>{LINE}</code>\n\n{body}\n\n"
        "<blockquote>Полный список — запросом <code>GET /orders</code>."
        "</blockquote>",
        reply_markup=_back_kb(),
    )
    await call.answer()


KIND_TITLES = {
    "deposit": "➕ пополнение", "charge": "➖ покупка",
    "refund": "↩️ возврат", "adjust": "✍️ правка",
}


@router.callback_query(F.data == "api:txs")
async def cb_txs(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    rows = await db.api_txs_of(conn, call.from_user.id, limit=12)
    if not rows:
        body = "<i>Движения денег по API ещё не было.</i>"
    else:
        body = "\n".join(
            f"<code>{row['tx_id']}</code> {KIND_TITLES.get(row['kind'], row['kind'])}"
            f" <b>{fmt(row['amount'])}</b>\n"
            f"   <i>{row['created_at'][:16].replace('T', ' ')} · "
            f"{fmt(row['balance_before'])} → {fmt(row['balance_after'])}"
            + (f" · {row['order_ref']}" if row["order_ref"] else "")
            + "</i>"
            for row in rows
        )
    await call.message.edit_text(
        f"💳 <b>Транзакции</b>\n<code>{LINE}</code>\n\n{body}\n\n"
        "<blockquote>Баланс до и после каждой операции — чтобы вы могли "
        "сверить его со своей стороной до копейки.</blockquote>",
        reply_markup=_back_kb(),
    )
    await call.answer()


def _back_kb():
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("back", "Назад"), "api:home"))
    return kb.as_markup()


# ───────────────────────────────────────────────────────── вебхук


@router.callback_query(F.data == "api:hook")
async def cb_hook(call: CallbackQuery, state: FSMContext,
                  conn: aiosqlite.Connection) -> None:
    await state.clear()
    hook = await db.get_api_hook(conn, call.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.row(btn("🔗 Задать адрес", "api:hook_set", style=PRIMARY))
    if hook and hook["secret"]:
        kb.row(InlineKeyboardButton(
            text="📋 Скопировать секрет",
            copy_text=CopyTextButton(text=hook["secret"])))
    if hook and hook["url"]:
        kb.row(btn("🚫 Отключить", "api:hook_off", style=DANGER))
    kb.row(btn(labeled("back", "Назад"), "api:home"))

    if not hook or not hook["url"]:
        body = ("<i>Адрес не задан.</i>\n\nБез него о смене статуса вы "
                "узнаёте только запросом <code>GET /order/status</code>.")
    else:
        state_mark = "🟢 работает" if hook["enabled"] else "🚫 отключён"
        body = (
            f"Адрес: <code>{hook['url']}</code>\n"
            f"Состояние: <b>{state_mark}</b>\n"
            + (f"Последняя доставка: <i>{hook['last_ok_at'][:16]}</i>\n"
               if hook["last_ok_at"] else "")
            + (f"⚠️ Ошибок подряд: <b>{hook['fails']}</b> — "
               f"<i>{hook['last_error']}</i>\n" if hook["fails"] else "")
        )

    await call.message.edit_text(
        f"🔔 <b>Вебхук</b>\n<code>{LINE}</code>\n\n{body}\n\n"
        "<blockquote>Мы шлём <code>POST</code> на ваш адрес, как только "
        "заказ меняет статус. Тело подписано HMAC-SHA256 на вашем "
        "секрете — заголовок <code>X-Signature</code>.\n\n"
        "<b>Обязательно проверяйте подпись.</b> Без неё любой, кто узнает "
        "ваш адрес, сможет прислать «заказ выполнен».</blockquote>",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "api:hook_set")
async def cb_hook_set(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ApiCab.hook_url)
    await call.message.edit_text(
        f"🔗 <b>Адрес вебхука</b>\n<code>{LINE}</code>\n\n"
        "Пришлите адрес, куда слать события.\n\n"
        "<blockquote>Например: <code>https://ваш-сайт.tj/hooks/shop</code>\n\n"
        "Только <code>https://</code>, порт 443 или 8443. Внутренние "
        "адреса (<code>127.0.0.1</code>, <code>10.x</code>, "
        "<code>localhost</code>) не принимаются: наш сервер не должен "
        "ходить по ним за кого-то.</blockquote>",
        reply_markup=_cancel_kb("api:hook"),
    )
    await call.answer()


@router.message(ApiCab.hook_url, F.text)
async def on_hook_url(
    message: Message, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    url = message.text.strip()
    problem = hooks.check_url(url)
    if problem:
        await message.answer(
            f"❌ <b>Адрес не принят</b> — {problem}\n\n"
            "<blockquote>Нужен внешний адрес по https, например\n"
            "<code>https://ваш-сайт.tj/hooks/shop</code></blockquote>"
        )
        return

    await state.clear()
    old = await db.get_api_hook(conn, message.from_user.id)
    secret = (old or {}).get("secret") or hooks.new_secret()
    await db.set_api_hook(conn, message.from_user.id, url, secret)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Скопировать секрет",
                                copy_text=CopyTextButton(text=secret)))
    kb.row(btn("🔔 К вебхуку", "api:hook", style=PRIMARY))
    await message.answer(
        f"✅ <b>Адрес сохранён</b>\n<code>{LINE}</code>\n\n"
        f"<code>{url}</code>\n\n"
        f"Секрет подписи:\n<code>{secret}</code>\n\n"
        "<blockquote>Проверяйте подпись на своей стороне: HMAC-SHA256 "
        "от сырого тела запроса на этом секрете, заголовок "
        "<code>X-Signature</code>. Пример кода — в документации."
        "</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "api:hook_off")
async def cb_hook_off(call: CallbackQuery, state: FSMContext,
                      conn: aiosqlite.Connection) -> None:
    await db.set_api_hook(conn, call.from_user.id, "", "")
    await call.answer("Вебхук отключён")
    await cb_hook(call, state, conn)


# ───────────────────────────────────────────────────────── статистика


@router.callback_query(F.data == "api:stats")
async def cb_stats(call: CallbackQuery, conn: aiosqlite.Connection) -> None:
    user_id = call.from_user.id
    stats = await db.api_stats(conn, user_id)
    recent = await db.api_requests_of(conn, user_id, limit=8)
    orders = await db.api_orders_of(conn, user_id, limit=500)

    done = [row for row in orders if row["status"] == db.ORDER_DELIVERED]
    sold = sum(row["price"] for row in done)

    last = "\n".join(
        f"<code>{row['created_at'][11:19]}</code> "
        f"{row['method']} {row['path'].replace(api_server.PREFIX, '')} — "
        f"<b>{row['status']}</b> <i>{row['ms']} мс</i>"
        for row in recent
    ) or "<i>запросов ещё не было</i>"

    share = round(stats["ok"] * 100 / stats["total"]) if stats["total"] else 0
    await call.message.edit_text(
        f"📊 <b>Статистика API</b>\n<code>{LINE}</code>\n\n"
        f"🔁 <b>Запросы</b>\n"
        f"├ Всего: <b>{stats['total']}</b>\n"
        f"├ Успешных: <b>{stats['ok']}</b> <i>({share}%)</i>\n"
        f"└ С ошибкой: <b>{stats['errors']}</b>\n\n"
        f"📦 <b>Заказы</b>\n"
        f"├ Всего: <b>{len(orders)}</b>\n"
        f"├ Выполнено: <b>{len(done)}</b>\n"
        f"└ На сумму: <b>{fmt(sold)}</b>\n\n"
        f"🕒 <b>Последние запросы</b>\n{last}\n\n"
        f"<blockquote>Лимит — {runtime.get_int('api_rate_per_min', 60)} "
        "запросов в минуту на ключ.</blockquote>",
        reply_markup=_back_kb(),
    )
    await call.answer()
