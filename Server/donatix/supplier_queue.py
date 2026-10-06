"""Очередь заказов, когда у поставщика кончились деньги.

Раньше заказ падал с возвратом («товар временно недоступен») — клиент уходил.
Теперь:
  1. Поставщик ответил «недостаточно средств» → заказ НЕ отменяется, остаётся
     «в обработке» и встаёт в очередь; у этого поставщика включается «пауза».
  2. Пока пауза, новые заказы к нему сразу встают в конец очереди — запросы на
     создание заказа поставщику не уходят вообще.
  3. Раз в минуту воркер смотрит баланс поставщика (это не заказ, денег не тратит).
     Хватает на первый заказ в очереди — пауза снимается.
  4. Очередь уходит по порядку, не больше 50 заказов в минуту. Деньги снова
     кончились посреди — пауза включается опять, остальные ждут.

Очередь у каждого поставщика своя (FazerCards и CoinDrop — разные балансы).
Метка в заказе: supplier_status = 'queued_funds:<поставщик>'.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from collections import deque
from typing import Any

from . import db
from .money import SCALE

log = logging.getLogger(__name__)

QUEUED = "queued_funds"
PER_MINUTE = 50           # столько заказов из очереди в минуту
CHECK_EVERY = 60          # баланс поставщика на паузе — раз в минуту
REMIND_EVERY = 3 * 3600   # напоминание админу, пока пауза

_sent: dict[str, deque] = {}          # поставщик → время отправок за последнюю минуту
_checked: dict[str, float] = {}       # поставщик → когда смотрели баланс


def provider_for(supplier: Any, product: dict[str, Any]) -> str:
    route = getattr(supplier, "_for_product", None)
    target = route(product) if route else supplier
    return str(getattr(target, "name", "") or "main")


def _supplier_named(supplier: Any, name: str) -> Any:
    extra = getattr(supplier, "get_extra", None)
    found = extra(name) if extra else None
    return found or getattr(supplier, "primary", supplier)


def mark(provider: str) -> str:
    return f"{QUEUED}:{provider}"


_NO_FUNDS = re.compile(r"insufficient[ _-]*(balance|funds)|not enough (balance|funds|money)|low balance"
                       r"|недостаточно (средств|денег)|не хватает (средств|денег)|баланс[а-я]* недостаточ")


def is_no_funds(exc: Exception) -> bool:
    """Поставщик отказал из-за нехватки денег у нас на счету.

    Только точный код или фраза: клиент не должен уметь «включить» паузу словом balance
    в своём логине/юзернейме или ошибкой вроде «лимит баланса кошелька Steam».
    """
    return getattr(exc, "code", "") == "insufficient_balance" or bool(_NO_FUNDS.search(str(exc).lower()))


def has_funds_for(supplier: Any, provider: str, order: Any) -> bool:
    """Отказ похож на «нет денег» только по тексту, а баланс на этот заказ есть — значит, отказ не про деньги."""
    try:
        balance = _supplier_named(supplier, provider).balance()
    except Exception:  # noqa: BLE001 — не узнали баланс: верим отказу, заказ ждёт в очереди
        return False
    return balance * SCALE >= order["cost_micro"] > 0


def on_hold(conn: sqlite3.Connection, provider: str) -> bool:
    return bool(db.get_setting(conn, f"supplier.hold.{provider}"))


def queued_count(conn: sqlite3.Connection, provider: str | None = None) -> int:
    if provider is None:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'processing' AND supplier_status LIKE ?",
                            (QUEUED + ":%",)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'processing' AND supplier_status = ?",
                        (mark(provider),)).fetchone()[0]


def should_queue(conn: sqlite3.Connection, provider: str) -> bool:
    """Пауза или в очереди уже кто-то есть — новый заказ встаёт в конец, чтобы не обгонять."""
    return on_hold(conn, provider) or queued_count(conn, provider) > 0


def enqueue(conn: sqlite3.Connection, order_id: int, provider: str) -> None:
    # Прошлая попытка точно отклонена — при отправке из очереди нужен новый ключ повтора,
    # иначе поставщик может вернуть тот же отказ по старому ключу.
    conn.execute(
        "UPDATE orders SET supplier_status = ?, supplier_idem_key = ?, supplier_attempts = 0, "
        "error = 'Ждёт пополнения баланса у поставщика', updated_at = ? WHERE id = ? AND status = 'processing'",
        (mark(provider), f"dx-{uuid.uuid4()}", db.now(), order_id))


def start_hold(conn: sqlite3.Connection, provider: str) -> None:
    if not on_hold(conn, provider):
        db.set_setting(conn, f"supplier.hold.{provider}", db.now())
        db.set_setting(conn, f"supplier.hold_notified.{provider}", "")
        log.warning("поставщик %s: не хватает денег — заказы в очередь", provider)


def _lift(conn: sqlite3.Connection, provider: str) -> None:
    db.set_setting(conn, f"supplier.hold.{provider}", "")
    db.set_setting(conn, f"supplier.hold_notified.{provider}", "")


def _providers(conn: sqlite3.Connection) -> set[str]:
    out = {r[0].split(":", 1)[1] for r in conn.execute(
        "SELECT DISTINCT supplier_status FROM orders WHERE status = 'processing' AND supplier_status LIKE ?",
        (QUEUED + ":%",))}
    out |= {r[0].split(".", 2)[2] for r in conn.execute(
        "SELECT key FROM settings WHERE key LIKE 'supplier.hold.%' AND value != ''")}
    return out


def _notify(config: Any, text: str) -> None:
    if config is None:
        return
    from .worker import notify_admin
    notify_admin(config, text)


def dispatch(conn: sqlite3.Connection, config: Any, supplier: Any, now: float | None = None) -> int:
    """Воркер: снять паузу, если денег хватает, и отправить из очереди до 50 заказов в минуту."""
    from . import orders
    now = time.monotonic() if now is None else now
    sent_total = 0
    for provider in sorted(_providers(conn)):
        waiting = queued_count(conn, provider)
        if on_hold(conn, provider):
            key = f"supplier.hold_notified.{provider}"
            last = db.get_setting(conn, key) or ""
            if not last or (last.replace(".", "").isdigit() and time.time() - float(last) > REMIND_EVERY):
                db.set_setting(conn, key, str(int(time.time())))
                _notify(config, f"⚠️ У поставщика {provider} не хватает денег. Заказы НЕ отменяются — "
                                f"стоят в очереди ({waiting} шт.), клиенты видят «в обработке». Пополните баланс "
                                f"у поставщика — как только денег хватит, отправлю сам, по {PER_MINUTE} в минуту.")
            if now - _checked.get(provider, -CHECK_EVERY) < CHECK_EVERY:
                continue
            _checked[provider] = now
            first = conn.execute("SELECT cost_micro FROM orders WHERE status = 'processing' AND supplier_status = ? "
                                 "ORDER BY id LIMIT 1", (mark(provider),)).fetchone()
            if first is not None:
                try:
                    balance = _supplier_named(supplier, provider).balance()
                except Exception as exc:  # noqa: BLE001 — не узнали баланс: спросим через минуту
                    log.info("очередь %s: баланс не получен: %s", provider, exc)
                    continue
                if balance * SCALE < first["cost_micro"]:
                    continue
            _lift(conn, provider)
            if waiting:
                _notify(config, f"✅ У поставщика {provider} появились деньги — отправляю очередь: {waiting} "
                                f"заказов, по {PER_MINUTE} в минуту.")
        sent = _sent.setdefault(provider, deque())
        while sent and now - sent[0] >= 60:
            sent.popleft()
        budget = PER_MINUTE - len(sent)
        if budget <= 0:
            continue
        rows = conn.execute("SELECT * FROM orders WHERE status = 'processing' AND supplier_status = ? "
                            "ORDER BY id LIMIT ?", (mark(provider), budget)).fetchall()
        for order in rows:
            conn.execute("UPDATE orders SET supplier_status = NULL, error = NULL WHERE id = ?", (order["id"],))
            sent.append(now)
            sent_total += 1
            try:
                orders._send_to_supplier(conn, supplier, orders.get_order_row(conn, order["id"]), from_queue=True)
            except Exception:
                log.exception("очередь: заказ %s", order["public_id"])
            if on_hold(conn, provider):   # деньги снова кончились — остальные ждут
                break
    return sent_total


def summary_line(conn: sqlite3.Connection) -> str:
    n = queued_count(conn)
    return f"\n⏳ В очереди (нет денег у поставщика): {n}" if n else ""
