"""Регионы игровых аккаунтов.

Один и тот же Free Fire у поставщика разложен по регионам: СНГ, Индонезия,
Бразилия и так далее. Это не мелочь оформления — сервер аккаунта решает,
найдётся ли игрок по ID и куда уйдут алмазы. Промахнулись регионом —
проверка ника не находит игрока, а заказ либо отклоняется, либо уходит
чужому человеку.

У поставщика регион зашит в код категории хвостом: free_fire_br,
free_fire_id, free_fire_cis. Отсюда правило: код = семья + регион.
Семья («free_fire») — то, что клиент выбирает первым шагом, регион —
вторым.
"""
from __future__ import annotations

import re

#: Хвост кода → (как показать клиенту, код региона для справочника ников).
#: Второе значение пусто там, где gameskinbo такого региона не знает:
#: слать ему чужой код нельзя, а искать он умеет и без региона.
#: Порядок важен: первым идёт то, что чаще спрашивают в Душанбе.
REGIONS: dict[str, tuple[str, str]] = {
    "cis": ("🌍 СНГ", ""),
    "ru": ("🌍 СНГ", ""),
    "id": ("🇮🇩 Индонезия", "ID"),
    "br": ("🇧🇷 Бразилия", "BR"),
    "global": ("🌐 Глобальный", ""),
    "eu": ("🇪🇺 Европа", "EU"),
    "na": ("🌎 Северная Америка", "NA"),
    "sac": ("🌎 Южная Америка", "SAC"),
    "mena": ("🕌 Ближний Восток", "ME"),
    "me": ("🕌 Ближний Восток", "ME"),
    "ind": ("🇮🇳 Индия", "IND"),
    "in": ("🇮🇳 Индия", "IND"),
    "pk": ("🇵🇰 Пакистан", "PK"),
    "bd": ("🇧🇩 Бангладеш", "BD"),
    "th": ("🇹🇭 Таиланд", "TH"),
    "vn": ("🇻🇳 Вьетнам", "VN"),
    "ph": ("🇵🇭 Филиппины", "PH"),
    "sg": ("🇸🇬 Сингапур", "SG"),
    "my": ("🇲🇾 Малайзия", "SG"),
    "us": ("🇺🇸 США", "US"),
    "tw": ("🇹🇼 Тайвань", "TW"),
    "tr": ("🇹🇷 Турция", "TR"),
    "ua": ("🇺🇦 Украина", ""),
}

#: Порядок кнопок на экране выбора региона: сперва те, что нужны нам.
ORDER = ["cis", "ru", "id", "br"]


#: Регион в названии пишут по-разному: «Free Fire (RU)», «Free Fire — Brazil»,
#: «Free Fire Indonesia». Приводим написание к ключу справочника.
BY_WORD: dict[str, str] = {
    "снг": "cis", "cis": "cis", "россия": "ru", "russia": "ru", "ru": "ru",
    "индонезия": "id", "indonesia": "id", "id": "id", "idn": "id",
    "бразилия": "br", "brazil": "br", "brasil": "br", "br": "br", "bra": "br",
    "глобал": "global", "глобальный": "global", "global": "global",
    "europe": "eu", "европа": "eu", "eu": "eu",
    "индия": "ind", "india": "ind", "ind": "ind", "in": "in",
    "пакистан": "pk", "pakistan": "pk", "pk": "pk",
    "бангладеш": "bd", "bangladesh": "bd", "bd": "bd",
    "таиланд": "th", "thailand": "th", "th": "th",
    "вьетнам": "vn", "vietnam": "vn", "vn": "vn",
    "филиппины": "ph", "philippines": "ph", "ph": "ph",
    "сингапур": "sg", "singapore": "sg", "sg": "sg",
    "малайзия": "my", "malaysia": "my", "my": "my",
    "тайвань": "tw", "taiwan": "tw", "tw": "tw",
    "турция": "tr", "turkey": "tr", "tr": "tr",
    "украина": "ua", "ukraine": "ua", "ua": "ua",
    "ближний восток": "mena", "mena": "mena", "me": "me",
    "сша": "na", "north america": "na", "na": "na",
    "южная америка": "sac", "south america": "sac", "sac": "sac",
}

#: Хвост названия в скобках или после тире: «… (RU)», «… - Brazil».
_TAIL_RE = re.compile(r"[\s\-–—]*[\(\[]([^\)\]]{1,24})[\)\]]\s*$|[\-–—]\s*([^\-–—]{1,24})$")


def _by_word(text: str) -> str:
    return BY_WORD.get((text or "").strip().lower().strip(".,"), "")


def split_code(category_id: str) -> tuple[str, str]:
    """Код категории → (семья, регион).

    Хвост признаётся регионом, только если он есть в справочнике, — иначе
    pubg_mobile превратился бы в семью «pubg» с «регионом» mobile.
    """
    code = (category_id or "").strip().lower()
    head, _, tail = code.rpartition("_")
    if head and tail in REGIONS:
        return head, tail
    return code, ""


def split_title(title: str) -> tuple[str, str]:
    """Название → (название без региона, регион).

    Поставщик часто пишет регион прямо в названии: «Magic Chess Go Go (RU)».
    Если это не разобрать, у клиента получится десяток одинаковых кнопок.
    """
    name = (title or "").strip()
    found = _TAIL_RE.search(name)
    if not found:
        return name, ""
    tail = found.group(1) or found.group(2) or ""
    key = _by_word(tail)
    if not key:
        return name, ""
    return name[:found.start()].strip(" -–—"), key


def split(category_id: str, title: str = "") -> tuple[str, str]:
    """Игра → (ключ семьи, регион). Код важнее названия: он точнее."""
    family, region = split_code(category_id)
    if region:
        return family, region
    base, region = split_title(title)
    if region:
        return base.lower(), region
    return family, ""


def _pair(game_or_code, title: str = "") -> tuple[str, str]:
    """Принимаем и игру из базы, и голый код — так короче на вызове."""
    if isinstance(game_or_code, str):
        return split(game_or_code, title)
    return split(getattr(game_or_code, "category_id", ""),
                 getattr(game_or_code, "title", ""))


def family_of(game_or_code, title: str = "") -> str:
    return _pair(game_or_code, title)[0]


def suffix_of(game_or_code, title: str = "") -> str:
    return _pair(game_or_code, title)[1]


def title_of(game_or_code, title: str = "") -> str:
    """Как назвать регион на кнопке. Пусто — регион не распознан."""
    tail = suffix_of(game_or_code, title)
    return REGIONS[tail][0] if tail else ""


def nick_region(game_or_code, title: str = "") -> str:
    """Код региона для сервиса ников."""
    tail = suffix_of(game_or_code, title)
    return REGIONS[tail][1] if tail else ""


def clean_title(game_or_code, title: str = "") -> str:
    """Название игры без приписки региона — для кнопки в меню."""
    if isinstance(game_or_code, str):
        name = title
    else:
        name = getattr(game_or_code, "title", "")
    base, region = split_title(name)
    return base if region and base else name


def sort_key(game_or_code, title: str = "") -> tuple[int, str]:
    """Сортировка регионов: сначала нужные нам, потом остальные по алфавиту."""
    family, tail = _pair(game_or_code, title)
    return (ORDER.index(tail) if tail in ORDER else len(ORDER), tail or family)


def group(games: list) -> list[dict]:
    """Сгруппировать игры по семьям, сохранив порядок появления.

    Клиент не должен видеть три «Free Fire» подряд: он выбирает игру,
    а регион — следующим шагом.
    """
    out: dict[str, dict] = {}
    for game in games:
        family = family_of(game)
        holder = out.setdefault(
            family, {"family": family, "title": clean_title(game), "games": []}
        )
        holder["games"].append(game)
    for holder in out.values():
        holder["games"].sort(key=sort_key)
    return list(out.values())


def region_title(game) -> str:
    """Подпись региона для кнопки: из справочника, иначе название игры."""
    return title_of(game) or game.title
