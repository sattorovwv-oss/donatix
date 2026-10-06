"""Кассиры: проверяют чеки о пополнении по своим банкам прямо в Telegram.

Админ добавляет человека по Telegram ID в админ-боте и выбирает банки: все или
отдельные. Чек по такому банку приходит и админу, и кассиру — с кнопками
«Зачислить / Отклонить». Кто бы ни нажал первым, у всех копий сообщения кнопки
пропадают и появляется итог («✅ Зачислено — Али»), так что второй раз заявку
не примут.

Доход кассира — его процент (по умолчанию 20%) от нашей чистой прибыли,
пропорционально деньгам, пришедшим через его банки:

  доход кассира = прибыль × (пополнения через его банки ÷ все пополнения) × процент

Остальное (80%) — владельцу. Считается за сутки 00:00 → 00:00, как финансовый отчёт.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any

import httpx

from . import db
from .config import Config
from .money import fmt

log = logging.getLogger(__name__)

ALL = "*"
DEFAULT_PERCENT = 20


class CashierError(ValueError):
    pass


# ── Список кассиров ────────────────────────────────────────


def listing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cashiers ORDER BY created_at, tg_id").fetchall()


def get(conn: sqlite3.Connection, tg_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cashiers WHERE tg_id = ?", (tg_id,)).fetchone()


def active(conn: sqlite3.Connection, tg_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cashiers WHERE tg_id = ? AND active = 1", (tg_id,)).fetchone()


def parse_id(text: str) -> int:
    raw = text.strip().lstrip("@")
    if not raw.isdigit() or not 5 <= len(raw) <= 15:
        raise CashierError("Нужен числовой Telegram ID, например 123456789. "
                           "Человек узнает его, нажав /start в этом боте.")
    return int(raw)


def add(conn: sqlite3.Connection, tg_id: int, name: str, admin_chat: str = "") -> None:
    name = " ".join(name.split())[:40]
    if len(name) < 2:
        raise CashierError("Имя — от 2 до 40 символов.")
    if str(tg_id) == str(admin_chat).strip():
        raise CashierError("Это ваш собственный ID — вы и так получаете все чеки.")
    if get(conn, tg_id):
        raise CashierError("Этот человек уже в списке кассиров.")
    conn.execute("INSERT INTO cashiers (tg_id, name, methods, percent, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                 (tg_id, name, ALL, DEFAULT_PERCENT, db.now()))


def delete(conn: sqlite3.Connection, tg_id: int) -> None:
    conn.execute("DELETE FROM cashiers WHERE tg_id = ?", (tg_id,))


def set_active(conn: sqlite3.Connection, tg_id: int, on: bool) -> None:
    conn.execute("UPDATE cashiers SET active = ? WHERE tg_id = ?", (1 if on else 0, tg_id))


def bump_percent(conn: sqlite3.Connection, tg_id: int, delta: int) -> None:
    conn.execute("UPDATE cashiers SET percent = MAX(0, MIN(100, percent + ?)) WHERE tg_id = ?", (delta, tg_id))


# ── Банки кассира ──────────────────────────────────────────


def bank_methods(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    """Банки с чеками — то, что кассир может проверять. Крипта с автопроверкой сюда не входит."""
    from .payments import settings
    return [m for m in settings(conn, config)["all_methods"] if not m.get("auto")]


def codes_of(row: sqlite3.Row) -> list[str] | None:
    """None — все банки."""
    if row["methods"] == ALL:
        return None
    try:
        return [str(c) for c in json.loads(row["methods"])]
    except (ValueError, TypeError):
        return []


def toggle_all(conn: sqlite3.Connection, tg_id: int) -> None:
    row = get(conn, tg_id)
    if row is not None:
        conn.execute("UPDATE cashiers SET methods = ? WHERE tg_id = ?",
                     ("[]" if row["methods"] == ALL else ALL, tg_id))


def toggle_method(conn: sqlite3.Connection, config: Config, tg_id: int, code: str) -> None:
    row = get(conn, tg_id)
    if row is None:
        return
    codes = codes_of(row)
    if codes is None:   # были «все» — оставляем все, кроме этого
        codes = [m["code"] for m in bank_methods(conn, config) if m["code"] != code]
    elif code in codes:
        codes.remove(code)
    else:
        codes.append(code)
    conn.execute("UPDATE cashiers SET methods = ? WHERE tg_id = ?", (json.dumps(codes), tg_id))


def handles(conn: sqlite3.Connection, config: Config, row: sqlite3.Row | None, method: str) -> bool:
    if row is None or not row["active"]:
        return False
    if _is_auto(conn, config, method):   # автоплатежи (крипта) проверяет блокчейн — кассиру их не решать
        return False
    codes = codes_of(row)
    return codes is None or method in codes


def _is_auto(conn: sqlite3.Connection, config: Config, method: str) -> bool:
    from .payments import is_auto
    return is_auto(conn, config, method)


def banks_label(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> str:
    codes = codes_of(row)
    if codes is None:
        return "все банки"
    titles = [m["title"] for m in bank_methods(conn, config) if m["code"] in codes]
    return ", ".join(titles) if titles else "не выбраны — чеки не приходят"


def targets(conn: sqlite3.Connection, config: Config, method: str) -> list[sqlite3.Row]:
    """Кому из кассиров отправить чек по этому банку."""
    return [r for r in listing(conn) if handles(conn, config, r, method)]


# ── Отправка чека и правка всех копий после решения ────────


CAPTION_MAX = 1024   # предел Telegram для подписи к фото/файлу


def safe_caption(caption: str) -> str:
    """Подпись не длиннее 1024 символов. Обрезать HTML посередине тега нельзя — Telegram
    тогда вообще не отправит сообщение. Длинную подпись сокращаем по строкам с конца."""
    if len(caption) <= CAPTION_MAX:
        return caption
    lines = caption.split("\n")
    while lines and len("\n".join(lines)) > CAPTION_MAX - 2:
        lines.pop()
    return "\n".join(lines) + "\n…"


def _plain(caption: str) -> str:
    import html as _html
    import re as _re
    return _html.unescape(_re.sub(r"<[^>]+>", "", caption))[:CAPTION_MAX]


def send_file(config: Config, chat_id: str | int, caption: str, buttons: list | None, path, *,
              photo: bool) -> int | None:
    """Файл с кнопками в любой чат админ-бота. Вернёт message_id, чтобы потом поправить или удалить.
    Не принял HTML — повторяем простым текстом: заявка не должна теряться."""
    if not config.alert_telegram_token:
        return None
    from .tgbot import keyboard
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    base = {"chat_id": str(chat_id)}
    if buttons:
        base["reply_markup"] = json.dumps(keyboard(buttons), ensure_ascii=False)
    attempts = [{**base, "caption": safe_caption(caption), "parse_mode": "HTML"}, {**base, "caption": _plain(caption)}]
    for data in attempts:
        try:
            with open(path, "rb") as fh:
                resp = httpx.post(f"https://api.telegram.org/bot{config.alert_telegram_token}/{method}",
                                  data=data, files={field: (path.name, fh)}, timeout=30)
            body = resp.json() or {}
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("не удалось отправить чек в чат %s: %s", chat_id, exc)
            return None
        if body.get("ok"):
            return int((body.get("result") or {}).get("message_id") or 0) or None
        log.warning("чек в чат %s не принят Telegram: %s", chat_id, body.get("description"))
    return None


def remember(conn: sqlite3.Connection, payment_id: int, chat_id: str | int, message_id: int | None,
             caption: str) -> None:
    if message_id:
        conn.execute("INSERT OR IGNORE INTO payment_msgs (payment_id, chat_id, message_id, caption) "
                     "VALUES (?, ?, ?, ?)", (payment_id, str(chat_id), int(message_id), caption[:4000]))


def is_tracked(conn: sqlite3.Connection, chat_id: str | int, message_id: int | None) -> bool:
    return bool(message_id) and conn.execute(
        "SELECT 1 FROM payment_msgs WHERE chat_id = ? AND message_id = ?", (str(chat_id), int(message_id))
    ).fetchone() is not None


def settle(conn: sqlite3.Connection, config: Config, payment_id: int, result: str, api: Any = None) -> None:
    """Заявка решена — у всех копий чека (админ и кассиры) убираем кнопки и пишем итог.

    api — клиент админ-бота (из самого бота, синхронно). Без него — фоновым потоком,
    чтобы веб-страница не ждала Telegram."""
    rows = [dict(r) for r in conn.execute(
        "SELECT chat_id, message_id, caption FROM payment_msgs WHERE payment_id = ?", (payment_id,)).fetchall()]
    if not rows:
        return
    edits = [{"chat_id": r["chat_id"], "message_id": r["message_id"], "parse_mode": "HTML",
              "caption": safe_caption(f"{r['caption']}\n\n<b>{_e(result)}</b>")} for r in rows]
    def one(post) -> None:
        for e in edits:
            # Чек — фото с подписью; копия из списка /payments — обычный текст. Пробуем оба.
            if post("editMessageCaption", e):
                continue
            text = {k: v for k, v in e.items() if k != "caption"}
            if not post("editMessageText", {**text, "text": e["caption"]}):
                post("editMessageReplyMarkup", {"chat_id": e["chat_id"], "message_id": e["message_id"],
                                                "reply_markup": {"inline_keyboard": []}})

    if api is not None:
        def via_api(method: str, payload: dict) -> bool:
            try:
                api(method, **payload)
                return True
            except Exception as exc:  # noqa: BLE001 — сообщение могли удалить; остальные всё равно правим
                log.info("чек #%s: %s: %s", payment_id, method, exc)
                return False
        one(via_api)
        return
    if not config.alert_telegram_token:
        return

    def via_http(method: str, payload: dict) -> bool:
        try:
            resp = httpx.post(f"https://api.telegram.org/bot{config.alert_telegram_token}/{method}",
                              json=payload, timeout=10)
            return bool((resp.json() or {}).get("ok"))
        except (httpx.HTTPError, ValueError) as exc:
            log.info("чек #%s: %s: %s", payment_id, method, exc)
            return False
    threading.Thread(target=one, args=(via_http,), name="donatix-settle", daemon=True).start()


def _e(value: Any) -> str:
    import html
    return html.escape(str(value if value is not None else ""))


# ── Доход кассира ──────────────────────────────────────────


def _received(conn: sqlite3.Connection, a: str, b: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT method, COALESCE(auto_kind, '') AS auto, COUNT(*) AS n, COALESCE(SUM(amount_micro), 0) AS usd "
        "FROM payments WHERE status = 'paid' AND resolved_at >= ? AND resolved_at < ? GROUP BY method, auto",
        (a, b)).fetchall()


def shares(conn: sqlite3.Connection, config: Config, a: str, b: str, profit: int,
           rows: list[sqlite3.Row] | None = None) -> list[dict[str, Any]]:
    """Доля каждого кассира за период [a, b) при чистой прибыли profit."""
    rows = _received(conn, a, b) if rows is None else rows
    total = sum(int(r["usd"]) for r in rows)
    out = []
    for c in listing(conn):
        codes = codes_of(c)
        mine = [r for r in rows if (not r["auto"] if codes is None else r["method"] in codes)]
        received = sum(int(r["usd"]) for r in mine)
        share = (profit * received * int(c["percent"]) // (total * 100)) if profit > 0 and total > 0 else 0
        out.append({"tg_id": c["tg_id"], "name": c["name"], "percent": int(c["percent"]),
                    "active": bool(c["active"]), "received": received,
                    "count": sum(int(r["n"]) for r in mine), "share": share})
    return out


def earnings(conn: sqlite3.Connection, config: Config, tg_id: int, start: datetime, end: datetime
             ) -> dict[str, Any]:
    from .finance import _iso, net_profit
    a, b = _iso(start), _iso(end)
    found = next((s for s in shares(conn, config, a, b, net_profit(conn, a, b)) if s["tg_id"] == tg_id), None)
    return found or {"received": 0, "count": 0, "share": 0, "percent": 0}


def periods(conn: sqlite3.Connection, now: datetime | None = None) -> list[tuple[str, datetime, datetime]]:
    from .finance import window
    today = window(conn, now)
    yesterday = window(conn, now, days_back=1)
    month_start = today[0].replace(day=1)
    return [("Сегодня", *today), ("Вчера", *yesterday), ("За месяц", month_start, today[1])]


def income_text(conn: sqlite3.Connection, config: Config, tg_id: int, now: datetime | None = None) -> str:
    lines = []
    for label, start, end in periods(conn, now):
        e = earnings(conn, config, tg_id, start, end)
        lines.append(f"{label}: через ваши банки ${fmt(e['received'])} ({e['count']} шт.) · "
                     f"<b>ваш доход ${fmt(e['share'])}</b>")
    return "\n".join(lines)


def daily_reports(conn: sqlite3.Connection, config: Config, start: datetime, end: datetime) -> None:
    """Каждому кассиру — его итог за прошедшие сутки."""
    if not config.alert_telegram_token:
        return
    from .finance import _iso, net_profit
    a, b = _iso(start), _iso(end)
    profit = net_profit(conn, a, b)
    month_start = start.replace(day=1)
    month = {s["tg_id"]: s for s in shares(conn, config, _iso(month_start), b,
                                               net_profit(conn, _iso(month_start), b))}
    for s in shares(conn, config, a, b, profit):
        if not s["active"]:
            continue
        m = month.get(s["tg_id"], {"share": 0})
        text = (f"💰 <b>Итог за {start:%d.%m.%Y}</b>\n"
                f"Через ваши банки: ${fmt(s['received'])} ({s['count']} пополн.)\n"
                f"Ваш доход ({s['percent']}%): <b>${fmt(s['share'])}</b>\n"
                f"С начала месяца: ${fmt(m['share'])}")
        try:
            httpx.post(f"https://api.telegram.org/bot{config.alert_telegram_token}/sendMessage",
                       json={"chat_id": s["tg_id"], "text": text, "parse_mode": "HTML"}, timeout=10)
        except httpx.HTTPError as exc:
            log.warning("отчёт кассиру %s: %s", s["tg_id"], exc)


def drop_messages(conn: sqlite3.Connection, config: Config, payment_id: int, api: Any = None) -> int:
    """Убрать прошлые сообщения с этой заявкой (у админа и кассиров) — ДО повторной отправки.

    Удаляем сразу (не в фоне), чтобы старое исчезло раньше, чем придёт новое. Telegram не даёт
    боту удалять сообщения старше 48 часов — такое сообщение правим: убираем кнопки и пишем,
    что заявка отправлена заново ниже. Так старых кнопок «Зачислить» не остаётся нигде."""
    rows = [dict(r) for r in conn.execute(
        "SELECT chat_id, message_id, caption FROM payment_msgs WHERE payment_id = ?", (payment_id,)).fetchall()]
    conn.execute("DELETE FROM payment_msgs WHERE payment_id = ?", (payment_id,))
    if not rows or not config.alert_telegram_token:
        return len(rows)

    def call(method: str, **payload: Any) -> bool:
        if api is not None:
            try:
                api(method, **payload)
                return True
            except Exception as exc:  # noqa: BLE001
                log.info("чек #%s: %s не вышел: %s", payment_id, method, exc)
                return False
        try:
            resp = httpx.post(f"https://api.telegram.org/bot{config.alert_telegram_token}/{method}",
                              json=payload, timeout=8)
            ok = bool((resp.json() or {}).get("ok"))
            if not ok:
                log.info("чек #%s: %s не вышел: %s", payment_id, method, resp.text[:200])
            return ok
        except (httpx.HTTPError, ValueError) as exc:
            log.info("чек #%s: %s не вышел: %s", payment_id, method, exc)
            return False

    removed = 0
    for r in rows:
        target = {"chat_id": r["chat_id"], "message_id": r["message_id"]}
        if call("deleteMessage", **target):
            removed += 1
            continue
        # Удалить нельзя (старше 48 ч) — хотя бы снять кнопки, чтобы не нажали на старую копию
        note = f"{r['caption']}\n\n<b>♻️ Заявка отправлена заново — смотрите ниже</b>"
        if not call("editMessageCaption", **target, caption=safe_caption(note), parse_mode="HTML"):
            call("editMessageReplyMarkup", **target, reply_markup={"inline_keyboard": []})
    return removed

