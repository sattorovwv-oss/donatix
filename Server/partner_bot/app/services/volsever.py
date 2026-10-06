"""Проверка игрового ID через Volsever.

Этот сервис устроен иначе, чем справочники ников Free Fire: он сделан
ровно для нашей задачи — «перед пополнением скажи, существует ли такой
аккаунт и как зовут игрока». Умеет много игр разом и принимает второе
поле (сервер/зона) там, где аккаунт задан парой чисел, — Magic Chess и
Mobile Legends как раз такие.

Ключ платный и лежит в настройках, а не в коде: репозиторий открытый.
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)

BASE = "https://gate.volsever.com/proxy/api"
#: Проверка ID: /game/<код игры>?id=...&zone=...
GAME_PATH = "/game/{game}"
#: Список игр и остаток лимита. Точные адреса в выжимке не названы,
#: поэтому пробуем вероятные и запоминаем сработавший — как уже сделано
#: для путей поставщика выдачи.
LIST_CANDIDATES = ["/list-game", "/game", "/games", "/list"]
USAGE_CANDIDATES = ["/get-usage", "/usage", "/account/usage"]

#: Проверка стоит перед оплатой, клиент ждёт ответа на экране.
TIMEOUT = aiohttp.ClientTimeout(total=10, connect=4)

#: Где в ответе лежит ник. Сервис отвечает {data: {username: ...}}, но
#: обёртки у таких сервисов меняются, а ломаться на этом нельзя.
NAME_KEYS = ("username", "nickname", "name", "user_name", "player_name")
ID_KEYS = ("user_id", "userId", "uid", "id", "account_id")


def _headers(key: str) -> dict:
    return {"X-API-KEY": key, "Accept": "application/json"}


def _dig(data, keys: tuple[str, ...], depth: int = 0):
    """Найти первое непустое значение по набору имён на любой глубине."""
    if not isinstance(data, dict) or depth > 4:
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    for value in data.values():
        if isinstance(value, dict):
            found = _dig(value, keys, depth + 1)
            if found:
                return found
    return None


def _refused(data: dict, status: int) -> bool:
    """Сервис прямо говорит «такого аккаунта нет»?

    Отличать это от своих поломок важно: на отказ по аккаунту мы
    предупреждаем клиента, а на поломку — молчим и продаём дальше.
    """
    if status in (404, 422):
        return True
    text = " ".join(
        str(data.get(key, "")) for key in ("message", "error", "status")
    ).lower()
    return any(mark in text for mark in
               ("not found", "invalid", "tidak ditemukan", "not exist"))


async def check(
    key: str, game: str, uid: str, zone: str = "",
) -> tuple[str | None, str, str]:
    """Ник игрока. Возвращает (ник, вердикт, пояснение).

    Вердикт: ok — аккаунт есть, bad — сервис сказал «нет такого»,
    unknown — проверить не удалось. Блокировать покупку можно только на
    bad, и то не молча: решает вызывающий.
    """
    if not key or not game:
        return None, "unknown", "не задан ключ или игра"

    url = f"{BASE}{GAME_PATH.format(game=game)}?id={uid}"
    if zone:
        url += f"&zone={zone}"

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(url, headers=_headers(key)) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 — мог прийти HTML
                    data = {}
                if not isinstance(data, dict):
                    data = {}
    except Exception as exc:  # noqa: BLE001 — проверка не обязана работать
        log.info("Volsever: %s не ответил — %s", game, exc)
        return None, "unknown", f"{type(exc).__name__}: {str(exc)[:60]}"

    if status == 401:
        return None, "unknown", "HTTP 401: ключ не принят"
    if status == 429:
        return None, "unknown", "HTTP 429: лимит исчерпан"
    if _refused(data, status):
        return None, "bad", _note(data, status)
    if status != 200:
        return None, "unknown", f"HTTP {status}"

    name = _dig(data, NAME_KEYS)
    if not name:
        return None, "unknown", _note(data, status)

    # Тот же принцип, что и у справочников ников: ответ обязан быть про
    # запрошенный аккаунт. Чужой ник хуже, чем никакого.
    found = _dig(data, ID_KEYS)
    if found and found != str(uid).strip():
        log.warning("Volsever: ответ про чужой ID (просили %s, пришёл %s)",
                    uid, found)
        return None, "unknown", f"ответ про чужой ID {found}"
    return name, "ok", ""


def _note(data: dict, status: int) -> str:
    for key in ("message", "error", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return f"ответил: {value.strip()[:70]}"
    return f"HTTP {status}" if status != 200 else "в ответе нет ника"


async def _probe(key: str, paths: list[str], setting: str) -> dict | None:
    """Пройтись по вероятным адресам и запомнить сработавший."""
    from app import runtime

    known = runtime.get(setting)
    order = ([known] if known else []) + [p for p in paths if p != known]

    for path in order:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.get(BASE + path,
                                       headers=_headers(key)) as response:
                    if response.status != 200:
                        continue
                    data = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 — перебор, а не работа
            log.debug("Volsever: %s — %s", path, exc)
            continue

        if isinstance(data, (dict, list)) and data:
            if path != known:
                await _remember(setting, path)
            return data if isinstance(data, dict) else {"data": data}
    return None


async def _remember(setting: str, value: str) -> None:
    from app import db, runtime

    try:
        conn = await db.connect()
        try:
            await runtime.set_value(conn, setting, value)
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001 — не сохранили, не беда
        log.warning("Volsever: адрес %s не запомнился — %s", setting, exc)


async def games(key: str) -> list[dict]:
    """Какие игры сервис умеет проверять: код, название, нужен ли сервер."""
    data = await _probe(key, LIST_CANDIDATES, "volsever_list_path")
    if not data:
        return []

    raw = None
    for holder in ("data", "games", "items", "result", "list"):
        value = data.get(holder)
        if isinstance(value, list):
            raw = value
            break
    if raw is None:
        return []

    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"code": item, "name": item, "zone": False})
            continue
        if not isinstance(item, dict):
            continue
        code = _dig(item, ("code", "slug", "game", "key", "id"))
        if not code:
            continue
        out.append({
            "code": code,
            "name": _dig(item, ("name", "title", "label")) or code,
            # Признак второго поля называют по-разному; сам факт важнее.
            "zone": bool(item.get("zone") or item.get("need_zone")
                         or item.get("server") or item.get("serverId")),
        })
    return out


async def usage(key: str) -> dict | None:
    """Остаток лимита. None — узнать не удалось."""
    return await _probe(key, USAGE_CANDIDATES, "volsever_usage_path")
