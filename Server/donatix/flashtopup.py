"""FlashTopup — только проверка ID игрока: по ID находим ник (Free Fire, Mobile Legends, PUBG…).

Заказы через FlashTopup не идут — нужен лишь их /check-id, чтобы покупатель видел свой ник до оплаты.
Подпись каждого запроса — HMAC-SHA256 (api_key) над:
    METHOD \\n /api/reseller/v2/<путь> \\n timestamp \\n nonce \\n sha256(тело)
Ключи — только в .env на сервере: DONATIX_FLASHTOPUP_API_ID, DONATIX_FLASHTOPUP_API_KEY.
Наш сервер нужно добавить в «IP allowlist» в кабинете FlashTopup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://flashtopup.com"
PREFIX = "/api/reseller/v2"
TIMEOUT = 12
GAMES_TTL = 6 * 3600

_REGION_WORDS = re.compile(r"\b(global|indonesia|india|brazil|cis|снг|russia|turkey|ph|my|sg|th|vn|id|br|tr|ru|"
                           r"me|mena|latam|eu|us|asia|sea|top ?up|topup|direct|login|id)\b", re.I)


def norm(name: str) -> str:
    """«Free Fire (Indonesia)», «FREE FIRE - Global», «Free Fire» → «freefire»."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (name or "").lower())
    text = _REGION_WORDS.sub(" ", text)
    return re.sub(r"[^a-zа-я0-9]", "", text)


class FlashError(Exception):
    pass


class FlashTopup:
    def __init__(self, api_id: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.api_id, self.api_key = api_id, api_key
        self._client = httpx.Client(base_url=BASE, timeout=TIMEOUT, transport=transport)
        self._lock = threading.Lock()
        self._games: tuple[float, dict[str, dict[str, Any]]] | None = None

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        canonical = "\n".join([method, PREFIX + path, ts, nonce, hashlib.sha256(raw.encode()).hexdigest()])
        headers = {"X-FT-API-ID": self.api_id, "X-FT-Timestamp": ts, "X-FT-Nonce": nonce,
                   "X-FT-Signature": hmac.new(self.api_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._client.request(method, PREFIX + path, content=raw.encode() if raw else None,
                                        params=params, headers=headers)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FlashError(f"нет ответа: {exc}") from exc
        if not isinstance(data, dict):
            raise FlashError("странный ответ")
        if not data.get("success"):
            err = data.get("error") or {}
            raise FlashError(f"{err.get('code') or resp.status_code}: {err.get('message') or ''}".strip())
        return data

    # ── Игры, где работает проверка ID ──

    def games(self) -> dict[str, dict[str, Any]]:
        """{нормализованное название игры: {"validation_code", "name"}} — держим 6 часов."""
        with self._lock:
            if self._games and time.monotonic() - self._games[0] < GAMES_TTL:
                return self._games[1]
        out: dict[str, dict[str, Any]] = {}
        page = 1
        while page <= 20:
            data = self._request("GET", "/products", params={"page": page, "per_page": 500})
            items = _items(data.get("data"))
            for it in items:
                code = it.get("validation_code") or it.get("validationCode")
                name = it.get("name") or it.get("product_name") or it.get("title") or ""
                if code and name:
                    out.setdefault(norm(name), {"validation_code": str(code), "name": name})
            if len(items) < 500:
                break
            page += 1
        with self._lock:
            self._games = (time.monotonic(), out)
        log.info("FlashTopup: проверка ID есть у %s игр", len(out))
        return out

    def match(self, category_name: str) -> dict[str, Any] | None:
        try:
            games = self.games()
        except FlashError as exc:
            log.warning("FlashTopup: список игр не получен: %s", exc)
            with self._lock:   # не долбим их каждую секунду — попробуем через 10 минут
                self._games = (time.monotonic() - GAMES_TTL + 600, self._games[1] if self._games else {})
            return None
        return games.get(norm(category_name))

    def check_id(self, validation_code: str, user_id: str, server_id: str = "") -> dict[str, Any]:
        body = {"validation_code": validation_code, "user_id": user_id}
        if server_id:
            body["server_id"] = server_id
        try:
            data = self._request("POST", "/check-id", body)
        except FlashError as exc:
            text = str(exc)
            if text.startswith("INVALID_PLAYER_ID") or "VALIDATION_FAILED" in text:
                return {"valid": False, "player_name": None, "message": "Игрок с таким ID не найден."}
            raise
        d = data.get("data") or {}
        name = next((d.get(k) for k in ("account_name", "player_name", "nickname", "username", "name")
                     if isinstance(d, dict) and d.get(k)), None)
        valid = d.get("valid") if isinstance(d, dict) and "valid" in d else bool(name)
        return {"valid": bool(valid), "player_name": name, "message": "" if valid else "Игрок не найден."}


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("products", "items", "data", "list"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []
