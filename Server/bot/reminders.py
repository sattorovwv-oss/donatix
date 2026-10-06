"""Напоминания: планировщик и правила, по которым они появляются.

Всё состояние лежит в базе, а не в памяти процесса — перезапуск бота
не теряет ни одного назначенного напоминания.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import db

log = logging.getLogger(__name__)

# Пять секунд, а не тридцать: иначе «напомни через 30 секунд» опоздает почти
# на полминуты. Запрос лёгкий — выборка по индексу.
TICK_SECONDS = 5

# Границы для «напомни через …». Меньше — человек не успеет дочитать ответ бота;
# больше года — почти наверняка ошибка распознавания речи.
MIN_DELAY = timedelta(seconds=5)
MAX_DELAY = timedelta(days=365)
# Бот лежал дольше — не вываливать человеку кучу протухших напоминаний.
MAX_LATE = timedelta(days=3)
# Сколько ждать, прежде чем напомнить о неподписанном фото.
PHOTO_AFTER = timedelta(hours=12)
# Просроченный денежный срок напоминает не чаще раза в сутки.
OVERDUE_EVERY = timedelta(days=1)

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


# ── время ──────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("момент без часового пояса — так нельзя, будет сдвиг")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_clock(value: str | None, default_hour: int) -> tuple[int, int, int]:
    """«15:30:45» или «15:30» или «7» → (час, минута, секунда).

    Модель может прислать что угодно — мусор не должен ронять бота,
    поэтому непонятное молча откатывается к часу по умолчанию.
    """
    if not value:
        return default_hour, 0, 0
    parts = str(value).strip().replace(".", ":").split(":")
    numbers = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            return default_hour, 0, 0
    if not numbers:
        return default_hour, 0, 0
    hour = max(0, min(numbers[0], 23))
    minute = max(0, min(numbers[1], 59)) if len(numbers) > 1 else 0
    second = max(0, min(numbers[2], 59)) if len(numbers) > 2 else 0
    return hour, minute, second


def local_moment(
    day: date | str, clock: str | None, tz: ZoneInfo, default_hour: int
) -> datetime:
    """Собрать момент в часовом поясе человека.

    Хранить будем в UTC: иначе смена TZ или переход на летнее время сдвинет
    уже назначенные напоминания.
    """
    if isinstance(day, str):
        day = datetime.strptime(day[:10], "%Y-%m-%d").date()
    hour, minute, second = parse_clock(clock, default_hour)
    return datetime.combine(day, time(hour, minute, second), tzinfo=tz)


def moment_after(seconds: int, now: datetime | None = None) -> datetime:
    """Момент «через N секунд». Пояс тут не при чём — это просто сдвиг."""
    return (now or utc_now()) + timedelta(seconds=int(seconds))


def humanize_delay(moment: datetime, now: datetime) -> str | None:
    """«через 30 секунд» вместо даты, когда до срабатывания меньше минуты.

    «Напомню 31.08.2026 в 17:32» на просьбу «через 30 секунд» выглядит глупо.
    """
    delta = moment - now
    if delta >= timedelta(minutes=1) or delta < timedelta(0):
        return None
    seconds = max(1, round(delta.total_seconds()))
    if seconds % 10 == 1 and seconds % 100 != 11:
        word = "секунду"
    elif 2 <= seconds % 10 <= 4 and not 12 <= seconds % 100 <= 14:
        word = "секунды"
    else:
        word = "секунд"
    return f"через {seconds} {word}"


def fmt_local(iso: str, tz: ZoneInfo) -> str:
    moment = parse_utc(iso).astimezone(tz)
    pattern = "%d.%m.%Y в %H:%M:%S" if moment.second else "%d.%m.%Y в %H:%M"
    return moment.strftime(pattern)


# ── тексты ─────────────────────────────────────────────────────────────────

def due_text(row: dict[str, Any], *, days_before: int) -> str:
    """Напоминание о денежном сроке — с суммой, чтобы не лезть в отчёт."""
    from .reports import fmt_money

    when = {0: "Сегодня срок", 1: "Завтра срок"}.get(days_before, f"Через {days_before} дн. срок")
    parts = [when]
    if row.get("counterparty"):
        parts.append(row["counterparty"])
    amount = fmt_money(row.get("amount"), row.get("currency"))
    if amount != "—":
        parts.append(amount)
    if row.get("item"):
        parts.append(f"за «{row['item']}»")
    return ": ".join([parts[0], " · ".join(parts[1:])]) if len(parts) > 1 else parts[0]


def overdue_text(row: dict[str, Any], days: int) -> str:
    from .reports import fmt_money

    parts = ["Просрочено" + (f" на {days} дн." if days > 0 else "")]
    tail = [x for x in (
        row.get("counterparty"),
        fmt_money(row.get("amount"), row.get("currency")) if row.get("amount") else None,
        f"за «{row['item']}»" if row.get("item") else None,
    ) if x]
    return ": ".join([parts[0], " · ".join(tail)]) if tail else parts[0]


# ── правила: когда напоминания появляются ──────────────────────────────────

def schedule_for_transaction(
    conn: sqlite3.Connection,
    owner_id: int,
    transaction_id: int,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Поставить напоминания о денежном сроке: за день и в день.

    Делает это код, а не решение модели: Claude может забыть вызвать инструмент,
    а забытый денежный срок — это потерянные деньги.
    """
    now = now or utc_now()
    db.cancel_reminders_for_transaction(conn, owner_id, transaction_id)

    row = db.get_transaction(conn, owner_id, transaction_id)
    if not row or not row.get("due_date"):
        return []

    hour = db.reminder_hour(conn, owner_id)
    due = datetime.strptime(row["due_date"][:10], "%Y-%m-%d").date()
    created = []
    for days_before in (1, 0):
        moment = local_moment(due - timedelta(days=days_before), None, tz, hour)
        if moment <= now:      # срок уже прошёл или сегодня позже времени — не в прошлое
            continue
        created.append(db.add_reminder(
            conn, owner_id,
            fire_at=to_utc_iso(moment),
            text=due_text(row, days_before=days_before),
            kind="due", transaction_id=transaction_id,
        ))
    return created


def backfill_due_reminders(
    conn: sqlite3.Connection, owner_id: int, tz: ZoneInfo, *, now: datetime | None = None
) -> list[int]:
    """Досоздать напоминания для сроков, записанных до появления этой возможности.

    Без этого напоминания работали бы только для новых записей, а всё, что человек
    надиктовал раньше, молча осталось бы без предупреждения.
    """
    now = now or utc_now()
    today = now.astimezone(tz).date()
    created: list[int] = []

    pending_for = {r["transaction_id"] for r in db.list_reminders(conn, owner_id)
                   if r["transaction_id"]}
    for row in db.transactions_with_due_date(conn, owner_id, since=today.isoformat()):
        if row["id"] in pending_for:
            continue
        created += schedule_for_transaction(conn, owner_id, row["id"], tz, now=now)
    return created


def schedule_overdue(
    conn: sqlite3.Connection, owner_id: int, tz: ZoneInfo, *, now: datetime | None = None
) -> list[int]:
    """Срок прошёл, а операция не закрыта — напоминать раз в сутки."""
    now = now or utc_now()
    today = now.astimezone(tz).date()
    hour = db.reminder_hour(conn, owner_id)
    created = []

    for row in db.transactions_with_due_date(conn, owner_id):
        due = datetime.strptime(row["due_date"][:10], "%Y-%m-%d").date()
        if due >= today:
            continue
        if (row.get("note") or "").lower().find("закрыт") >= 0:
            continue
        if any(r["transaction_id"] == row["id"]
               for r in db.list_reminders(conn, owner_id)):
            continue
        moment = local_moment(today + timedelta(days=1), None, tz, hour)
        created.append(db.add_reminder(
            conn, owner_id, fire_at=to_utc_iso(moment),
            text=overdue_text(row, (today - due).days),
            kind="due", transaction_id=row["id"],
        ))
    return created


def schedule_photo_reminders(
    conn: sqlite3.Connection, tz: ZoneInfo, *, now: datetime | None = None
) -> list[int]:
    """Прислал фото и забыл сказать, что это — потом не найдёшь."""
    now = now or utc_now()
    cutoff = (now - PHOTO_AFTER).isoformat(timespec="seconds")
    created = []

    for doc in db.undescribed_documents(conn, cutoff):
        owner_id = doc["owner_id"]
        if db.has_pending_reminder(conn, owner_id, "photo", document_id=doc["id"]):
            continue
        hour = db.reminder_hour(conn, owner_id)
        own = db.user_tz(conn, owner_id, tz)
        moment = local_moment(now.astimezone(own).date(), None, own, hour)
        if moment <= now:
            moment = local_moment(now.astimezone(own).date() + timedelta(days=1), None, own, hour)
        created.append(db.add_reminder(
            conn, owner_id, fire_at=to_utc_iso(moment),
            text="Фото так и не подписано. Скажи, что это — иначе потом не найдёшь.",
            kind="photo", document_id=doc["id"],
        ))
    return created


# ── отправка ───────────────────────────────────────────────────────────────

def format_message(reminder: dict[str, Any], tz: ZoneInfo, *, now: datetime) -> str:
    text = f"⏰ {reminder['text']}"
    late = now - parse_utc(reminder["fire_at"])
    if late > timedelta(minutes=5):
        # Бот лежал — честно говорим, что напоминание задержалось.
        text += f"\n\n(было назначено на {fmt_local(reminder['fire_at'], tz)})"
    return text


async def deliver_due(
    bot: Any, conn: sqlite3.Connection, tz: ZoneInfo, *, now: datetime | None = None
) -> int:
    """Отправить всё, чему пришло время. Возвращает, сколько ушло."""
    from .keyboards import reminder_keyboard

    now = now or utc_now()
    sent = 0

    for reminder in db.due_reminders(conn, to_utc_iso(now)):
        # Показываем время в поясе того, кому шлём, а не в общем.
        owner_tz = db.user_tz(conn, reminder["owner_id"], tz)
        # Слишком старое не шлём: после недельного простоя десяток протухших
        # напоминаний хуже, чем молчание. В /napominaniya они остаются видны.
        if now - parse_utc(reminder["fire_at"]) > MAX_LATE:
            db.mark_sent(conn, reminder["id"])
            log.info("Пропущено как устаревшее: %s", reminder["id"])
            continue

        try:
            await bot.send_message(
                reminder["owner_id"],
                format_message(reminder, owner_tz, now=now),
                reply_markup=reminder_keyboard(reminder["id"]),
            )
            sent += 1
        except Exception:
            # Человек заблокировал бота или удалил чат — это не повод ронять
            # планировщик и мешать всем остальным.
            log.warning("Не доставлено напоминание %s пользователю %s",
                        reminder["id"], reminder["owner_id"])
        db.mark_sent(conn, reminder["id"])

    return sent


async def run_scheduler(
    bot: Any, conn: sqlite3.Connection, tz: ZoneInfo, *, tick: int = TICK_SECONDS
) -> None:
    """Фоновая задача. Гасится отменой из main."""
    log.info("Планировщик напоминаний запущен, проверка раз в %s сек", tick)
    last_daily: date | None = None

    while True:
        try:
            now = utc_now()

            # Раз в сутки досоздаём то, что появляется не по команде.
            today = now.astimezone(tz).date()
            if last_daily != today:
                last_daily = today
                schedule_photo_reminders(conn, tz, now=now)
                for user in db.list_users(conn):
                    if user["status"] == "active":
                        # У каждого свой пояс — «в 9 утра» у всех своё.
                        own = db.user_tz(conn, user["id"], tz)
                        backfill_due_reminders(conn, user["id"], own, now=now)
                        schedule_overdue(conn, user["id"], own, now=now)

            await deliver_due(bot, conn, tz, now=now)
        except asyncio.CancelledError:
            log.info("Планировщик остановлен")
            raise
        except Exception:
            log.exception("Планировщик споткнулся — продолжаю")

        await asyncio.sleep(tick)
