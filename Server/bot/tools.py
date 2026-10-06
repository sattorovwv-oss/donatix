"""Инструменты, которые Claude вызывает во время разговора.

Каждый инструмент — чистая функция над базой: ничего не отправляет в Telegram
сам. Побочные эффекты (послать фото, послать PDF) складываются в TurnResult,
а обработчик выполняет их после завершения цикла. Так всё тестируется офлайн.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from . import db, reports

log = logging.getLogger(__name__)

MAX_LIST = 60


@dataclass
class TurnResult:
    """Что накопилось за один ход разговора."""
    reply: str = ""
    saved_transaction_ids: list[int] = field(default_factory=list)
    documents_to_send: list[int] = field(default_factory=list)
    reports_to_send: list[Path] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    owner_id: int
    result: TurnResult
    reports_dir: Path
    font_path: str | None = None
    font_bold_path: str | None = None
    default_currency: str = "TJS"
    today: str = ""
    tz: Any = None            # ZoneInfo пользователя
    now: Any = None           # datetime в UTC — в тестах передаётся явно


# ── проверка и приведение входных данных ───────────────────────────────────
# Whisper и модель иногда дают «почти правильные» значения: дату в другом
# формате, сумму строкой, валюту по-русски. Приводим их сами, а не падаем.

_CURRENCY_ALIASES = {
    "сомони": "TJS", "somoni": "TJS", "tjs": "TJS",
    "тенге": "KZT", "tenge": "KZT", "kzt": "KZT",
    "рубль": "RUB", "рублей": "RUB", "руб": "RUB", "rub": "RUB",
    "доллар": "USD", "долларов": "USD", "usd": "USD", "$": "USD",
    "сум": "UZS", "сума": "UZS", "сумов": "UZS", "uzs": "UZS",
    "евро": "EUR", "eur": "EUR",
    "сом": "KGS", "kgs": "KGS",
}


def clean_currency(value: Any, default: str) -> str:
    if not value:
        return default
    text = str(value).strip().lower()
    return _CURRENCY_ALIASES.get(text, text.upper()[:6]) or default


def clean_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def clean_date(value: Any) -> str | None:
    """Принять YYYY-MM-DD, DD.MM.YYYY или ISO-время. Иначе — None."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def clean_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    return text if text in allowed else None


def _tx_brief(row: dict[str, Any]) -> dict[str, Any]:
    """Компактный вид записи — чтобы не раздувать контекст модели."""
    out = {
        "id": row["id"],
        "дата": row.get("happened_on") or row["created_at"][:10],
        "направление": row.get("direction"),
        "тип": row.get("kind"),
        "кто": row.get("counterparty"),
        "сумма": row.get("amount"),
        "валюта": row.get("currency"),
        "за_что": row.get("item"),
    }
    if row.get("quantity") is not None:
        out["количество"] = row["quantity"]
        out["единица"] = row.get("unit")
    if row.get("due_date"):
        out["срок"] = row["due_date"]
    if row.get("note"):
        out["заметка"] = row["note"]
    return {k: v for k, v in out.items() if v not in (None, "")}


def _doc_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "загружено": row["created_at"][:10],
        "тип": row.get("doc_kind"),
        "описание": row.get("description"),
        "привязано_к_операции": row.get("transaction_id"),
    }


# ── реализации инструментов ────────────────────────────────────────────────

def t_save_transaction(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    happened = clean_date(args.get("happened_on")) or ctx.today or date.today().isoformat()
    amount = clean_amount(args.get("amount"))
    tx_id = db.add_transaction(
        ctx.conn, ctx.owner_id,
        happened_on=happened,
        direction=clean_enum(args.get("direction"), db.DIRECTIONS) or "out",
        kind=clean_enum(args.get("kind"), db.KINDS) or "transfer",
        counterparty=(args.get("counterparty") or None),
        amount=amount,
        currency=clean_currency(args.get("currency"), ctx.default_currency) if amount is not None else None,
        item=(args.get("item") or None),
        quantity=clean_amount(args.get("quantity")),
        unit=(args.get("unit") or None),
        due_date=clean_date(args.get("due_date")),
        note=(args.get("note") or None),
        raw_text=(args.get("raw_text") or None),
        source=(args.get("source") or None),
    )
    ctx.result.saved_transaction_ids.append(tx_id)
    _sync_due_reminders(ctx, tx_id)
    saved = db.get_transaction(ctx.conn, ctx.owner_id, tx_id)
    return {"ok": True, "сохранено": _tx_brief(saved) if saved else {"id": tx_id}}


def _sync_due_reminders(ctx: ToolContext, tx_id: int) -> None:
    """Напоминания о денежном сроке ставит код, а не решение модели: Claude может
    забыть вызвать инструмент, а забытый денежный срок — это потерянные деньги."""
    if ctx.tz is None:
        return
    from . import reminders as rem

    try:
        tz = db.user_tz(ctx.conn, ctx.owner_id, ctx.tz)
        rem.schedule_for_transaction(ctx.conn, ctx.owner_id, tx_id, tz, now=ctx.now)
    except Exception:  # напоминание — не повод потерять саму запись
        log.exception("Не удалось поставить напоминание о сроке для операции %s", tx_id)


def t_search_transactions(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or MAX_LIST)
    rows = db.search_transactions(
        ctx.conn, ctx.owner_id,
        text=args.get("text") or None,
        counterparty=args.get("counterparty") or None,
        date_from=clean_date(args.get("date_from")),
        date_to=clean_date(args.get("date_to")),
        direction=clean_enum(args.get("direction"), db.DIRECTIONS),
        kind=clean_enum(args.get("kind"), db.KINDS),
        limit=max(1, min(limit, 500)),
    )
    return {
        "найдено": len(rows),
        "операции": [_tx_brief(r) for r in rows],
        "итоги_по_валютам": db.totals_by_currency(rows),
    }


def t_update_transaction(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    tx_id = int(args["transaction_id"])
    fields: dict[str, Any] = {}
    for key in ("counterparty", "item", "unit", "note", "raw_text"):
        if args.get(key):
            fields[key] = args[key]
    if args.get("amount") is not None:
        fields["amount"] = clean_amount(args["amount"])
    if args.get("quantity") is not None:
        fields["quantity"] = clean_amount(args["quantity"])
    if args.get("currency"):
        fields["currency"] = clean_currency(args["currency"], ctx.default_currency)
    for key in ("happened_on", "due_date"):
        if args.get(key):
            fields[key] = clean_date(args[key])
    if args.get("direction"):
        fields["direction"] = clean_enum(args["direction"], db.DIRECTIONS)
    if args.get("kind"):
        fields["kind"] = clean_enum(args["kind"], db.KINDS)

    if not db.update_transaction(ctx.conn, ctx.owner_id, tx_id, **fields):
        return {"ok": False, "ошибка": f"Операция {tx_id} не найдена или менять нечего."}
    _sync_due_reminders(ctx, tx_id)   # срок мог поменяться — пересоздаём
    updated = db.get_transaction(ctx.conn, ctx.owner_id, tx_id)
    return {"ok": True, "обновлено": _tx_brief(updated) if updated else {"id": tx_id}}


def t_delete_transaction(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    tx_id = int(args["transaction_id"])
    if not db.delete_transaction(ctx.conn, ctx.owner_id, tx_id):
        return {"ok": False, "ошибка": f"Операция {tx_id} не найдена."}
    db.cancel_reminders_for_transaction(ctx.conn, ctx.owner_id, tx_id)
    return {"ok": True, "удалено": tx_id}


def t_describe_document(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    doc_id = int(args["document_id"])
    tx_id = args.get("transaction_id")
    ok = db.describe_document(
        ctx.conn, ctx.owner_id, doc_id,
        description=str(args.get("description") or "").strip(),
        doc_kind=(args.get("doc_kind") or None),
        transaction_id=int(tx_id) if tx_id else None,
    )
    if not ok:
        return {"ok": False, "ошибка": f"Фото {doc_id} не найдено."}
    doc = db.get_document(ctx.conn, ctx.owner_id, doc_id)
    return {"ok": True, "подписано": _doc_brief(doc) if doc else {"id": doc_id}}


def t_find_documents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = db.search_documents(
        ctx.conn, ctx.owner_id,
        text=args.get("text") or None,
        date_from=clean_date(args.get("date_from")),
        date_to=clean_date(args.get("date_to")),
        limit=int(args.get("limit") or 20),
    )
    return {"найдено": len(rows), "фото": [_doc_brief(r) for r in rows]}


def t_send_documents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raw_ids = args.get("document_ids") or []
    if isinstance(raw_ids, (int, str)):
        raw_ids = [raw_ids]
    sent, missing = [], []
    for value in raw_ids:
        try:
            doc_id = int(value)
        except (TypeError, ValueError):
            continue
        if db.get_document(ctx.conn, ctx.owner_id, doc_id):
            if doc_id not in ctx.result.documents_to_send:
                ctx.result.documents_to_send.append(doc_id)
            sent.append(doc_id)
        else:
            missing.append(doc_id)
    if not sent:
        return {"ok": False, "ошибка": "Ни одного такого фото нет.", "не_найдены": missing}
    return {"ok": True, "будут_отправлены": sent, "не_найдены": missing}


def t_create_reminder(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from . import reminders as rem

    text = " ".join(str(args.get("text") or "").split())[:300]
    if not text:
        return {"ok": False, "ошибка": "Нужен текст напоминания."}

    now = ctx.now or rem.utc_now()
    tz = db.user_tz(ctx.conn, ctx.owner_id, ctx.tz)

    delay = clean_amount(args.get("after_seconds"))
    if delay is not None:
        # «Через 30 секунд» — просто сдвиг от текущего момента: модель не считает
        # дату сама, и целый класс ошибок с часовыми поясами не возникает.
        seconds = int(delay)
        if seconds < rem.MIN_DELAY.total_seconds():
            return {"ok": False, "ошибка": "Слишком скоро — назови хотя бы 5 секунд."}
        if seconds > rem.MAX_DELAY.total_seconds():
            return {"ok": False, "ошибка": "Слишком далеко — не больше года."}
        moment = rem.moment_after(seconds, now)
    else:
        when = clean_date(args.get("when"))
        if not when:
            return {"ok": False, "ошибка": "Нужна дата (ГГГГ-ММ-ДД) или after_seconds."}
        hour = db.reminder_hour(ctx.conn, ctx.owner_id)
        moment = rem.local_moment(when, args.get("time"), tz, hour)
        if moment <= now:
            return {"ok": False, "ошибка": "Этот момент уже прошёл — назови будущее время."}

    fire_at = rem.to_utc_iso(moment)
    reminder_id = db.add_reminder(
        ctx.conn, ctx.owner_id, fire_at=fire_at, text=text, kind="manual"
    )
    return {
        "ok": True,
        "напоминание": {
            "id": reminder_id,
            # Ближе минуты — «через 30 секунд», иначе дата и время.
            "когда": rem.humanize_delay(moment, now) or rem.fmt_local(fire_at, tz),
            "текст": text,
        },
    }


def t_list_reminders(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from . import reminders as rem

    rows = db.list_reminders(ctx.conn, ctx.owner_id)
    tz = db.user_tz(ctx.conn, ctx.owner_id, ctx.tz)
    return {
        "найдено": len(rows),
        "напоминания": [
            {"id": r["id"], "когда": rem.fmt_local(r["fire_at"], tz), "текст": r["text"]}
            for r in rows
        ],
    }


def t_cancel_reminder(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reminder_id = int(args["reminder_id"])
    if not db.cancel_reminder(ctx.conn, ctx.owner_id, reminder_id):
        return {"ok": False, "ошибка": f"Напоминания {reminder_id} нет или оно уже прошло."}
    return {"ok": True, "убрано": reminder_id}


def t_build_report(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    date_from = clean_date(args.get("date_from"))
    date_to = clean_date(args.get("date_to"))
    if not date_from or not date_to:
        return {"ok": False, "ошибка": "Нужны обе даты в формате ГГГГ-ММ-ДД."}
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    path, count = reports.build_report(
        ctx.conn, ctx.owner_id,
        date_from=date_from, date_to=date_to,
        out_dir=ctx.reports_dir,
        font_path=ctx.font_path, font_bold_path=ctx.font_bold_path,
        counterparty=args.get("counterparty") or None,
        direction=clean_enum(args.get("direction"), db.DIRECTIONS),
        text=args.get("text") or None,
    )
    ctx.result.reports_to_send.append(path)
    rows = db.search_transactions(
        ctx.conn, ctx.owner_id,
        text=args.get("text") or None,
        counterparty=args.get("counterparty") or None,
        date_from=date_from, date_to=date_to,
        direction=clean_enum(args.get("direction"), db.DIRECTIONS),
        limit=2000,
    )
    return {
        "ok": True,
        "период": f"{date_from} — {date_to}",
        "операций": count,
        "итоги_по_валютам": db.totals_by_currency(rows),
        "файл_отправлен": True,
    }


HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], dict[str, Any]]] = {
    "save_transaction": t_save_transaction,
    "search_transactions": t_search_transactions,
    "update_transaction": t_update_transaction,
    "delete_transaction": t_delete_transaction,
    "describe_document": t_describe_document,
    "find_documents": t_find_documents,
    "send_documents": t_send_documents,
    "create_reminder": t_create_reminder,
    "list_reminders": t_list_reminders,
    "cancel_reminder": t_cancel_reminder,
    "build_report": t_build_report,
}
