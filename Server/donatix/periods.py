"""Периоды для отчётов и фильтров: календарные сутки 00:00 → 23:59 по местному времени.

«Сегодня» начинается в 00:00, в полночь счёт идёт с нуля. «7 дней» — сегодня и шесть
предыдущих суток целиком. «С … по …» — обе даты включительно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo

PRESETS = {"today": "Сегодня", "yesterday": "Вчера", "7d": "7 дней", "30d": "30 дней", "month": "Этот месяц",
           "all": "Всё время"}
MAX_DAYS = 3660


@dataclass
class Period:
    key: str                  # today / yesterday / … / custom / all
    label: str
    start: datetime | None    # местное время, полночь; None — с самого начала
    end: datetime | None      # не включительно (полночь следующего дня)
    date_from: str = ""       # для полей «с … по …» (ГГГГ-ММ-ДД)
    date_to: str = ""

    @property
    def a(self) -> str | None:
        return iso(self.start) if self.start else None

    @property
    def b(self) -> str | None:
        return iso(self.end) if self.end else None

    def sql(self, column: str) -> tuple[str, list[str]]:
        """Условие для WHERE: (текст, параметры)."""
        parts, args = [], []
        if self.a:
            parts.append(f"{column} >= ?")
            args.append(self.a)
        if self.b:
            parts.append(f"{column} < ?")
            args.append(self.b)
        return (" AND ".join(parts) or "1=1"), args


def iso(dt: datetime) -> str:
    """В формат времени базы (UTC) для сравнения строк."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def midnight(tz: tzinfo, now: datetime | None = None) -> datetime:
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _day(tz: tzinfo, d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _parse(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def resolve(tz: tzinfo, key: str = "", date_from: str = "", date_to: str = "", *,
            default: str = "all", now: datetime | None = None) -> Period:
    """Период из параметров страницы. Даты «с/по» важнее готового варианта."""
    today = midnight(tz, now)
    d1, d2 = _parse(date_from), _parse(date_to)
    if d1 or d2:
        d1, d2 = d1 or d2, d2 or d1
        if d1 > d2:
            d1, d2 = d2, d1
        if (d2 - d1).days > MAX_DAYS:
            d1 = d2 - timedelta(days=MAX_DAYS)
        label = f"{d1:%d.%m.%Y}" if d1 == d2 else f"{d1:%d.%m.%Y} — {d2:%d.%m.%Y}"
        return Period("custom", label, _day(tz, d1), _day(tz, d2 + timedelta(days=1)), d1.isoformat(), d2.isoformat())
    key = key if key in PRESETS else default
    if key == "today":
        start, end = today, today + timedelta(days=1)
    elif key == "yesterday":
        start, end = today - timedelta(days=1), today
    elif key in ("7d", "30d"):
        start, end = today - timedelta(days=int(key[:-1]) - 1), today + timedelta(days=1)
    elif key == "month":
        start, end = today.replace(day=1), today + timedelta(days=1)
    else:
        return Period("all", PRESETS["all"], None, None)
    # арифметика по местным часам: полночь остаётся полночью
    start, end = _day(tz, start.date()), _day(tz, end.date())
    last = end - timedelta(days=1)
    return Period(key, PRESETS[key], start, end, start.date().isoformat(), last.date().isoformat())
