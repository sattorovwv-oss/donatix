"""Пароли, API-ключи, подписи webhook."""

from __future__ import annotations

import hashlib
import hmac
import secrets

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

API_KEY_PREFIX = "dx_live_"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_api_key() -> tuple[str, str, str]:
    """Возвращает (ключ целиком — показать один раз, короткий префикс, хэш для базы)."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[: len(API_KEY_PREFIX) + 6], hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Подпись: HMAC-SHA256 от "<timestamp>.<тело>" ключом клиента, hex."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return mac.hexdigest()


# ── Хранение API-ключа с возможностью показать его клиенту ─────
# Шифр на стандартной библиотеке: HMAC-SHA256 как псевдослучайная функция в режиме
# счётчика (поток), затем HMAC всего шифротекста (encrypt-then-MAC). Ключи шифрования
# и подписи выводятся из DONATIX_SECRET_KEY; без него расшифровать ключи нельзя.


def _subkey(secret: str, label: bytes) -> bytes:
    return hmac.new(secret.encode(), b"donatix-apikey-" + label, hashlib.sha256).digest()


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def seal(secret: str, plaintext: str) -> str:
    data = plaintext.encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(_subkey(secret, b"enc"), nonce, len(data)), strict=True))
    tag = hmac.new(_subkey(secret, b"mac"), nonce + ct, hashlib.sha256).digest()
    return "v1$" + (nonce + ct + tag).hex()


def unseal(secret: str, sealed: str) -> str | None:
    """Расшифровать; None — если данные подменены или ключ сайта другой."""
    try:
        version, blob_hex = sealed.split("$", 1)
        blob = bytes.fromhex(blob_hex)
    except ValueError:
        return None
    if version != "v1" or len(blob) < 48:
        return None
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    expected = hmac.new(_subkey(secret, b"mac"), nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return None
    return bytes(a ^ b for a, b in zip(ct, _stream(_subkey(secret, b"enc"), nonce, len(ct)), strict=True)).decode()
