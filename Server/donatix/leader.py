"""Несколько процессов сайта на одном сервере.

Python в одном процессе использует одно ядро процессора. Чтобы сайт работал на всех
ядрах, запускается несколько процессов (DONATIX_WEB_WORKERS). Запросы посетителей
обслуживают все, а фоновую работу — заказы у поставщика, админ-бот, бот поддержки,
боты партнёров, загрузку каталога — делает ровно один «ведущий» процесс. Ведущий
определяется замком на файле: если он упал, замок освобождается сам и его берёт другой.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable

_held: dict[str, IO[Any]] = {}


def _path(config: Any, name: str) -> Path:
    folder = Path(config.db_path).parent
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.lock"


def try_lock(config: Any, name: str) -> bool:
    """Взять замок без ожидания. True — теперь он наш (и держится до конца процесса или release)."""
    if name in _held:
        return True
    f = open(_path(config, name), "a+")  # noqa: SIM115 — дескриптор держит замок
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _held[name] = f
    return True


def release(name: str) -> None:
    f = _held.pop(name, None)
    if f is not None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def become_leader(config: Any, start: Callable[[], None], stop_event: threading.Event, every: float = 15) -> bool:
    """Стать ведущим сейчас или позже (если ведущий процесс упадёт). True — стали сразу."""
    if try_lock(config, "leader"):
        start()
        return True

    def wait() -> None:
        while not stop_event.wait(every):
            if try_lock(config, "leader"):
                start()
                return

    threading.Thread(target=wait, name="donatix-leader-wait", daemon=True).start()
    return False


class FileLock:
    """Замок и между потоками одного процесса, и между процессами (для загрузки каталога)."""

    def __init__(self, name: str):
        self.name = name
        self._thread_lock = threading.Lock()
        self._config: Any = None

    def bind(self, config: Any) -> "FileLock":
        self._config = config
        return self

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        if not self._thread_lock.acquire(blocking, -1 if deadline is None else max(0.0, timeout)):
            return False
        if self._config is None:   # без настроек (тесты, консоль) — хватает замка потоков
            return True
        while True:
            if try_lock(self._config, self.name):
                return True
            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                self._thread_lock.release()
                return False
            time.sleep(0.5)

    def release(self) -> None:
        if self._config is not None:
            release(self.name)
        self._thread_lock.release()
