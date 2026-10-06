"""Steam-гифты: каталог игр храним у себя (для быстрого поиска), а издания с ценами
по регионам берём у поставщика по запросу и кэшируем на несколько минут."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from decimal import Decimal
from typing import Any

from . import db
from .money import apply_markup, fmt_unit, to_decimal
from .suppliers import Supplier, SupplierError

OFFERS_TTL = 300  # секунд
INVITE_RE = re.compile(r"^https://s\.team/p/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/?$")
REGION_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()


def cover_url(appid: int) -> str:
    """Обложка из магазина Steam — так же её показывают все магазины ключей."""
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{int(appid)}/header.jpg"


def sync_games(conn: sqlite3.Connection, supplier: Supplier) -> int:
    games = supplier.steam_gift_games()
    if not games:
        return 0
    with db.tx(conn):
        conn.execute("DELETE FROM steam_gift_games")
        conn.executemany(
            "INSERT OR REPLACE INTO steam_gift_games (appid, name, name_lc) VALUES (?, ?, ?)",
            [(g["appid"], g["name"], g["name"].lower()) for g in games],
        )
    return len(games)


def search(conn: sqlite3.Connection, q: str, limit: int = 30) -> list[dict[str, Any]]:
    q = q.strip()
    if q.isdigit():
        rows = conn.execute("SELECT appid, name FROM steam_gift_games WHERE appid = ?", (int(q),)).fetchall()
    elif q:
        rows = conn.execute(
            "SELECT appid, name FROM steam_gift_games WHERE name_lc LIKE ? "
            "ORDER BY (name_lc LIKE ?) DESC, length(name) LIMIT ?",
            (f"%{q.lower()}%", f"{q.lower()}%", limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT appid, name FROM steam_gift_games ORDER BY rowid LIMIT ?", (limit,)).fetchall()
    return [{"appid": r["appid"], "name": r["name"], "cover": cover_url(r["appid"])} for r in rows]


def game_name(conn: sqlite3.Connection, appid: int) -> str | None:
    row = conn.execute("SELECT name FROM steam_gift_games WHERE appid = ?", (appid,)).fetchone()
    return row["name"] if row else None


def offers(supplier: Supplier, appid: int) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(appid)
        if hit and now - hit[0] < OFFERS_TTL:
            return hit[1]
    data = supplier.steam_gift_offers(appid)
    with _lock:
        _cache[appid] = (now, data)
    return data


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def client_offers(supplier: Supplier, appid: int, markup: Decimal) -> list[dict[str, Any]]:
    """Издания с ценами для клиента (с наценкой). Регионы с нулевой ценой не продаём."""
    result = []
    for o in offers(supplier, appid):
        regions = []
        for r in o.get("regions") or []:
            price = to_decimal(r.get("price") or "0")
            if price > 0:
                regions.append({"region": str(r["region"]), "price_usd": fmt_unit(apply_markup(price, markup))})
        if regions:
            result.append({"sub_id": int(o["sub_id"]), "name": str(o.get("name") or o["sub_id"]), "regions": regions})
    return result


def resolve(supplier: Supplier, fields: dict[str, str]) -> tuple[str, Decimal]:
    """Найти издание и цену поставщика для заказа. Возвращает (название издания, цена USD)."""
    from .orders import OrderError  # избегаем кругового импорта

    try:
        items = offers(supplier, int(fields["app_id"]))
    except SupplierError:
        raise OrderError("Не удалось получить цены Steam. Попробуйте через минуту.", "supplier_unavailable",
                         503) from None
    for o in items:
        if int(o["sub_id"]) != int(fields["sub_id"]):
            continue
        for r in o.get("regions") or []:
            if str(r["region"]).lower() == fields["region"].lower():
                price = to_decimal(r.get("price") or "0")
                if price <= 0:
                    break
                return str(o.get("name") or o["sub_id"]), price
        raise OrderError("Это издание недоступно в выбранном регионе.", "region_unavailable", 409)
    raise OrderError("Издание не найдено. Обновите страницу и выберите заново.", "product_not_found", 404)
