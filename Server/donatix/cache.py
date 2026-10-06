"""Короткий кеш в памяти: главная и каталог не ходят в базу на каждый запрос.

Данные живут несколько секунд–минут; после загрузки каталога или правки товара
кеш сбрасывается сразу (clear), так что клиент не видит старых цен.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}
_key_locks: dict[str, threading.Lock] = {}


def _key_lock(key: str) -> threading.Lock:
    with _lock:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def get_or_set(key: str, ttl: float, make: Callable[[], Any]) -> Any:
    """Значение из кеша; считается одним потоком за раз.

    Без этого, когда запись устаревала, её пересчитывали сразу все одновременные
    запросы («лавина»): сайт замирал каждые несколько минут. Теперь: устарело —
    один запрос пересчитывает, остальные сразу получают прежнее значение; значения
    нет вовсе (после очистки) — остальные ждут один расчёт, а не делают свой.
    """
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
    if hit and hit[0] > now:
        return hit[1]
    lock = _key_lock(key)
    if hit is not None:
        if not lock.acquire(blocking=False):
            return hit[1]                      # уже пересчитывает другой запрос — отдаём прежнее
        try:
            return _compute(key, ttl, make)
        finally:
            lock.release()
    with lock:
        with _lock:
            hit = _store.get(key)
        if hit and hit[0] > time.monotonic():  # пока ждали — посчитал другой
            return hit[1]
        return _compute(key, ttl, make)


def _compute(key: str, ttl: float, make: Callable[[], Any]) -> Any:
    value = make()
    with _lock:
        _store[key] = (time.monotonic() + ttl, value)
    return value


def clear(prefix: str = "") -> None:
    with _lock:
        for key in [k for k in _store if k.startswith(prefix)]:
            del _store[key]


# ── Несколько процессов сайта ──────────────────────────────────
# Каталог обновили или админ поменял настройки в одном процессе — остальные узнают
# по отметке в базе и тоже сбрасывают свой кеш (проверка раз в несколько секунд).
_epoch = {"seen": None}


def clear_everywhere(conn: Any) -> None:
    from . import db
    clear()
    value = str(time.time_ns())
    db.set_setting(conn, "cache.epoch", value)
    _epoch["seen"] = value


def sync_epoch(conn: Any) -> None:
    from . import db
    value = db.get_setting(conn, "cache.epoch")
    if _epoch["seen"] is None:
        _epoch["seen"] = value
    elif value != _epoch["seen"]:
        _epoch["seen"] = value
        clear()
