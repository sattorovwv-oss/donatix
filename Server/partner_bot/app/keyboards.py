"""Клавиатуры бота.

Цвет кнопок доступен с Bot API 9.4: поле style принимает primary (синий),
success (зелёный) и danger (красный). Цвет здесь не украшение, а подсказка:
зелёным помечено подтверждение и приход денег, красным — отмена и всё, что
что-то ломает, синим — главное действие экрана. Навигация остаётся без
цвета, иначе выделенным окажется всё сразу и цвет перестанет что-то значить.

Оттуда же icon_custom_emoji_id — премиум-эмодзи прямо на кнопке. Ставится
только если владелец задал его для этого значка и проверка прошла.
"""
from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import runtime
from app.config import settings
from app.emoji import custom_id, em, premium_on
from app.money import fmt, fmt_short, stars_cost, steam_cost
from app.services import regions

#: Значения поля style из Bot API 9.4
PRIMARY = "primary"    # синий — главное действие экрана
SUCCESS = "success"    # зелёный — подтвердить, оплатить, зачислить
DANGER = "danger"      # красный — отменить, отклонить, выключить


def btn(
    text: str,
    callback_data: str | None = None,
    *,
    url: str | None = None,
    style: str | None = None,
    icon: str | None = None,
) -> InlineKeyboardButton:
    """Кнопка с цветом и, если задан, премиум-значком.

    icon — ключ значка. Когда для него задан премиум-эмодзи, он ставится
    отдельным полем кнопки, а из текста обычный значок убирается: иначе
    рядом оказались бы два одинаковых.
    """
    fields: dict = {"text": text}
    if callback_data is not None:
        fields["callback_data"] = callback_data
    if url is not None:
        fields["url"] = url
    if style is not None:
        fields["style"] = style

    if icon:
        emoji_id = custom_id(icon)
        if emoji_id and premium_on():
            fields["icon_custom_emoji_id"] = emoji_id
            plain = em(icon)
            if plain and fields["text"].startswith(plain):
                fields["text"] = fields["text"][len(plain):].lstrip()
    return InlineKeyboardButton(**fields)


def game_btn(game, data: str, text: str = "") -> InlineKeyboardButton:
    """Кнопка игры со своим значком.

    На кнопке Telegram держит ровно один премиум-значок — поле для него
    одно. Поэтому два значка рядом бывают только в тексте сообщения,
    а здесь ставится тот, что относится к самой игре.

    Заданный владельцу ID важнее справочника: справочник знает только те
    игры, что мы продаём сегодня.
    """
    from app.emoji import game_key

    own = (getattr(game, "emoji", "") or "").strip()
    if own and premium_on():
        return InlineKeyboardButton(
            text=_without_plain(text or game.title),
            callback_data=data, style=PRIMARY, icon_custom_emoji_id=own,
        )
    return btn(text or game.title, data, style=PRIMARY,
               icon=game_key(game.category_id))


def _without_plain(text: str) -> str:
    """Убрать обычный значок из начала подписи: рядом с премиум-значком
    он оказался бы вторым таким же."""
    stripped = text.lstrip()
    while stripped and not (stripped[0].isalnum() or stripped[0] in "«\"("):
        stripped = stripped[1:].lstrip()
    return stripped or text


def labeled(icon: str, text: str) -> str:
    """Подпись со значком — значок берётся из настроек оформления."""
    return f"{em(icon)} {text}"


# ════════════════════════════════════════════════════════ главное меню


def reviews_link() -> str:
    """Ссылка на канал отзывов. Канал из панели важнее ссылки из .env —
    иначе владельцу пришлось бы задавать одно и то же дважды."""
    channel = (runtime.get("reviews_channel") or "").strip()
    if channel and not channel.lstrip("-").isdigit():
        return f"https://t.me/{channel.lstrip('@')}"
    return settings.reviews_url


def main_menu(games: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if runtime.get_bool("stars_enabled"):
        kb.row(btn(labeled("stars", "Ситора харидан"), "m:stars",
                   style=PRIMARY, icon="stars"))
    if runtime.get_bool("premium_enabled"):
        kb.row(btn(labeled("premium", "Telegram Premium"), "m:premium",
                   style=PRIMARY, icon="premium"))

    # Steam и игры живут за одной кнопкой: для покупателя это одно и то же
    # действие — пополнить игровой аккаунт.
    if games or runtime.steam_on():
        kb.row(games_entry_btn())

    deposit = btn(labeled("deposit", "Пур кардан"), "m:deposit",
                  style=SUCCESS, icon="deposit")
    profile = btn(labeled("profile", "Профил"), "m:profile", icon="profile")
    kb.row(deposit, profile) if runtime.get_bool("deposit_enabled") else kb.row(profile)

    kb.row(
        btn(labeled("support", "Дастгирӣ"), "m:support", icon="support"),
        btn(labeled("calc", "Ҳисобкунак"), "m:calc", icon="calc"),
    )
    kb.row(btn(labeled("info", "Маълумот"), "m:info", icon="info"))
    link = reviews_link()
    if link:
        kb.row(btn(labeled("reviews", "Шарҳҳо"), url=link, icon="reviews"))
    kb.row(btn(labeled("top", "Беҳтарин мизоҷон"), "m:top", icon="top"))
    return kb.as_markup()


#: Пустая на вид подпись. Текст кнопки Telegram требует непустым, а
#: показать мы хотим только значок — этот символ ничего не рисует.
BLANK = "\u3164"


def games_entry_btn() -> InlineKeyboardButton:
    """Вход в игры: значок и понятная подпись «Бозиҳо».

    Одни значки (🔥🎯) покупатели не понимали — кнопку пропускали.
    Премиум-значок у кнопки может быть только один (поле у Telegram одно),
    поэтому с ним — подпись без обычного эмодзи, без него — обычный эмодзи.
    """
    emoji_id = custom_id("game")
    if emoji_id and premium_on():
        return InlineKeyboardButton(
            text="Бозиҳо", callback_data="m:games",
            style=PRIMARY, icon_custom_emoji_id=emoji_id,
        )
    return btn(labeled("game", "Бозиҳо"), "m:games", style=PRIMARY)


def back(target: str = "m:main", text: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(text or labeled("back", "Ба меню"), target))
    return kb.as_markup()


def cancel(text: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(text or labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


# ═════════════════════════════════════════════════════════════ покупка


def stars_entry() -> InlineKeyboardMarkup:
    """Готовые наборы по два в ряд плюс ввод своего количества."""
    kb = InlineKeyboardBuilder()
    packs = runtime.star_packs()
    for left in range(0, len(packs), 2):
        kb.row(*[
            btn(f"{em('stars')} {quantity} — {fmt(stars_cost(quantity))}",
                f"stars:pack:{quantity}", style=PRIMARY)
            for quantity in packs[left:left + 2]
        ])
    kb.row(btn(labeled("edit", "Шумораи дигар"), "stars:buy"))
    kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
    return kb.as_markup()


def games_menu(games: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if runtime.steam_on():
        kb.row(btn(labeled("steam", "Steam"), "m:steam",
                   style=PRIMARY, icon="steam"))
    # Одна игра — одна кнопка, даже если у поставщика она разложена
    # по регионам: регион спрашиваем следующим шагом.
    for group in regions.group(games):
        items = group["games"]
        if len(items) == 1:
            kb.row(game_btn(items[0], f"g:{items[0].category_id}"))
        else:
            kb.row(game_btn(items[0], f"gf:{group['family']}", group["title"]))
    kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
    return kb.as_markup()


def game_regions(games: list) -> InlineKeyboardMarkup:
    """Регионы одной игры. Регион решает, на каком сервере искать игрока."""
    kb = InlineKeyboardBuilder()
    for game in games:
        kb.row(btn(regions.region_title(game), f"g:{game.category_id}",
                   style=PRIMARY))
    kb.row(btn(labeled("back", "Бозгашт"), "m:games"))
    return kb.as_markup()


#: Длиннее этого подпись в половину ширины не помещается — Telegram
#: обрежет её многоточием. Такие пакеты ставим на всю строку.
PAIR_LIMIT = 22


def game_packs(game, offers: list) -> InlineKeyboardMarkup:
    """Пакеты игры: короткие по две в ряд, длинные на всю строку.

    Полтора десятка пакетов по одному в строку вытягиваются на два
    экрана. Но и загонять в пару всё подряд нельзя: «Еженедельный
    ваучер — 17 с.» в половину ширины не влезает, и клиент видит
    обрезок вместо названия. Поэтому пара — только для коротких.
    """
    from app.emoji import game_key

    category_id = getattr(game, "category_id", game)
    emoji_id = ""
    if not isinstance(game, str):
        own = (getattr(game, "emoji", "") or "").strip()
        emoji_id = own or custom_id(game_key(category_id))

    labels = [f"{o['name']} — {fmt_short(o['price'])}" for o in offers]

    def make(index: int) -> InlineKeyboardButton:
        if emoji_id and premium_on():
            return InlineKeyboardButton(
                text=labels[index], callback_data=f"gp:{category_id}:{index}",
                style=PRIMARY, icon_custom_emoji_id=emoji_id,
            )
        return btn(labels[index], f"gp:{category_id}:{index}", style=PRIMARY)

    kb = InlineKeyboardBuilder()
    index = 0
    while index < len(labels):
        short = len(labels[index]) <= PAIR_LIMIT
        pairable = (short and index + 1 < len(labels)
                    and len(labels[index + 1]) <= PAIR_LIMIT)
        if pairable:
            kb.row(make(index), make(index + 1))
            index += 2
        else:
            kb.row(make(index))
            index += 1

    kb.row(btn(labeled("back", "Бозгашт"), "m:games"))
    return kb.as_markup()


def game_found_in(games: list) -> InlineKeyboardMarkup:
    """Регионы, в которых ID всё-таки нашёлся."""
    kb = InlineKeyboardBuilder()
    for game in games:
        kb.row(btn(f"✅ {regions.region_title(game)}",
                   f"g:{game.category_id}", style=SUCCESS))
    kb.row(btn(labeled("back", "Ба бозиҳо"), "m:games"))
    return kb.as_markup()


def confirm_unverified(game, family_size: int = 1) -> InlineKeyboardMarkup:
    """Купить без подтверждённого ID — под свою ответственность.

    Проверка ID работает не у всех регионов и не у всех игр. Запирать
    из-за неё покупку нельзя: пополнение идёт по ID, а не по проверке.
    """
    kb = InlineKeyboardBuilder()
    kb.row(btn("⚠️ Бо ҳамин ҳол харидан", "g:ok", style=DANGER))
    if family_size > 1:
        kb.row(btn("🌍 Минтақаро иваз кардан", f"gf:{regions.family_of(game)}"))
    kb.row(btn(labeled("edit", "ID-и дигар"), f"g:{game.category_id}"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main"))
    return kb.as_markup()


def confirm_game(category_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("confirm", "Ҳа, ин ҳисоби ман аст"), "g:ok", style=SUCCESS))
    kb.row(btn(labeled("edit", "ID-и дигар"), f"g:{category_id}"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def steam_menu() -> InlineKeyboardMarkup:
    """Готовые суммы пополнения Steam по две в ряд."""
    kb = InlineKeyboardBuilder()
    packs = runtime.steam_packs()
    currency = runtime.steam_currency()
    for left in range(0, len(packs), 2):
        kb.row(*[
            btn(f"{em('steam')} {amount} {currency} — {fmt(steam_cost(amount))}",
                f"steam:{amount}", style=PRIMARY)
            for amount in packs[left:left + 2]
        ])
    kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
    return kb.as_markup()


def confirm_steam() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("confirm", "Ҳа, ин ҳисоби ман аст"), "steam:ok", style=SUCCESS))
    kb.row(btn(labeled("edit", "Логини дигар"), "steam:again"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def premium_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in runtime.premium_plans():
        kb.row(btn(
            f"{em('premium')} {plan['months']} моҳ — {fmt(plan['price'])}",
            f"premium:{plan['months']}", style=PRIMARY, icon="premium",
        ))
    kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
    return kb.as_markup()


def ask_recipient(has_username: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_username:
        kb.row(btn(labeled("stars", "Ба худам"), "order:self", style=SUCCESS, icon="stars"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def confirm_recipient() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("confirm", "Ҳа, ҳама дуруст"), "order:recipient_ok", style=SUCCESS))
    kb.row(btn(labeled("edit", "Юзернейми дигар"), "order:again"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def confirm(has_promo: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("confirm", "Пардохт"), "order:go", style=SUCCESS))
    if has_promo:
        kb.row(btn("✖️ Промокодро гирифтан", "order:promo_off"))
    else:
        kb.row(btn(labeled("promo", "Промокод"), "order:promo"))
    kb.row(btn(labeled("edit", "Гирандаи дигар"), "order:again"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def cancel_order(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("cancel", "Фармоишро бекор кардан"), f"order:cancel:{order_id}",
               style=DANGER))
    return kb.as_markup()


def sponsor_gate(channels: list[str]) -> InlineKeyboardMarkup:
    """Куда подписаться и чем подтвердить.

    Канал без ссылки (задан числовым ID, а бот не смог узнать адрес)
    кнопкой не рисуем: кнопка без адреса — это кнопка, которая никуда не
    ведёт, и человек решит, что бот сломан.
    """
    from app.services import sponsor

    kb = InlineKeyboardBuilder()
    for channel in channels:
        link = sponsor.link_of(channel)
        if link:
            kb.row(btn(f"📢 Обуна шудан — {channel}", url=link))
    kb.row(btn(labeled("confirm", "Ман обуна шудам"), "sub:check", style=SUCCESS))
    return kb.as_markup()


# ══════════════════════════════════════════════════════════ пополнение


def deposit_methods() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    from app.handlers import donatix
    from app.services import paymethods
    if donatix.enabled():
        # Бот из конструктора: банки, которые владелец добавил в «Реквизитах»
        for m in paymethods.enabled():
            kb.row(btn(("💎 " if paymethods.crypto(m["bank"]) else "🏦 ") + m["bank"], f"dep:pm:{m['id']}",
                       style=SUCCESS))
        if runtime.get("ru_pay_number"):
            kb.row(btn("🇷🇺 Аз Русия — Сбербанк, Тинькофф", "dep:ru"))
        kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
        return kb.as_markup()
    kb.row(btn(labeled("dcity", "Душанбе Сити"), "dep:card",
               style=SUCCESS, icon="dcity"))
    kb.row(btn("🇷🇺 Аз Русия — Сбербанк, Тинькофф", "dep:ru"))
    kb.row(btn(labeled("back", "Бозгашт"), "m:main"))
    return kb.as_markup()


# ════════════════════════════════════════════════════════════ профиль


def deposit_pay(link: str = "", card: str = "") -> InlineKeyboardMarkup:
    """Реквизиты одним экраном: оплатить, скопировать карту, «я оплатил».

    Просить чек на этом же экране бесполезно — клиент ещё не платил, и
    длинный текст он дочитывает до кнопок, а не до просьбы. Поэтому про
    чек говорим отдельным шагом, когда он уже перевёл.
    """
    kb = InlineKeyboardBuilder()
    if link:
        kb.row(btn("🏙 Дар Душанбе Сити пардохт", url=link, style=SUCCESS))
    if card:
        kb.row(InlineKeyboardButton(
            text="📋 Нусхаи рақами корт",
            copy_text=CopyTextButton(text=card),
        ))
    kb.row(btn("✅ Ман пардохт кардам", "dep:paid", style=PRIMARY))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def deposit_ru(number: str = "") -> InlineKeyboardMarkup:
    """Реквизиты для перевода из России: номер получателя и «я отправил».

    Сумму здесь не называем — её назначает не бот, а банк при пересчёте
    рублей в сомони. Поэтому и кнопка другая: не «я оплатил», а «я
    отправил» — после неё спросим сумму из чека.
    """
    kb = InlineKeyboardBuilder()
    if number:
        kb.row(InlineKeyboardButton(
            text="📋 Нусхаи рақам",
            copy_text=CopyTextButton(text=number),
        ))
    kb.row(btn("✅ Ман фиристодам", "dep:ru_sent", style=PRIMARY))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def deposit_open(deposit_id: int) -> InlineKeyboardMarkup:
    """Что делать с уже заведённой заявкой: прислать чек или отменить."""
    kb = InlineKeyboardBuilder()
    kb.row(btn("🧾 Чек фиристодан", "dep:paid", style=PRIMARY))
    kb.row(btn("🗑 Дархостро бекор кардан", f"dep:drop:{deposit_id}", style=DANGER))
    kb.row(btn(labeled("back", "Ба меню"), "m:main"))
    return kb.as_markup()


def deposit_receipt() -> InlineKeyboardMarkup:
    """Шаг чека: вернуться к реквизитам или выйти."""
    kb = InlineKeyboardBuilder()
    kb.row(btn("‹ Реквизитҳо", "dep:back"))
    kb.row(btn(labeled("cancel", "Бекор"), "m:main", style=DANGER))
    return kb.as_markup()


def profile() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("history", "Таърихи харидҳо"), "p:history", icon="history"))
    kb.row(btn(labeled("promo", "Промокод"), "p:promo", style=SUCCESS, icon="promo"))
    kb.row(btn(labeled("referral", "Муаррифшудагон"), "p:ref", icon="referral"))
    kb.row(btn(labeled("back", "Ба меню"), "m:main"))
    return kb.as_markup()


def support_menu(has_open: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_open:
        kb.row(btn(labeled("edit", "Ба муроҷиат илова кардан"), "t:reply", style=PRIMARY))
    else:
        kb.row(btn(labeled("support", "Ба дастгирӣ навиштан"), "t:new",
                   style=PRIMARY, icon="support"))
    kb.row(btn(labeled("back", "Ба меню"), "m:main"))
    return kb.as_markup()


# ══════════════════════════════════════════════════════════════ админ


def admin_deposit(deposit_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("ok", "Зачислить"), f"a:dep_ok:{deposit_id}", style=SUCCESS))
    kb.row(btn(labeled("fail", "Отклонить"), f"a:dep_no:{deposit_id}", style=DANGER))
    return kb.as_markup()


def admin_retry(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(labeled("refresh", "Повторить выдачу"), f"a:retry:{order_id}",
               style=PRIMARY))
    return kb.as_markup()
