"""Часовые пояса: время на сайте — такое, как у человека на часах.

Браузер сам сообщает свой пояс (cookie dx_tz, ставит скрипт в base.html). Клиент может
выбрать пояс вручную в кабинете — тогда выбор хранится в базе и работает на всех его
устройствах. Если ничего не известно — время Душанбе. В базе время всегда в UTC.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from . import db

DEFAULT = "Asia/Dushanbe"
AUTO = "auto"

# Что предлагаем выбрать вручную: Таджикистан, Россия и соседи
ZONES: list[tuple[str, str]] = [
    ("Asia/Dushanbe", "Таджикистан — Душанбе"),
    ("Europe/Kaliningrad", "Россия — Калининград"),
    ("Europe/Moscow", "Россия — Москва"),
    ("Europe/Samara", "Россия — Самара"),
    ("Asia/Yekaterinburg", "Россия — Екатеринбург"),
    ("Asia/Omsk", "Россия — Омск"),
    ("Asia/Novosibirsk", "Россия — Новосибирск"),
    ("Asia/Krasnoyarsk", "Россия — Красноярск"),
    ("Asia/Irkutsk", "Россия — Иркутск"),
    ("Asia/Yakutsk", "Россия — Якутск"),
    ("Asia/Vladivostok", "Россия — Владивосток"),
    ("Asia/Magadan", "Россия — Магадан"),
    ("Asia/Kamchatka", "Россия — Камчатка"),
    ("Asia/Tashkent", "Узбекистан — Ташкент"),
    ("Asia/Bishkek", "Кыргызстан — Бишкек"),
    ("Asia/Almaty", "Казахстан — Алматы"),
    ("Asia/Kabul", "Афганистан — Кабул"),
    ("Europe/Istanbul", "Турция — Стамбул"),
    ("Asia/Dubai", "ОАЭ — Дубай"),
]
_NAMES = dict(ZONES)
_VALID = re.compile(r"^[A-Za-z]+(?:/[A-Za-z0-9_+\-]+){1,2}$")
_FALLBACK = {"Asia/Dushanbe": 5, "Europe/Moscow": 3, "Asia/Tashkent": 5, "Asia/Bishkek": 6}


def zone(name: str | None) -> tzinfo:
    """Пояс по имени IANA; неизвестное или странное имя — Душанбе."""
    name = name if name and _VALID.match(name) else DEFAULT
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(DEFAULT)
        except Exception:   # нет базы поясов в системе
            return timezone(timedelta(hours=_FALLBACK.get(name, 5)))


def valid(name: str | None) -> bool:
    if not name or not _VALID.match(name):
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return name in _FALLBACK


def user_choice(conn: sqlite3.Connection, user_id: int) -> str:
    """Выбор клиента: имя пояса или 'auto' (по устройству)."""
    value = db.get_setting(conn, f"tz.user.{user_id}") or AUTO
    return value if value == AUTO or valid(value) else AUTO


def set_user_choice(conn: sqlite3.Connection, user_id: int, value: str) -> None:
    db.set_setting(conn, f"tz.user.{user_id}", value if value == AUTO or valid(value) else AUTO)


def site_zone_name(conn: sqlite3.Connection) -> str:
    """Пояс для отчётов админа."""
    value = db.get_setting(conn, "site.timezone") or DEFAULT
    return value if valid(value) else DEFAULT


def resolve(conn: sqlite3.Connection | None, user: Any, cookie: str | None) -> tuple[str, str]:
    """→ (имя пояса, выбор клиента). Ручной выбор важнее того, что сообщил браузер."""
    choice = AUTO
    if conn is not None and user is not None:
        choice = user_choice(conn, user["id"])
        if choice != AUTO:
            return choice, choice
    return (cookie if valid(cookie) else DEFAULT), choice


def parse(value: Any) -> datetime | None:
    """Время из базы (ISO, UTC; с Z, +00:00 или без пояса) → aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local(value: Any, name: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    dt = parse(value)
    if dt is None:
        return str(value or "")
    return dt.astimezone(zone(name)).strftime(fmt)


def label(name: str | None) -> str:
    """«Таджикистан — Душанбе (UTC+5)»; для неизвестного — само имя пояса."""
    offset = datetime.now(timezone.utc).astimezone(zone(name)).utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(minutes), 60)
    utc = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    title = _NAMES.get(name or DEFAULT) or (name or DEFAULT).split("/")[-1].replace("_", " ")
    return f"{title} ({utc})"
