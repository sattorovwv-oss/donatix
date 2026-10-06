"""Ники Free Fire по числовому ID — чтобы клиент увидел, кому платит.

Бесплатный план gameskinbo даёт 100 запросов В МЕСЯЦ и 5 в минуту. Это
очень мало, поэтому удачные ответы держим в памяти полчаса: один и тот же
человек обычно вводит свой ID несколько раз подряд, и каждый такой ввод
не должен съедать лимит.

Неудачи не кэшируем: сервис мог просто не ответить, и через минуту тот же
ID вернёт ник. Запомнить «ника нет» значило бы испортить проверку до
перезапуска бота.

Ник — не пропуск к покупке. Пополнение идёт по ID, ник нужен только для
сверки глазами: не получили — показываем «ID принят» и даём купить.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

GAMESKINBO = "https://api.gameskinbo.com/ff-info/get"
GAMESKINBO_USAGE = "https://api.gameskinbo.com/api/usage"
#: Второй источник. Лимит у gameskinbo — 100 запросов в месяц, этого
#: мало, поэтому спрашиваем и его: два независимых источника дают ник
#: чаще, чем один.
COMMUNITY = "https://developers.freefirecommunity.com/api/v1/info"
#: Первый по очереди источник: ни ключа, ни региона — только ID игрока,
#: а регион он возвращает сам. Ради него и стоит спрашивать его первым:
#: пока он отвечает, месячный лимит gameskinbo вообще не тратится.
GLOBINFO = "https://glob-info2.vercel.app/info"
#: Запасной источник без ключа. Живёт на бесплатном хостинге и засыпает,
#: поэтому спрашиваем его последним.
FALLBACK = "https://free-ff-api-src-5plp.onrender.com/api/v1/account"

#: Где в ответе лежит ник. Сервисы отвечают по-разному, а обёртка вокруг
#: одного и того же поля меняется от версии к версии, поэтому ищем по
#: набору известных имён, а не по одному пути.
NAME_KEYS = (
    "AccountName", "accountName", "nickname", "nickName", "name",
    "player_name", "playerName", "username", "user_name",
)
#: Ветки, внутрь которых стоит заглянуть.
NEST_KEYS = (
    "AccountInfo", "accountInfo", "basicInfo", "basic_info",
    "data", "result", "player", "account", "profile", "info",
)
#: Где в ответе лежит ID аккаунта. По нему проверяем, что сервис ответил
#: про того самого игрока, а не про кого-то ещё.
ID_KEYS = (
    "accountId", "account_id", "AccountId", "accountID", "uid", "UID",
    "player_id", "playerId", "id",
)

TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)
FALLBACK_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
CACHE_TTL = 30 * 60          # полчаса

#: Регионы, которые понимает gameskinbo. Чужой код слать нельзя: сервис
#: либо ответит отказом, либо будет искать не там. Регион вообще
#: необязателен — он только ускоряет поиск, ID в Free Fire уникален
#: глобально, поэтому незнакомый код просто выбрасываем.
KNOWN_REGIONS = {"BD", "IND", "BR", "US", "SAC", "NA", "ID", "SG", "PK"}
#: А этот источник знает и СНГ. Список у каждого свой: общий на всех
#: выбрасывал бы RU и CIS, которые здесь как раз работают.
FALLBACK_REGIONS = {"IND", "BR", "SG", "RU", "ID", "TW", "US", "VN", "TH",
                    "ME", "PK", "CIS", "BD"}


def known_region(region: str, allowed: set[str] | None = None) -> str:
    code = (region or "").strip().upper()
    return code if code in (allowed or KNOWN_REGIONS) else ""

_cache: dict[str, tuple[float, str]] = {}


@dataclass
class Nickname:
    uid: str
    name: str | None
    verdict: str       # ok | bad | unknown
    source: str = ""


def cached(uid: str) -> str | None:
    found = _cache.get(uid)
    if not found:
        return None
    stored_at, name = found
    if time.time() - stored_at > CACHE_TTL:
        _cache.pop(uid, None)
        return None
    return name


def remember(uid: str, name: str) -> None:
    _cache[uid] = (time.time(), name)


def forget_all() -> None:
    _cache.clear()


def pick_name(data, depth: int = 0) -> str | None:
    """Достать ник из ответа, как бы сервис его ни завернул.

    Разбирать каждый источник отдельно — значит ломаться на каждой смене
    формата. Ник узнаётся по имени поля, а не по месту.
    """
    if not isinstance(data, dict) or depth > 4:
        return None

    for key in NAME_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in NEST_KEYS:
        found = pick_name(data.get(key), depth + 1)
        if found:
            return found

    # Ответ мог прийти списком найденных аккаунтов.
    for value in data.values():
        if isinstance(value, list) and value:
            found = pick_name(value[0], depth + 1)
            if found:
                return found
    return None


def pick_id(data, depth: int = 0) -> str | None:
    """Достать ID аккаунта из ответа — тем же способом, что и ник."""
    if not isinstance(data, dict) or depth > 4:
        return None

    for key in ID_KEYS:
        value = data.get(key)
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text.isdigit():
                return text

    for key in NEST_KEYS:
        found = pick_id(data.get(key), depth + 1)
        if found:
            return found
    return None


def trusted_name(data, uid: str) -> str | None:
    """Ник из ответа — но только если ответ точно про этого игрока.

    Главная защита от чужого ника. Сервисы бесплатные и живут на чужих
    хостингах: любой из них может отдать закэшированный чужой профиль,
    перепутать параметры или подставить «похожего» игрока. Показать
    клиенту чужой ник хуже, чем не показать никакого: он подтвердит
    покупку, а алмазы уйдут не туда, и вернуть их будет нельзя.

    Поэтому: нашли в ответе ID — он обязан совпасть с запрошенным.
    Не нашли ID вовсе — ник берём (проверить нечем, но и подмены не
    видно), это случай gameskinbo.
    """
    name = pick_name(data)
    if not name:
        return None

    found = pick_id(data, 0)
    if found and found != str(uid).strip():
        log.warning("Ники: ответ про чужой ID (просили %s, пришёл %s) — "
                    "ник отброшен", uid, found)
        return None
    return name


def _why_empty(data, uid: str) -> str:
    """Ответ пришёл, а ника нет. Сказать, что именно в нём было.

    Без этого «не ответил» покрывает три разные беды: сервис недоступен,
    сервис ответил «нет такого игрока» и сервис ответил про кого-то
    другого. Лечатся они по-разному, поэтому и различать их надо.
    """
    if not isinstance(data, dict):
        return "ответ не похож на JSON"
    if not data:
        return "ответ пустой"

    found = pick_id(data)
    if found and found != str(uid).strip():
        return f"ответ про чужой ID {found}"
    for key in ("error", "message", "msg", "detail", "status"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return f"ответил: {value.strip()[:60]}"
    return "в ответе нет ника"


async def _from_glob(
    session: aiohttp.ClientSession, uid: str, region: str, key: str,
) -> tuple[str | None, str, str]:
    """Ни ключа, ни региона — только ID игрока."""
    url = f"{GLOBINFO}?uid={uid}"
    async with session.get(url) as response:
        if response.status != 200:
            log.info("Ники: glob-info ответил %s", response.status)
            return None, "unknown", f"HTTP {response.status}"
        data = await response.json(content_type=None)

    if isinstance(data, dict) and data.get("error"):
        return None, "unknown", f"ответил: {str(data.get('error'))[:60]}"
    name = trusted_name(data, uid)
    if name:
        return name, "ok", ""
    return None, "unknown", _why_empty(data, uid)


async def _from_community(
    session: aiohttp.ClientSession, uid: str, region: str, key: str,
) -> tuple[str | None, str]:
    """Второй справочник: developers.freefirecommunity.com."""
    region = known_region(region)
    url = f"{COMMUNITY}?uid={uid}" + (f"&region={region}" if region else "")
    headers = {"Accept": "application/json"}
    if key:
        headers["x-api-key"] = key
        headers["Authorization"] = f"Bearer {key}"

    async with session.get(url, headers=headers) as response:
        if response.status in (400, 402):
            # Прямой отказ по игроку. Покупку он всё равно не запирает —
            # это решает вызывающий, — но и ник придумывать не из чего.
            return None, "bad", f"HTTP {response.status}"
        if response.status == 404:
            # Двусмысленно: так отвечают и на «нет игрока», и на «нет
            # такого адреса». Принять второе за первое — значит пугать
            # клиента из-за переехавшего сервиса.
            log.info("Ники: freefirecommunity вернул 404 по %s", uid)
            return None, "unknown", "HTTP 404"
        if response.status != 200:
            log.info("Ники: freefirecommunity ответил %s", response.status)
            return None, "unknown", f"HTTP {response.status}"
        data = await response.json(content_type=None)

    name = trusted_name(data, uid)
    if name:
        return name, "ok", ""
    return None, "unknown", _why_empty(data, uid)


async def _from_gameskinbo(
    session: aiohttp.ClientSession, uid: str, region: str, key: str,
) -> tuple[str | None, str, str]:
    region = known_region(region)
    url = f"{GAMESKINBO}?uid={uid}" + (f"&region={region}" if region else "")
    async with session.get(url, headers={"x-api-key": key}) as response:
        if response.status == 402:
            # Сервис прямо говорит: ID неверный.
            return None, "bad", "HTTP 402"
        if response.status == 401:
            return None, "unknown", "HTTP 401: ключ не принят"
        if response.status == 429:
            return None, "unknown", "HTTP 429: лимит исчерпан"
        if response.status != 200:
            return None, "unknown", f"HTTP {response.status}"
        data = await response.json(content_type=None)

    name = trusted_name(data, uid)
    if name:
        return name, "ok", ""
    return None, "unknown", _why_empty(data, uid)


async def _from_fallback(uid: str, region: str) -> tuple[str | None, str, str]:
    # У этого источника свой список регионов — в нём есть и СНГ.
    url = (f"{FALLBACK}?region="
           f"{known_region(region, FALLBACK_REGIONS) or 'BR'}&uid={uid}")
    try:
        async with aiohttp.ClientSession(timeout=FALLBACK_TIMEOUT) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None, "unknown", f"HTTP {response.status}"
                data = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 — запасной источник ненадёжен
        log.info("Ники: запасной источник молчит — %s", exc)
        return None, "unknown", f"{type(exc).__name__}: {str(exc)[:60]}"

    name = trusted_name(data, uid)
    if name:
        return name, "ok", ""
    return None, "unknown", _why_empty(data, uid)


async def free_fire(
    uid: str, key: str = "", region: str = "", community_key: str = "",
) -> Nickname:
    """Ник по ID. Регион только ускоряет поиск — ID в Free Fire уникален
    глобально, поэтому регион аккаунта с регионом товара не сверяем.

    Источников четыре, и спрашиваются они по очереди. Первый не просит
    ни ключа, ни региона — пока он отвечает, месячный лимит второго
    (сто запросов) не тратится вовсе. Последний живёт на бесплатном
    хостинге и часто спит. Первый же найденный ник обрывает очередь.

    Ник берётся только из ответа, который точно про запрошенный ID —
    см. trusted_name. Чужой ник хуже, чем никакого.
    """
    uid = str(uid).strip()
    hit = cached(uid)
    if hit:
        return Nickname(uid=uid, name=hit, verdict="ok", source="кэш")

    refused = False        # хоть один источник прямо сказал «нет такого»

    for source, fetch, source_key, needs_key in sources(key, community_key):
        if needs_key and not source_key:
            continue       # без ключа этот источник не отвечает вовсе
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                name, verdict, note = await fetch(
                    session, uid, region, source_key)
            if note:
                log.info("Ники: %s — %s", source, note)
        except Exception as exc:  # noqa: BLE001 — переходим к следующему
            log.info("Ники: %s не ответил — %s", source, exc)
            continue

        if verdict == "ok" and name:
            remember(uid, name)
            return Nickname(uid=uid, name=name, verdict="ok", source=source)
        if verdict == "bad":
            # Отказ запоминаем, но очередь не обрываем: другой источник
            # знает другие регионы и может найти того же игрока.
            refused = True

    if refused:
        return Nickname(uid=uid, name=None, verdict="bad")
    return Nickname(uid=uid, name=None, verdict="unknown")


#: Все источники в одном месте: очередь берёт их отсюда, и проверка —
#: тоже. Два разных списка рано или поздно разошлись бы.
def sources(key: str = "", community_key: str = "") -> list[tuple]:
    """(имя, функция, ключ, нужен ли ключ) в порядке опроса."""
    return [
        ("glob-info", _from_glob, "", False),
        ("gameskinbo", _from_gameskinbo, key, True),
        ("freefirecommunity", _from_community, community_key, False),
        ("free-ff-api", _fallback_source, "", False),
    ]


async def _fallback_source(session, uid, region, key):
    """Запасной источник в общем виде — он поднимает своё соединение."""
    return await _from_fallback(uid, region)


async def probe(
    uid: str, key: str = "", region: str = "", community_key: str = "",
) -> list[dict]:
    """Спросить ник у ВСЕХ источников и сказать, кто что ответил.

    Нужна, чтобы выбирать источники по факту, а не по обещаниям в их
    документации: сервисы бесплатные, живут на чужих хостингах и со
    временем портятся молча. Раз в пару месяцев стоит посмотреть, кто
    ещё отвечает и не расходятся ли ответы.
    """
    import time

    uid = str(uid).strip()
    out: list[dict] = []

    for name, fetch, source_key, needs_key in sources(key, community_key):
        row = {"source": name, "name": None, "verdict": "", "error": "",
               "seconds": 0.0}
        if needs_key and not source_key:
            row["verdict"] = "нет ключа"
            out.append(row)
            continue

        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                found, verdict, note = await fetch(
                    session, uid, region, source_key)
            row["name"], row["verdict"], row["error"] = found, verdict, note
        except Exception as exc:  # noqa: BLE001 — это и есть предмет проверки
            row["verdict"] = "сбой"
            row["error"] = f"{type(exc).__name__}: {str(exc)[:90]}"
        row["seconds"] = round(time.monotonic() - started, 1)
        out.append(row)
    return out


async def usage(key: str) -> dict | None:
    """Остаток лимита у gameskinbo."""
    if not key:
        return None
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(
                GAMESKINBO_USAGE, headers={"x-api-key": key}
            ) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.info("Ники: лимит не прочитался — %s", exc)
        return None
