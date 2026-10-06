"""Шаги диалогов на диске, а не в памяти.

Бот перезапускается при каждом обновлении сайта, и память о шаге («ждём
имя владельца карты») пропадала: человек отправлял ответ в пустоту, а
кнопка «Сохранить» отвечала ошибкой. Здесь шаг и данные шага лежат в
отдельном маленьком файле SQLite и переживают перезапуск.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

#: Шаг, брошенный на сутки, забываем — иначе через неделю бот «вспомнит» старый вопрос
TTL_SECONDS = 24 * 3600


def _key(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.business_connection_id or ''}:" \
           f"{key.destiny}"


class SqliteStorage(BaseStorage):
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS fsm (k TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL "
                         "DEFAULT '{}', ts REAL NOT NULL)")
        self._db.execute("DELETE FROM fsm WHERE ts < ?", (time.time() - TTL_SECONDS,))
        self._lock = threading.Lock()
        # Пока бот работает — те же объекты, что и раньше (память); диск — чтобы пережить перезапуск
        self._mem: dict[str, tuple[str | None, dict[str, Any]]] = {}

    def _row(self, k: str) -> tuple[str | None, dict[str, Any]]:
        if k in self._mem:
            return self._mem[k]
        with self._lock:
            row = self._db.execute("SELECT state, data, ts FROM fsm WHERE k = ?", (k,)).fetchone()
        if row is None or row[2] < time.time() - TTL_SECONDS:
            return None, {}
        try:
            data = json.loads(row[1] or "{}")
        except ValueError:
            data = {}
        return row[0], data if isinstance(data, dict) else {}

    def _write(self, k: str, state: str | None, data: dict[str, Any]) -> None:
        if state is None and not data:
            self._mem.pop(k, None)
        else:
            self._mem[k] = (state, data)
        with self._lock:
            if state is None and not data:
                self._db.execute("DELETE FROM fsm WHERE k = ?", (k,))
            else:
                self._db.execute("INSERT INTO fsm (k, state, data, ts) VALUES (?, ?, ?, ?) "
                                 "ON CONFLICT(k) DO UPDATE SET state = excluded.state, data = excluded.data, "
                                 "ts = excluded.ts", (k, state, json.dumps(data, ensure_ascii=False, default=str),
                                                      time.time()))

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        k = _key(key)
        _, data = self._row(k)
        self._write(k, value, data)

    async def get_state(self, key: StorageKey) -> str | None:
        return self._row(_key(key))[0]

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        k = _key(key)
        state, _ = self._row(k)
        self._write(k, state, dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._row(_key(key))[1])

    async def close(self) -> None:
        with self._lock:
            self._db.close()
