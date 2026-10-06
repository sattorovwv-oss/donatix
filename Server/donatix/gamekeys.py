"""Ключи игр: где ключ активируется. Ответ поставщика держим час в памяти."""

from __future__ import annotations

import threading
import time
from typing import Any

from .suppliers import Supplier, SupplierError

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def regions(supplier: Supplier, game_id: str) -> dict[str, Any]:
    with _lock:
        hit = _cache.get(game_id)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
    try:
        data = supplier.gamekey_regions(game_id)
    except (SupplierError, AttributeError):
        return {"available": [], "unavailable": [], "region_type": "", "known": False}
    data = {**data, "known": True}
    with _lock:
        _cache[game_id] = (time.monotonic(), data)
    return data
