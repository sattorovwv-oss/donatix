"""Каталог: храним копию каталога поставщика у себя и обновляем по расписанию
(FazerCards просит не дёргать каталог перед каждым заказом)."""

from __future__ import annotations

import json
import logging
import sqlite3
from decimal import Decimal
from typing import Any, Callable

from . import cache, db
from .leader import FileLock
from .money import apply_markup, fmt_unit, to_decimal
from .suppliers import KIND_TITLES, Supplier, SupplierError, region_title

log = logging.getLogger(__name__)


# Одно обновление каталога за раз: фоновый воркер и кнопка в админке не мешают друг другу
# (замок и между процессами сайта — после bind(config) при запуске)
SYNC_LOCK = FileLock("catalog")


_UPSERT = """
    INSERT INTO products (id, kind, category_id, category_name, name, base_price, unit,
                          min_qty, max_qty, stock, fields_json, supplier_ref_json, active, updated_at,
                          image_url, region)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        kind = excluded.kind, category_id = excluded.category_id,
        category_name = excluded.category_name, name = excluded.name,
        base_price = excluded.base_price, unit = excluded.unit,
        min_qty = excluded.min_qty, max_qty = excluded.max_qty, stock = excluded.stock,
        fields_json = excluded.fields_json, supplier_ref_json = excluded.supplier_ref_json,
        active = 1, updated_at = excluded.updated_at,
        image_url = excluded.image_url, region = excluded.region
"""


MAX_DISABLE_SHARE = 0.3   # больше 30% каталога «пропало» за одну загрузку — это сбой, а не изменения


def sync_catalog(conn: sqlite3.Connection, supplier: Supplier,
                 progress: Callable[[int, str], None] | None = None, *, id_prefix: str = "") -> dict[str, int]:
    """Забрать каталог у поставщика. Пропавшие товары выключаются, а не удаляются:
    на них ссылаются старые заказы. progress(сколько товаров, текущая категория) — для экрана прогресса.

    id_prefix — частичная загрузка только одного поставщика (например "cd-" для CoinDrop):
    берём его товары и выключаем только его пропавшие, каталог остальных не трогаем."""
    started = db.now()
    seen: set[str] = set()
    batch: list[tuple] = []

    def write() -> None:
        # Пачкой в одной транзакции: раньше каждый товар был отдельной записью на диск,
        # и ежечасная загрузка тысяч товаров тормозила весь сайт
        if batch:
            with db.tx(conn):
                conn.executemany(_UPSERT, batch)
            batch.clear()

    for p in supplier.fetch_catalog():
        seen.add(p.id)
        if progress:
            progress(len(seen), p.category_name)
        batch.append((p.id, p.kind, p.category_id, p.category_name, p.name, str(p.base_price), p.unit,
                      p.min_qty, max(p.max_qty, p.min_qty), p.stock, json.dumps(p.fields, ensure_ascii=False),
                      json.dumps(p.supplier_ref, ensure_ascii=False), started, p.image_url, p.region))
        if len(batch) >= 300:
            write()
    write()
    disabled = 0
    if seen:
        # Все пришедшие товары получили updated_at = started; остальные — пропали у поставщика.
        # (Не «id NOT IN (…тысячи id…)»: старые SQLite не принимают больше 999 параметров.)
        where, args = "active = 1 AND updated_at <> ?", [started]
        if id_prefix:
            where += " AND id LIKE ?"
            args.append(id_prefix + "%")
        for prefix in sorted(getattr(supplier, "failed_prefixes", set())):
            where += " AND id NOT LIKE ?"          # поставщик не ответил — его товары не «пропали»
            args.append(prefix + "%")
        gone = conn.execute(f"SELECT COUNT(*) FROM products WHERE {where}", args).fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1" +
                              (" AND id LIKE ?" if id_prefix else ""),
                              [id_prefix + "%"] if id_prefix else []).fetchone()[0]
        if active >= 50 and gone > active * MAX_DISABLE_SHARE:
            # Поставщик ответил не полностью (сбой, лимит) — не выключаем разом пол-каталога
            log.warning("каталог: пропало %s из %s товаров — похоже на сбой у поставщика, не выключаю", gone, active)
            db.set_setting(conn, "catalog_sync_warning",
                           f"{db.now()}: пропало {gone} из {active} товаров — не выключено (сбой поставщика?)")
        else:
            disabled = conn.execute(f"UPDATE products SET active = 0 WHERE {where}", args).rowcount
    if not id_prefix and "steam-gift" in seen:
        from . import steam_gifts
        try:
            n = steam_gifts.sync_games(conn, supplier)
            log.info("steam-гифты: %s игр", n)
        except SupplierError as exc:
            log.warning("steam-гифты: каталог игр не обновлён: %s", exc)
        steam_gifts.clear_cache()
    db.set_setting(conn, "catalog_synced_at", db.now())
    cache.clear_everywhere(conn)
    log.info("каталог: %s товаров, выключено %s", len(seen), disabled)
    return {"products": len(seen), "disabled": disabled}


# Пополнение игры всегда идёт на чей-то аккаунт. Если поставщик не прислал поля (бывает у PUBG и др.),
# без этого сайт и бот не спрашивали ID игрока — и заказ уходил без получателя.
DEFAULT_TOPUP_FIELDS = [{"key": "player_id", "label": "ID игрока", "type": "text"}]


def load_product(row: sqlite3.Row) -> dict[str, Any]:
    product = dict(row)
    product["fields"] = json.loads(row["fields_json"] or "[]")
    if product.get("kind") == "topup" and not product["fields"]:
        product["fields"] = [dict(f) for f in DEFAULT_TOPUP_FIELDS]
    product["supplier_ref"] = json.loads(row["supplier_ref_json"] or "{}")
    return product


def get_product(conn: sqlite3.Connection, product_id: str, *, for_sale: bool = True) -> dict[str, Any] | None:
    sql = "SELECT * FROM products WHERE id = ?"
    if for_sale:
        sql += " AND active = 1 AND hidden = 0"
    row = conn.execute(sql, (product_id,)).fetchone()
    return load_product(row) if row else None


def list_products(
    conn: sqlite3.Connection, *, kind: str = "", q: str = "", category_id: str = "", region: str = "",
    include_hidden: bool = False, limit: int = 500, offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM products WHERE active = 1"
    args: list[Any] = []
    if not include_hidden:
        sql += " AND hidden = 0"
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if category_id:
        sql += " AND category_id = ?"
        args.append(category_id)
    if region:
        sql += " AND region = ?"
        args.append(region)
    if q:
        sql += " AND (name LIKE ? OR category_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY kind, category_name, CAST(base_price AS REAL), name LIMIT ? OFFSET ?"
    args += [limit, offset]
    # Каталог меняется раз в час (загрузка у поставщика сбрасывает кеш сразу) — а спрашивают его боты
    # партнёров постоянно. Держим готовый список минуту; копия — чтобы вызывающий не испортил кеш.
    key = "lp:" + "\x1f".join(map(str, (kind, q, category_id, region, include_hidden, limit, offset)))
    rows = cache.get_or_set(key, 60, lambda: [load_product(r) for r in conn.execute(sql, args)])
    return [dict(p) for p in rows]


def categories(conn: sqlite3.Connection, kind: str = "", q: str = "") -> list[sqlite3.Row]:
    """Игры и сервисы: одна строка на категорию, с обложкой и списком регионов."""
    sql = ("SELECT kind, category_id, category_name, COUNT(*) AS n, MIN(CAST(base_price AS REAL)) AS from_price, "
           "MAX(image_url) AS image_url, GROUP_CONCAT(DISTINCT region) AS regions "
           "FROM products WHERE active = 1 AND hidden = 0")
    args: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if q:
        sql += " AND (name LIKE ? OR category_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " GROUP BY kind, category_id, category_name ORDER BY kind, category_name"
    return cache.get_or_set(f"cats:{kind}\x1f{q}", 60, lambda: conn.execute(sql, args).fetchall())


def public_view(product: dict[str, Any], markup: Decimal) -> dict[str, Any]:
    """Товар, как его видит клиент: наша цена, без закупочной и без данных поставщика."""
    price = apply_markup(to_decimal(product["base_price"]), markup)
    return {
        "product_id": product["id"],
        "kind": product["kind"],
        "kind_title": KIND_TITLES.get(product["kind"], product["kind"]),
        "category_id": product["category_id"],
        "category_name": product["category_name"],
        "name": product["name"],
        **_pack_view(product),
        "unit": product["unit"],
        "price_usd": fmt_unit(price),
        "min_quantity": product["min_qty"],
        "max_quantity": product["max_qty"],
        "stock": product["stock"],
        "fields": product["fields"],
        "image_url": product.get("image_url"),
        "region": product.get("region"),
        "region_title": region_title(product.get("region")),
        **(_steam_extra(product, price) if product["kind"] == "steam_topup" else {}),
        **({"platform": product["supplier_ref"].get("platform") or "",
            "region_restriction": bool(product["supplier_ref"].get("region_restriction"))}
           if product["kind"] == "game_key" else {}),
    }


def _pack_view(product: dict[str, Any]) -> dict[str, Any]:
    """Для витрины: «💎 100 алмазов» вместо «100 Diamonds» у пакетов игр."""
    from . import packs
    if product["kind"] != "topup":
        return {"title": product["name"], "emoji": "", "group": None}
    return {"title": packs.label(product["name"]), "emoji": packs.emoji(product["name"]),
            "group": packs.group(product["name"])}


def _steam_extra(product: dict[str, Any], price: Decimal) -> dict[str, Any]:
    ref = product["supplier_ref"]
    return {
        "rates": ref.get("rates", {}),
        "min_usd": ref.get("min_usd"),
        "max_usd": ref.get("max_usd"),
        # Сколько клиент платит за 1 USD, зачисленный на Steam.
        "discount_percent": fmt_unit((Decimal(1) - price) * 100) if price < 1 else "0.0000",
    }


# Сроки выдачи, которые называет поставщик, — показываем клиенту до оплаты, чтобы не писал в поддержку раньше времени
DELIVERY_NOTES = (
    ("clash of clans", "⏱ Выдача через вход в аккаунт: обычно 20–90 минут, с 11:00 до 23:00 по Душанбе "
                       "(09:00–21:00 МСК). Заказ, оформленный ночью, выполнят утром."),
    ("standoff", "⏱ Выдача до 90 минут (обычно быстрее), с 11:00 до 23:00 по Душанбе (09:00–21:00 МСК). "
                 "Заказ, оформленный ночью, выполнят утром."),
)


def delivery_note(category_name: str | None) -> str:
    name = (category_name or "").lower()
    return next((note for key, note in DELIVERY_NOTES if key in name), "")

