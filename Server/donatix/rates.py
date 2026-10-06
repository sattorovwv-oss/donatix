"""Курс доллара к сомони — сам, из открытых источников, без ключей.

Каждые 5 минут его обновляет воркер; пока клиент на странице оплаты или
создаёт заявку — не старше 30 секунд. К рыночному курсу прибавляется запас
(админка → Реквизиты), чтобы разница с курсом банков не уходила в минус.
Источник не ответил или прислал странное число — остаётся последний курс.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from . import db
from .config import Config

log = logging.getLogger(__name__)

WORKER_SECONDS = 300   # фоновое обновление
PAYMENT_SECONDS = 30   # на странице оплаты и при создании заявки
MIN_RATE, MAX_RATE = Decimal("5"), Decimal("40")
MAX_JUMP = Decimal("0.15")  # скачок больше 15% за раз — ошибка источника, не верим

_lock = threading.Lock()
_last_try = 0.0


def _er_api(data: dict[str, Any]) -> Any:
    return data["rates"]["TJS"] if data.get("result") == "success" else None


def _currency_api(data: dict[str, Any]) -> Any:
    return data["usd"]["tjs"]


#: (название, адрес, как достать курс). Первый ответивший — и берём.
SOURCES: list[tuple[str, str, Callable[[dict[str, Any]], Any]]] = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", _er_api),
    ("currency-api", "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
     _currency_api),
    ("currency-api (зеркало)", "https://latest.currency-api.pages.dev/v1/currencies/usd.json", _currency_api),
]


def _get_json(url: str) -> dict[str, Any]:
    r = httpx.get(url, timeout=6, follow_redirects=True, headers={"User-Agent": "Donatix/1"})
    r.raise_for_status()
    return r.json()


def auto_enabled(conn, config: Config) -> bool:
    value = db.get_setting(conn, "pay.rate_auto")
    return config.rate_auto if value is None else value == "1"


def margin_pct(conn, config: Config) -> Decimal:
    try:
        return Decimal(db.get_setting(conn, "pay.rate_margin_pct") or config.rate_margin_pct)
    except InvalidOperation:
        return config.rate_margin_pct


def apply_margin(market: Decimal, margin: Decimal) -> Decimal:
    return (market * (1 + margin / 100)).quantize(Decimal("0.01"), rounding=ROUND_UP)


def fetch_market(previous: Decimal | None = None) -> tuple[Decimal, str]:
    """Рыночный курс: первый источник, приславший правдоподобное число."""
    errors = []
    for name, url, pick in SOURCES:
        try:
            rate = Decimal(str(pick(_get_json(url))))
        except Exception as exc:  # noqa: BLE001 — любой сбой источника: пробуем следующий
            errors.append(f"{name}: {exc.__class__.__name__}")
            continue
        if not MIN_RATE <= rate <= MAX_RATE:
            errors.append(f"{name}: странный курс {rate}")
            continue
        if previous and abs(rate - previous) / previous > MAX_JUMP:
            errors.append(f"{name}: скачок {previous} → {rate}")
            continue
        return rate.quantize(Decimal("0.0001")), name
    raise RuntimeError("; ".join(errors) or "нет источников")


def refresh(conn, config: Config, max_age: float = WORKER_SECONDS, *, force: bool = False) -> bool:
    """Обновить курс, если он старше max_age секунд. True — курс поменялся в базе."""
    global _last_try
    if not auto_enabled(conn, config):
        return False
    now = time.time()
    updated = float(db.get_setting(conn, "pay.rate_ts") or 0)
    if not force and (now - updated < max_age or now - _last_try < min(max_age, 30)):
        return False
    if not _lock.acquire(blocking=False):
        return False  # уже обновляет другой запрос
    try:
        _last_try = now
        prev = db.get_setting(conn, "pay.rate_market")
        try:
            market, source = fetch_market(Decimal(prev) if prev else None)
        except RuntimeError as exc:
            log.warning("курс не обновлён: %s", exc)
            db.set_setting(conn, "pay.rate_error", str(exc)[:300])
            return False
        rate = apply_margin(market, margin_pct(conn, config))
        with db.tx(conn):
            db.set_setting(conn, "pay.rate_market", str(market))
            db.set_setting(conn, "pay.tjs_rate", str(rate))
            db.set_setting(conn, "pay.rate_source", source)
            db.set_setting(conn, "pay.rate_ts", str(now))
            db.set_setting(conn, "pay.rate_error", "")
        return True
    finally:
        _lock.release()


def status(conn, config: Config) -> dict[str, Any]:
    ts = float(db.get_setting(conn, "pay.rate_ts") or 0)
    return {
        "auto": auto_enabled(conn, config),
        "margin_pct": margin_pct(conn, config),
        "market": db.get_setting(conn, "pay.rate_market") or "",
        "source": db.get_setting(conn, "pay.rate_source") or "",
        "age_seconds": int(time.time() - ts) if ts else None,
        "error": db.get_setting(conn, "pay.rate_error") or "",
    }


def reset() -> None:
    """Для тестов."""
    global _last_try
    _last_try = 0.0
