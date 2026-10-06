"""Очередь запросов к поставщику: не больше N в минуту (по умолчанию 50).

У FazerCards один ключ на весь проект — на сайт, API и всех ботов партнёров.
Превысили его лимит — поставщик отвечает 429, и заказы сыплются ошибками.
Здесь все запросы встают в общую очередь и уходят по одному, не чаще лимита:
лишние ждут своей секунды, а не получают отказ.

Покупки и проверка ID важнее фона: загрузке каталога, проверке баланса и
опросу статусов оставляем не больше (N − запас) запросов в минуту, так что
у клиента всегда есть окно, даже пока грузится каталог.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque

WINDOW = 60.0


class QueueTimeout(RuntimeError):
    pass


class Throttle:
    def __init__(self, per_minute: int = 50, reserve: int = 10, window: float = WINDOW):
        self.window = window
        self._cond = threading.Condition()
        self._sent: deque[float] = deque()
        self._tickets = itertools.count()
        self._queue: deque[tuple[int, bool]] = deque()   # (номер, фоновый?) — по порядку прихода
        self.configure(per_minute, reserve)

    def configure(self, per_minute: int, reserve: int | None = None) -> None:
        with self._cond:
            self.per_minute = max(1, int(per_minute))
            if reserve is not None:
                self.reserve = max(0, int(reserve))
            self.reserve = min(self.reserve, self.per_minute - 1)
            self._cond.notify_all()

    def _trim(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.window:
            self._sent.popleft()

    def _limit(self, background: bool) -> int:
        return self.per_minute - self.reserve if background else self.per_minute

    def _my_turn(self, ticket: int, background: bool) -> bool:
        """Покупки и проверки — первыми, по порядку прихода; фон — когда их нет."""
        if background and any(not bg for _, bg in self._queue):
            return False
        for t, bg in self._queue:
            if t == ticket:
                return True
            if bg == background:
                return False  # такой же запрос пришёл раньше — он первый
        return True

    def acquire(self, *, background: bool = False, max_wait: float = 25.0) -> float:
        """Дождаться своей очереди. Возвращает, сколько секунд ждали."""
        start = time.monotonic()
        with self._cond:
            ticket = next(self._tickets)
            self._queue.append((ticket, background))
            try:
                while True:
                    now = time.monotonic()
                    self._trim(now)
                    if self._my_turn(ticket, background) and len(self._sent) < self._limit(background):
                        self._sent.append(now)
                        return now - start
                    waited = now - start
                    if waited >= max_wait:
                        raise QueueTimeout("Поставщик сейчас занят — много запросов. Повторите через минуту.")
                    until_free = (self._sent[0] + self.window - now) if self._sent else 0.05
                    self._cond.wait(min(max(until_free, 0.01), max_wait - waited, 1.0))
            finally:
                self._queue.remove(next(q for q in self._queue if q[0] == ticket))
                self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._sent.clear()
            self._cond.notify_all()

    def status(self) -> dict[str, int]:
        with self._cond:
            self._trim(time.monotonic())
            return {"per_minute": self.per_minute, "last_minute": len(self._sent), "waiting": len(self._queue)}


#: Один на весь процесс: сайт, API, воркер и загрузка каталога делят один ключ поставщика
SUPPLIER = Throttle()
