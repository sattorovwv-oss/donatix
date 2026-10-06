"""Все эмодзи бота в одном месте — каждое меняется из админ-панели.

Тексты и клавиатуры не пишут значки напрямую, а вставляют токен вида
[[stars]]. Подстановка происходит при обращении, поэтому смена значка в
панели видна сразу, без перезапуска.
"""
from __future__ import annotations

import re

# Группа -> {ключ: (значок по умолчанию,понятное название)}
GROUPS: dict[str, dict[str, tuple[str, str]]] = {
    "Товары": {
        "stars": ("⭐️", "Звёзды"),
        "premium": ("👑", "Telegram Premium"),
        "gift": ("🎁", "Подарок"),
        "steam": ("🎮", "Steam"),
        "game": ("🔥", "Игры, Free Fire"),
        "pubg": ("🎯", "PUBG"),
    },
    "Деньги": {
        "money": ("💰", "Баланс"),
        "deposit": ("💳", "Пополнение"),
        # Свой значок, а не общий с «Пополнить»: они стоят на соседних
        # экранах, и один и тот же значок на обоих читается как одна и
        # та же кнопка.
        "dcity": ("🏦", "Душанбе Сити"),
        "price": ("🏷", "Цена"),
        "refund": ("↩️", "Возврат"),
        "receipt": ("🧾", "Чек, заказ"),
    },
    "Разделы": {
        "profile": ("👤", "Профиль"),
        "history": ("📜", "История"),
        "promo": ("🎟", "Промокод"),
        "referral": ("👥", "Рефералы"),
        "support": ("💬", "Поддержка"),
        "info": ("ℹ️", "Информация"),
        "reviews": ("👍", "Отзывы"),
        "top": ("🏆", "Топ клиентов"),
        "calc": ("🖩", "Калькулятор"),
    },
    "Состояния": {
        "ok": ("✅", "Успех"),
        "fail": ("❌", "Ошибка"),
        "warn": ("⚠️", "Предупреждение"),
        "wait": ("⏳", "Ожидание"),
        "search": ("🔍", "Проверка"),
        "party": ("🎉", "Праздник"),
        "block": ("🚫", "Запрет"),
    },
    "Навигация": {
        "back": ("‹", "Назад"),
        "cancel": ("✖️", "Отмена"),
        "confirm": ("✅", "Подтвердить"),
        "edit": ("✏️", "Изменить"),
        "refresh": ("🔄", "Обновить"),
        "point": ("👇", "Указатель вниз"),
    },
}

#: Премиум-эмодзи владельца: ключ -> ID. Подставляются сразу после
#: установки, но видны, только когда премиум-эмодзи включены проверкой.
#: Убрать любой из них можно кнопкой в панели — она пишет пустое значение,
#: а не сбрасывает к этому списку.
PREMIUM_IDS: dict[str, str] = {
    "money":    "5258204546391351475",
    "point":    "5231102735817918643",
    "stars":    "5258165702707125574",
    "premium":  "5805553606635559688",
    "deposit":  "5902206159095339799",
    "dcity":    "5281014258706979558",
    "profile":  "5256143829672672750",
    "support":  "5258337316715373336",
    "calc":     "5226513232549664618",
    "info":     "5258503720928288433",
    "top":      "5469967260380612012",
    "game":     "6012423622730192070",
    "pubg":     "5204252919565657978",
}

#: Значок известной игры по её коду у поставщика. Игр много, и заводить
#: каждой настройку в панели незачем: те, что мы правда продаём, узнаются
#: по коду. Свой значок у игры, если он задан, всё равно важнее.
GAME_EMOJI: dict[str, str] = {
    # Free Fire делит значок с разделом игр: он же стоит на кнопке
    # «Игры и Steam», и заводить второй такой же незачем.
    "free_fire": "game",
    "freefire": "game",
    "pubg": "pubg",
    "pubg_mobile": "pubg",
    "pubgm": "pubg",
}


def game_key(category_id: str) -> str:
    """Ключ значка для игры. Пусто — своего значка у неё нет."""
    from app.services import regions

    family = regions.family_of(category_id)
    if family in GAME_EMOJI:
        return GAME_EMOJI[family]
    # Коды у поставщиков пишут по-разному: pubg_mobile_global, freefire_br.
    for mark, key in GAME_EMOJI.items():
        if mark in family:
            return key
    return ""

#: Плоский словарь ключ -> значок по умолчанию
DEFAULTS: dict[str, str] = {
    key: value for group in GROUPS.values() for key, (value, _) in group.items()
}
#: Ключ -> человеческое название
TITLES: dict[str, str] = {
    key: title for group in GROUPS.values() for key, (_, title) in group.items()
}

TOKEN_RE = re.compile(r"\[\[(\w+)\]\]")


def em(key: str) -> str:
    """Обычный значок — тот, что виден всем и годится для кнопок."""
    from app import runtime

    return runtime.get(f"emoji_{key}") or DEFAULTS.get(key, "")


def custom_id(key: str) -> str:
    """ID премиум-эмодзи для этого значка, если он задан."""
    from app import runtime

    return runtime.get(f"emoji_id_{key}")


def premium_on() -> bool:
    """Разрешено ли подставлять премиум-эмодзи.

    Выключено по умолчанию: если у владельца нет Telegram Premium,
    Telegram отвергает такие сообщения целиком — бот замолчал бы весь.
    Включается в панели только после успешной проверки.
    """
    from app import runtime

    return runtime.get_bool("custom_emoji_on")


def em_html(key: str) -> str:
    """Значок для сообщения: премиум-эмодзи, если задан и разрешён.

    Обычный значок остаётся внутри тега запасным вариантом — его увидят
    те, у кого премиум-эмодзи не отображается.
    """
    plain = em(key)
    emoji_id = custom_id(key)
    if not emoji_id or not premium_on():
        return plain
    return f'<tg-emoji emoji-id="{emoji_id}">{plain}</tg-emoji>'


def substitute(text: str) -> str:
    """Заменить [[токены]] на значки — с премиум-эмодзи, если они включены."""
    return TOKEN_RE.sub(lambda m: em_html(m.group(1)), text)


def substitute_plain(text: str) -> str:
    """То же, но всегда обычными значками — для кнопок и служебных мест,
    где разметка не поддерживается."""
    return TOKEN_RE.sub(lambda m: em(m.group(1)), text)


def extract_custom(message) -> tuple[str, str] | None:
    """Достать премиум-эмодзи из присланного сообщения.

    Возвращает (id, запасной значок) или None, если это обычный текст.
    Так владельцу не нужно искать ID руками — достаточно прислать эмодзи.
    """
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "text", "") or ""
    for entity in entities:
        if getattr(entity, "type", "") != "custom_emoji":
            continue
        emoji_id = getattr(entity, "custom_emoji_id", "")
        if not emoji_id:
            continue
        # Telegram считает смещения в единицах UTF-16, а Python — в
        # символах. Для эмодзи вне базовой плоскости (а это почти все
        # премиум-эмодзи) обычный срез захватил бы соседний символ.
        start = getattr(entity, "offset", 0)
        length = getattr(entity, "length", 1)
        units = text.encode("utf-16-le")
        fallback = units[start * 2:(start + length) * 2].decode(
            "utf-16-le", errors="ignore"
        ).strip()
        return str(emoji_id), fallback or "⭐️"
    return None


def is_emoji_like(value: str) -> bool:
    """Грубая проверка: значок должен быть коротким и без букв.

    Строгая проверка на эмодзи не нужна — владелец может захотеть поставить
    любой символ вроде «•» или «›», лишь бы это не было предложением.
    """
    value = value.strip()
    if not value or len(value) > 8:
        return False
    return not any(char.isalnum() for char in value)
