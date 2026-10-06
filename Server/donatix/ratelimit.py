"""Ограничение частоты запросов к нашему API (в памяти процесса, скользящее окно)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

LIMITS = {
    "orders": 60,     # создание заказов в минуту
    "catalog": 120,
    "status": 120,
    "account": 60,
    "login": 10,      # попыток входа с одного IP за окно
}
WINDOWS = {"login": 15 * 60}


class RateLimiter:
    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = dict(LIMITS if limits is None else limits)
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, category: str, key: str) -> float | None:
        """None — можно; число — сколько секунд подождать."""
        limit = self.limits.get(category)
        if not limit:
            return None
        window = WINDOWS.get(category, 60)
        now = time.monotonic()
        with self._lock:
            q = self._hits[(category, key)]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, window - (now - q[0]))
            q.append(now)
        return None
