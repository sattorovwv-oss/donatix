"""Темп обращений к поставщику: не больше N в минуту, остальные ждут.

У поставщика стоит ограничение: столько-то запросов в минуту на ключ.
Превысить его — значит получить отказы на ровном месте, причём на
заказах, за которые клиент уже заплатил.

Сто человек, нажавших «купить» в одну секунду, дают сто обращений
разом. Отказывать им нельзя, и слать все сразу тоже нельзя — поэтому
лишние ждут своей очереди и уходят в следующую минуту.

Окно скользящее, а не календарная минута. С календарной минутой сто
запросов в 11:59:59 и ещё сто в 12:00:01 формально укладываются в
лимит, а на деле бьют поставщика двойной пачкой — и он отвечает
отказом, потому что считает по-своему.

Заказы идут вперёд фоновых опросов: клиент не должен ждать, пока
пройдут проверки статусов чужих заказов. Фоновые подождут — им спешить
некуда.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger(__name__)

#: Длина окна. Лимит поставщика задан на минуту.
WINDOW = 60.0

#: Сколько ждать очереди, прежде чем сдаться. Больше двух минут ждать
#: бессмысленно: клиент решит, что бот завис, и нажмёт ещё раз.
MAX_WAIT = 120.0


class Busy(Exception):
    """Очередь к поставщику не рассосалась за отведённое время."""


class Rate:
    """Скользящее окно с приоритетом для заказов.

    clock и nap подменяются в проверках — иначе каждая из них шла бы
    реальную минуту.
    """

    def __init__(self, per_minute: int, *, clock=time.monotonic, nap=asyncio.sleep):
        self.limit = max(1, int(per_minute))
        self._clock = clock
        self._nap = nap
        self._done: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._urgent_waiting = 0

    def _forget_old(self, now: float) -> None:
        edge = now - WINDOW
        while self._done and self._done[0] <= edge:
            self._done.popleft()

    async def take(self, *, urgent: bool = False) -> float:
        """Дождаться своей очереди. Возвращает, сколько пришлось ждать."""
        started = self._clock()
        if urgent:
            self._urgent_waiting += 1
        try:
            while True:
                async with self._lock:
                    now = self._clock()
                    self._forget_old(now)
                    free = len(self._done) < self.limit
                    # Фоновый запрос не занимает место, пока в очереди
                    # стоит хоть один заказ: клиент важнее опроса статуса.
                    mine = urgent or self._urgent_waiting == 0
                    if free and mine:
                        self._done.append(now)
                        return now - started

                    if self._done and not free:
                        pause = self._done[0] + WINDOW - now
                    else:
                        pause = 0.05          # ждём, пока пройдут заказы
                    pause = max(0.01, min(pause, 1.0))

                if self._clock() - started + pause > MAX_WAIT:
                    raise Busy(
                        f"Очередь к поставщику не разошлась за "
                        f"{int(MAX_WAIT)} секунд."
                    )
                await self._nap(pause)
        finally:
            if urgent:
                self._urgent_waiting -= 1

    @property
    def used(self) -> int:
        """Сколько запросов потрачено в текущем окне."""
        self._forget_old(self._clock())
        return len(self._done)


#: Ограничитель на каждый ключ: лимит считается по счёту поставщика, а
#: счетов у нас два — основной и игровой.
_by_key: dict[str, Rate] = {}


def limit_now() -> int:
    from app import runtime
    from app.config import settings

    return runtime.get_int("supplier_rate_per_min",
                           settings.supplier_rate_per_min)


def for_key(api_key: str) -> Rate:
    """Ограничитель этого ключа. Лимит из настроек, меняется на ходу."""
    rate = _by_key.get(api_key)
    want = limit_now()
    if rate is None:
        rate = _by_key[api_key] = Rate(want)
        log.info("Темп к поставщику: не больше %s запросов в минуту", want)
    elif rate.limit != want:
        rate.limit = max(1, want)
        log.info("Темп к поставщику изменён: %s запросов в минуту", rate.limit)
    return rate


def forget() -> None:
    """Забыть ограничители — нужно после смены ключей."""
    _by_key.clear()
