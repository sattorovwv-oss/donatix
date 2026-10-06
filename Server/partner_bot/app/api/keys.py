"""Ключи API: выдача, хранение и проверка.

Ключ показывается один раз — в момент создания. В базе лежит только
хеш: если базу украдут, чужими ключами воспользоваться не выйдет.

Почему scrypt, а не просто sha256. Ключ — секрет с высокой энтропией,
и подобрать его перебором нельзя в любом случае. Но хеш без «соли» и
без замедления превращает кражу базы в готовый радужный словарь, а
цена одной проверки у нас ничтожна: запросов немного, а ключ ещё и
кешируется на минуту. Поэтому берём медленный хеш — он ничего не
стоит нам и дорого стоит вору.

Видимое начало ключа (prefix) секретом не считается: по нему мы
находим кандидатов в базе, чтобы не сверять хеш со всей таблицей.
"""
from __future__ import annotations

import hmac
import secrets
import time
from hashlib import scrypt, sha256

#: С чего начинается ключ. По приставке разработчик сразу понимает, что
#: за строка попала ему в логи, и что её надо немедленно отозвать.
BRAND = "sk_live_"

#: Случайная часть. 32 знака base62 — примерно 190 бит: перебором не берётся.
BODY = 32
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

#: Сколько знаков ключа видно в кабинете после создания.
HEAD = len(BRAND) + 6
TAIL = 4

#: Параметры scrypt. n=2**14 — около 20 мс на проверку: человеку
#: незаметно, перебору мучительно.
_N, _R, _P = 2 ** 14, 8, 1

#: Проверенные ключи держим недолго в памяти: бот на одном процессе,
#: а гонять scrypt на каждый запрос подряд — терять те самые 20 мс.
_CACHE: dict[str, tuple[int, float]] = {}
CACHE_TTL = 60.0


def generate() -> str:
    """Новый ключ. Возвращается один раз — сохранить его наша забота не."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY))
    return BRAND + body


def prefix_of(key: str) -> str:
    return key[:HEAD]


def tail_of(key: str) -> str:
    return key[-TAIL:]


def hash_key(key: str, salt: bytes = b"") -> str:
    """Хеш ключа в виде scrypt$соль$хеш."""
    salt = salt or secrets.token_bytes(16)
    digest = scrypt(key.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify(key: str, stored: str) -> bool:
    """Тот ли это ключ. Сравнение постоянное по времени."""
    try:
        algo, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False

    got = scrypt(key.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return hmac.compare_digest(got, expected)


def looks_like(key: str) -> bool:
    """Похоже ли на наш ключ — до всякого обращения к базе."""
    if not key.startswith(BRAND) or len(key) != len(BRAND) + BODY:
        return False
    return all(ch in ALPHABET for ch in key[len(BRAND):])


# ─────────────────────────────────────────── пропуск в кабинет

#: С чего начинается пропуск. Приставка другая нарочно: по ней и человек,
#: и код сразу видят, что это не ключ и денег им не потратить.
PASS_BRAND = "cab_"
PASS_HEAD = len(PASS_BRAND) + 6

#: Проверенные пропуски держим в памяти так же, как ключи: страница
#: кабинета делает по несколько запросов подряд, и гонять scrypt на
#: каждый — терять по 20 мс на ровном месте.
_PASSES: dict[str, tuple[int, float]] = {}


def generate_pass() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY))
    return PASS_BRAND + body


def pass_prefix_of(token: str) -> str:
    return token[:PASS_HEAD]


def looks_like_pass(token: str) -> bool:
    if not token.startswith(PASS_BRAND) or len(token) != len(PASS_BRAND) + BODY:
        return False
    return all(ch in ALPHABET for ch in token[len(PASS_BRAND):])


def cached_pass(token: str) -> int | None:
    hit = _PASSES.get(_token(token))
    if hit is None:
        return None
    pass_id, until = hit
    if until < time.monotonic():
        _PASSES.pop(_token(token), None)
        return None
    return pass_id


def remember_pass(token: str, pass_id: int) -> None:
    _PASSES[_token(token)] = (pass_id, time.monotonic() + CACHE_TTL)


def forget_passes() -> None:
    """Забыть кеш пропусков — после отзыва. Пропусков у человека
    единицы, поэтому чистим всё разом: искать нужный дороже."""
    _PASSES.clear()


def _token(key: str) -> str:
    """Чем ключ представлен в кеше. Сам ключ в память не кладём."""
    return sha256(key.encode()).hexdigest()


def cached(key: str) -> int | None:
    """id ключа, если его недавно уже проверяли."""
    hit = _CACHE.get(_token(key))
    if hit is None:
        return None
    key_id, until = hit
    if until < time.monotonic():
        _CACHE.pop(_token(key), None)
        return None
    return key_id


def remember(key: str, key_id: int) -> None:
    _CACHE[_token(key)] = (key_id, time.monotonic() + CACHE_TTL)


def forget(key_id: int = 0) -> None:
    """Забыть кеш — после выключения или отзыва ключа.

    Без этого отозванный ключ работал бы ещё минуту, а отзыв делают
    как раз тогда, когда ключ утёк и каждая секунда на счету.
    """
    if not key_id:
        _CACHE.clear()
        return
    for token, (cached_id, _) in list(_CACHE.items()):
        if cached_id == key_id:
            _CACHE.pop(token, None)
