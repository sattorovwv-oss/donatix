"""Прайс-лист картинкой: Free Fire СНГ, Free Fire Индонезия и PUBG Mobile с ценами в сомони.

Цены — настоящие, из каталога: закупка у поставщика + наценка сайта (как видит
новый клиент) → по курсу сомони из «Реквизитов». Картинку собирает браузер
админа из этой страницы — поэтому цифры на ней всегда совпадают с сайтом, чего
не добиться от генератора картинок.
"""

from __future__ import annotations

import sqlite3
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from . import packs, popular
from .config import Config
from .money import apply_markup, to_decimal

SECTIONS = (("ff_cis", "Free Fire", "СНГ"), ("ff_id", "Free Fire", "Индонезия"), ("pubg", "PUBG Mobile", "UC"))


def _categories(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None, str | None]]:
    """key → (category_id, регион или None, обложка) — те же игры, что в «Популярном»."""
    return {p["key"]: (p["category_id"], p["region"], p["image_url"])
            for p in popular._pinned(conn) if p["key"] in ("ff_cis", "ff_id", "pubg")}


def tjs_price(base: Any, markup: Decimal, rate: Decimal) -> Decimal:
    """Цена продажи в сомони: так же, как сайт списывает (4 знака вверх) и показывает (2 знака)."""
    usd = apply_markup(to_decimal(base), markup).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
    return (usd * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build(conn: sqlite3.Connection, config: Config) -> dict[str, Any]:
    from .payments import settings as pay_settings
    rate = Decimal(str(pay_settings(conn, config)["tjs_rate"]))
    markup = config.kind_markups.get("topup", config.markups["bronze"])
    cats = _categories(conn)
    sections = []
    for key, game, sub in SECTIONS:
        if key not in cats:
            continue
        category_id, region, image = cats[key]
        sql = "SELECT name, base_price FROM products WHERE active = 1 AND hidden = 0 AND kind = 'topup' " \
              "AND category_id = ?"
        args: list[Any] = [category_id]
        if region:
            sql += " AND region = ?"
            args.append(region)
        rows = conn.execute(sql, args).fetchall()
        # Тот же перевод и порядок, что на сайте: 💎 алмазы → 🎫 ваучеры → 🚀 прокачка
        items = sorted(({"name": r["name"], "tjs": tjs_price(r["base_price"], markup, rate)} for r in rows),
                        key=lambda i: packs.order_key(i["name"], float(i["tjs"])))
        for i in items:
            i["group"] = packs.group(i["name"])
            i["price"] = f"{i['tjs']:.2f}"
            i["short"] = packs.full(i["name"])
        if items:
            sections.append({"key": key, "game": game, "sub": sub, "image": image, "packs": items})
    return {"sections": sections, "rate": rate, "markup": markup}
