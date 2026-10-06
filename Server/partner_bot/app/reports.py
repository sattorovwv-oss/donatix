"""Периоды для отчётов с поправкой на часовой пояс владельца.

Время в базе хранится в UTC. Владелец в Душанбе (UTC+5), и без поправки
«сегодня» начиналось бы для него в 5 утра, а вечерние заказы попадали бы
во вчерашний день.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app import runtime

#: Готовые периоды: ключ -> (подпись, сколько дней назад начинать)
PRESETS: dict[str, tuple[str, int]] = {
    "today": ("Сегодня", 0),
    "yesterday": ("Вчера", 1),
    "week": ("7 дней", 7),
    "month": ("30 дней", 30),
    "all": ("Всё время", 0),
}


def tz_hours() -> int:
    return runtime.get_int("tz_hours", 5)


def _tz() -> timezone:
    return timezone(timedelta(hours=tz_hours()))


def local_today() -> date:
    return datetime.now(_tz()).date()


def bounds(start: date, end: date) -> tuple[str, str]:
    """Границы периода [start, end] в UTC-строках для запросов к базе.

    end включается целиком: конец считается началом следующего дня.
    """
    tz = _tz()
    since = datetime.combine(start, datetime.min.time(), tz)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), tz)
    to_utc = lambda dt: dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return to_utc(since), to_utc(until)


def preset_range(key: str) -> tuple[date, date, str]:
    """Диапазон дат для готового периода."""
    today = local_today()
    if key == "today":
        return today, today, "Сегодня"
    if key == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "Вчера"
    if key == "week":
        return today - timedelta(days=6), today, "Последние 7 дней"
    if key == "month":
        return today - timedelta(days=29), today, "Последние 30 дней"
    if key == "all":
        return date(2020, 1, 1), today, "Всё время"
    return today, today, "Сегодня"


def parse_range(text: str) -> tuple[date, date] | None:
    """Разобрать «01.08 15.08» или «01.08.2026 15.08.2026».

    Год можно не писать — подставится текущий. Если даты перепутаны
    местами, они меняются: человек чаще ошибается порядком, чем датой.
    """
    parts = text.replace("—", " ").replace("-", " ").replace("по", " ").split()
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) == 1:
        parts = parts * 2
    if len(parts) != 2:
        return None

    year = local_today().year
    parsed = []
    for part in parts:
        chunks = part.split(".")
        try:
            if len(chunks) == 2:
                parsed.append(date(year, int(chunks[1]), int(chunks[0])))
            elif len(chunks) == 3:
                full = int(chunks[2])
                parsed.append(date(full if full > 99 else 2000 + full,
                                   int(chunks[1]), int(chunks[0])))
            else:
                return None
        except ValueError:
            return None

    start, end = parsed
    return (end, start) if start > end else (start, end)
