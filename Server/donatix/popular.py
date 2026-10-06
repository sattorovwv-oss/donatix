"""«Популярное» в меню кабинета и на главной.

Сначала закреплённые — то, ради чего к нам приходят: Free Fire СНГ,
Free Fire Индонезия, PUBG Mobile, Standoff 2, Clash of Clans (как только они есть в каталоге)
и Telegram. Дальше алгоритм добавляет сам
игры и сервисы, которые чаще всего покупали за последние 30 дней (выполненные
заказы). Список считается раз в 10 минут, так что меню не нагружает базу.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from . import cache

LIMIT = 7
TTL = 600

_CIS = {"cis", "снг", "ru", "russia", "россия"}
_ID = {"id", "idn", "indonesia", "индонезия"}


def _words(text: str) -> set[str]:
    for ch in "_-()[]/,.·|":
        text = text.replace(ch, " ")
    return set(text.lower().split())


def _game(name: str, cat_id: str) -> str:
    text = f"{name} {cat_id}".lower().replace("_", " ").replace("-", " ")
    words = _words(text)
    if "pubg" in text and "new state" not in text and "newstate" not in text and "lite" not in words:
        return "pubg"
    if ("free fire" in text or "freefire" in text) and "max" not in words:
        return "ff"
    if "standoff" in text:
        return "standoff"
    if "clash of clans" in text or "clashofclans" in text:
        return "coc"
    return ""


def _link(kind: str, category_id: str, region: str | None = None) -> str:
    q = {"kind": kind, "category": category_id}
    if region:
        q["region"] = region
    return "/panel/catalog?" + urlencode(q)


def _pinned(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT category_id, category_name, region, MAX(image_url) AS image_url, COUNT(*) AS n FROM products "
        "WHERE active = 1 AND hidden = 0 AND kind = 'topup' GROUP BY category_id, category_name, region "
        "ORDER BY n DESC").fetchall()
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        game = _game(r["category_name"], r["category_id"])
        if not game:
            continue
        region = (r["region"] or "").upper()
        words = _words(f"{r['category_name']} {r['category_id']}")
        if game == "pubg":
            key, title, reg = "pubg", "PUBG Mobile", None
        elif game == "standoff":
            key, title, reg = "standoff2", "Standoff 2", None
        elif game == "coc":
            key, title, reg = "coc", "Clash of Clans", None
        elif region in ("CIS", "RU") or words & _CIS:
            key, title, reg = "ff_cis", "Free Fire СНГ", r["region"] if region in ("CIS", "RU") else None
        elif region == "ID" or words & _ID:
            key, title, reg = "ff_id", "Free Fire Индонезия", r["region"] if region == "ID" else None
        else:
            continue
        entry = {"title": title, "href": _link("topup", r["category_id"], reg), "kind": "topup",
                 "image_url": r["image_url"], "key": key, "cat": f"topup:{r['category_id']}",
                 "category_id": r["category_id"], "region": reg}
        if key in ("standoff2", "coc") and str(r["category_id"]).startswith("vd_"):
            found[key] = entry   # Standoff 2 и Clash of Clans — от Vendoria, если она подключена
        else:
            found.setdefault(key, entry)
    out = [found[k] for k in ("ff_cis", "ff_id", "pubg", "standoff2", "coc") if k in found]
    if conn.execute("SELECT 1 FROM products WHERE active = 1 AND hidden = 0 "
                    "AND kind IN ('telegram_stars', 'telegram_premium') LIMIT 1").fetchone():
        out.append({"title": "Telegram Stars и Premium", "href": "/panel/catalog?kind=telegram",
                    "kind": "telegram_stars", "image_url": None, "key": "telegram"})
    return out


def _best_sellers(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT p.kind, p.category_id, p.category_name, MAX(p.image_url) AS image_url, COUNT(o.id) AS sold "
        "FROM orders o JOIN products p ON p.id = o.product_id "
        "WHERE o.status = 'completed' AND o.created_at >= ? AND p.active = 1 AND p.hidden = 0 "
        "GROUP BY p.kind, p.category_id, p.category_name ORDER BY sold DESC LIMIT 20", (since,)).fetchall()
    out = []
    for r in rows:
        if r["kind"] in ("telegram_stars", "telegram_premium"):
            out.append({"title": "Telegram Stars и Premium", "href": "/panel/catalog?kind=telegram",
                        "kind": "telegram_stars", "image_url": None, "key": "telegram"})
        elif r["kind"] in ("steam_topup", "steam_gift"):
            slug = "steam-topup" if r["kind"] == "steam_topup" else "steam-gift"
            out.append({"title": "Пополнение Steam" if r["kind"] == "steam_topup" else "Steam Гифты",
                        "href": f"/panel/buy/{slug}", "kind": r["kind"], "image_url": None, "key": r["kind"]})
        else:
            out.append({"title": r["category_name"], "href": _link(r["kind"], r["category_id"]),
                        "kind": r["kind"], "image_url": r["image_url"], "key": f"{r['kind']}:{r['category_id']}"})
    return out


def compute(conn: sqlite3.Connection, limit: int = LIMIT) -> list[dict[str, Any]]:
    pinned = _pinned(conn)
    out = list(pinned)
    # Игры, уже закреплённые по регионам (Free Fire СНГ/Индонезия), продажи не дублируют
    seen = {p["key"] for p in pinned} | {p["cat"] for p in pinned if "cat" in p}
    for item in _best_sellers(conn):
        if len(out) >= limit:
            break
        if item["key"] not in seen:
            seen.add(item["key"])
            out.append(item)
    return out[:limit]


def services(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return cache.get_or_set("popular", TTL, lambda: compute(conn))
