"""Пропуск в API: кто пришёл, не слишком ли часто и не подбирает ли ключ.

Три разные защиты, и путать их нельзя:

  1. Проверка ключа — кто это. Ключ сверяется с хешем, дальше смотрим,
     жив ли он и не забанен ли владелец.

  2. Ограничение частоты — сколько запросов в минуту можно одному ключу.
     Считаем «дырявым ведром»: без отдельного таймера и без списка
     отметок времени, одним числом на ключ.

  3. Защита от подбора — сколько раз подряд с одного адреса приходил
     неверный ключ. Считать по ключу тут нельзя: у подбирающего ключа
     нет, он его и ищет.

Счётчики живут в памяти: бот работает одним процессом, а лишняя запись
в базу на каждый запрос стоила бы дороже самой защиты. После перезапуска
счётчики обнуляются — это приемлемо: ключ от этого не становится
известен, а подбор начинается заново с нуля попыток.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app import db, runtime
from app.api import keys as apikeys

#: Сколько запросов в минуту разрешено одному ключу.
DEFAULT_RATE = 60

#: Всплеск: столько запросов подряд можно сделать не ожидая.
BURST = 20

#: Сколько неверных ключей с одного адреса терпим, прежде чем закрыть.
BAD_LIMIT = 10

#: На сколько закрываем адрес после этого, секунды.
BAD_BLOCK = 300.0


def rate_limit() -> int:
    """Запросов в минуту на ключ. Владелец меняет это в панели."""
    return max(1, runtime.get_int("api_rate_per_min", DEFAULT_RATE))


@dataclass
class Caller:
    """Кто сделал запрос.

    key пуст, когда пришли не ключом, а пропуском в кабинет. Тогда
    watch=True: смотреть можно всё своё, тратить деньги — нельзя.
    """
    key: db.ApiKey | None
    user: db.User
    watch: bool = False

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def key_id(self) -> int | None:
        return self.key.id if self.key else None


class Denied(Exception):
    """Запрос отклонён. code — машинное имя причины, status — код HTTP."""

    def __init__(self, status: int, code: str, message: str,
                 retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


# ─────────────────────────────────────────────── частота запросов


#: ключ -> (сколько «воды» в ведре, когда последний раз доливали)
_buckets: dict[int, tuple[float, float]] = {}


def take(key_id: int, cost: float = 1.0) -> float:
    """Занять место в ведре. 0 — можно, иначе сколько секунд ждать.

    Ведро течёт равномерно: за минуту вытекает rate_limit() единиц.
    Поэтому ровная нагрузка проходит всегда, а всплеск — пока в ведре
    есть запас.
    """
    now = time.monotonic()
    rate = rate_limit() / 60.0
    level, seen = _buckets.get(key_id, (0.0, now))
    level = max(0.0, level - (now - seen) * rate)

    if level + cost > BURST:
        _buckets[key_id] = (level, now)
        return round((level + cost - BURST) / rate, 3)

    _buckets[key_id] = (level + cost, now)
    return 0.0


def forget_rate(key_id: int = 0) -> None:
    if key_id:
        _buckets.pop(key_id, None)
    else:
        _buckets.clear()


# ─────────────────────────────────────────────── подбор ключа


#: адрес -> (сколько промахов, до какого времени закрыт)
_bad: dict[str, tuple[int, float]] = {}

#: Сколько адресов помним. Перебор идёт с тысяч адресов сразу, и без
#: предела этот словарь растёт ровно столько, сколько длится нападение —
#: то есть съедает память как раз тогда, когда она нужнее всего.
BAD_KEEP = 10_000


def _forget_stale(now: float) -> None:
    """Выбросить отсидевших и, если всё равно много, самых старых."""
    for ip, (_, until) in list(_bad.items()):
        if until and until < now:
            _bad.pop(ip, None)
    if len(_bad) <= BAD_KEEP:
        return
    for ip, _ in sorted(_bad.items(), key=lambda item: item[1][1])[:len(_bad) // 2]:
        _bad.pop(ip, None)


def blocked(ip: str) -> float:
    """Сколько секунд адресу ещё нельзя. 0 — можно."""
    misses, until = _bad.get(ip, (0, 0.0))
    left = until - time.monotonic()
    return round(left, 1) if left > 0 else 0.0


def note_bad_key(ip: str) -> None:
    if len(_bad) >= BAD_KEEP:
        _forget_stale(time.monotonic())
    misses, until = _bad.get(ip, (0, 0.0))
    misses += 1
    if misses >= BAD_LIMIT:
        _bad[ip] = (0, time.monotonic() + BAD_BLOCK)
    else:
        _bad[ip] = (misses, until)


def note_good_key(ip: str) -> None:
    _bad.pop(ip, None)


def forget_bad(ip: str = "") -> None:
    if ip:
        _bad.pop(ip, None)
    else:
        _bad.clear()


# ─────────────────────────────────────────────── сама проверка


def bearer(header: str) -> str:
    """Достать ключ из заголовка Authorization."""
    value = (header or "").strip()
    if not value:
        return ""
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


async def _by_pass(conn, token: str, ip: str) -> Caller:
    """Вход по пропуску из бота. Даёт только смотреть своё.

    Пропуск приходит ссылкой, а ссылка оседает в истории браузера и в
    пересланном сообщении. Поэтому у него не может быть права тратить
    деньги — на этом и держится вся его безопасность.

    В кеше храним сразу владельца, а не номер пропуска: страница делает
    по несколько запросов подряд, и второй заход в базу за тем же ответом
    не нужен. Отзыв кеш чистит, поэтому отозванный пропуск не живёт лишнюю
    минуту.
    """
    user_id = apikeys.cached_pass(token)
    if user_id is None:
        row = None
        for candidate in await db.api_passes_by_prefix(
            conn, apikeys.pass_prefix_of(token)
        ):
            if apikeys.verify(token, candidate["token_hash"]):
                row = candidate
                break
        if row is None:
            note_bad_key(ip)
            raise Denied(401, "invalid_key", "Ключ не принят.")
        user_id = row["user_id"]
        apikeys.remember_pass(token, user_id)
        await db.api_pass_used(conn, row["id"])

    note_good_key(ip)

    user = await db.get_user(conn, user_id)
    if user is None:
        raise Denied(403, "no_account", "Аккаунт не найден.")
    if user.is_banned:
        raise Denied(403, "account_blocked", "Доступ закрыт.")

    # Ведро своё: у пропуска нет номера ключа, а мешать их счётчики
    # значило бы, что открытая страница съедает лимит рабочего ключа.
    wait = take(-user.id)
    if wait:
        raise Denied(429, "rate_limited",
                     f"Слишком часто. Лимит — {rate_limit()} запросов в минуту.",
                     int(wait) + 1)
    return Caller(key=None, user=user, watch=True)


async def identify(conn, token: str, ip: str = "") -> Caller:
    """Кто это. Бросает Denied, если пропускать нельзя.

    Ответ на неверный ключ и на несуществующий одинаков нарочно: разница
    в тексте подсказала бы подбирающему, что префикс он угадал.
    """
    wait = blocked(ip)
    if wait:
        raise Denied(429, "too_many_attempts",
                     "Слишком много попыток с неверным ключом.", int(wait) + 1)

    if token and apikeys.looks_like_pass(token):
        return await _by_pass(conn, token, ip)

    if not token or not apikeys.looks_like(token):
        note_bad_key(ip)
        raise Denied(401, "invalid_key", "Ключ не принят.")

    key_id = apikeys.cached(token)
    key = await db.get_api_key(conn, key_id) if key_id else None
    if key is None:
        for candidate in await db.api_keys_by_prefix(conn, apikeys.prefix_of(token)):
            if apikeys.verify(token, candidate.key_hash):
                key = candidate
                break

    if key is None:
        note_bad_key(ip)
        raise Denied(401, "invalid_key", "Ключ не принят.")

    note_good_key(ip)
    apikeys.remember(token, key.id)

    if not key.live:
        raise Denied(403, "key_disabled", "Ключ выключен или отозван.")

    user = await db.get_user(conn, key.user_id)
    if user is None:
        raise Denied(403, "no_account", "Аккаунт владельца ключа не найден.")
    if user.is_banned:
        raise Denied(403, "account_blocked", "Доступ закрыт.")

    wait = take(key.id)
    if wait:
        raise Denied(429, "rate_limited",
                     f"Слишком часто. Лимит — {rate_limit()} запросов в минуту.",
                     int(wait) + 1)

    return Caller(key=key, user=user)
