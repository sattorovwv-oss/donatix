"""Несколько поставщиков как один: FazerCards (основной) + CoinDrop и т.п.

Каждый товар помнит своего поставщика (supplier_ref.provider), а номер заказа — с
меткой поставщика, поэтому заказы всегда уходят и проверяются у правильного. Если
дополнительный поставщик недоступен при загрузке каталога — его товары временно не
обновятся (останутся как были), но каталог основного не пострадает.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterable

from .base import ProductData, Supplier, SupplierError, SupplierOrder

log = logging.getLogger(__name__)


class MultiSupplier:
    def __init__(self, primary: Supplier, extras: list[Supplier]):
        self.primary = primary
        self.extras = extras
        self.name = " + ".join([primary.name] + [e.name for e in extras])

    def _for_product(self, product: dict[str, Any]) -> Supplier:
        provider = (product.get("supplier_ref") or {}).get("provider")
        for e in self.extras:
            if getattr(e, "handles_product", None) and e.handles_product(product):
                return e
            if provider and getattr(e, "name", "").lower() == str(provider).lower():
                return e
        return self.primary

    def _for_order_id(self, supplier_order_id: str) -> Supplier:
        for e in self.extras:
            if getattr(e, "handles_order_id", None) and e.handles_order_id(supplier_order_id):
                return e
        return self.primary

    def providers(self) -> list[dict[str, str]]:
        """Доп. поставщики для частичной загрузки: имя и префикс id их товаров."""
        out = []
        for e in self.extras:
            out.append({"name": e.name, "id_prefix": getattr(e, "PREFIX_ID", getattr(e, "id_prefix", "")) or ""})
        return out

    def get_extra(self, name: str):
        for e in self.extras:
            if e.name.lower() == name.lower():
                return e
        return None

    def fetch_catalog(self) -> Iterable[ProductData]:
        # Кто из доп. поставщиков не ответил: их товары при загрузке НЕ выключаем (catalog.sync_catalog)
        self.failed_prefixes: set[str] = set()
        yield from self.primary.fetch_catalog()
        for e in self.extras:
            try:
                yield from e.fetch_catalog()
            except Exception as exc:  # noqa: BLE001 — доп. поставщик упал: не рушим весь каталог
                log.warning("каталог %s недоступен, его товары не трогаю: %s", getattr(e, "name", "extra"), exc)
                prefix = getattr(e, "id_prefix", "")
                if prefix:
                    self.failed_prefixes.add(prefix)

    def create_order(self, product: dict[str, Any], quantity: int, fields: dict[str, str],
                     idem_key: str) -> SupplierOrder:
        return self._for_product(product).create_order(product, quantity, fields, idem_key)

    def get_order(self, supplier_order_id: str) -> SupplierOrder:
        return self._for_order_id(supplier_order_id).get_order(supplier_order_id)

    def is_idempotent(self, kind: str) -> bool:
        return self.primary.is_idempotent(kind)   # для CoinDrop решаем по товару в orders.py

    def balance(self) -> Decimal:
        return self.primary.balance()

    def extra_balances(self) -> list[dict[str, Any]]:
        """Балансы доп. поставщиков — для админки. Ошибку показываем, но не роняем страницу."""
        out = []
        for e in self.extras:
            try:
                out.append({"name": e.name, "balance": e.balance(), "error": ""})
            except SupplierError as exc:
                out.append({"name": e.name, "balance": None, "error": str(exc)[:200]})
        return out

    def validate_id_categories(self) -> list[str]:
        cats = list(self.primary.validate_id_categories())
        for e in self.extras:
            cats.extend(e.validate_id_categories())
        return cats

    def validate_account(self, category_id: str, fields: dict[str, str]) -> dict[str, Any]:
        for e in self.extras:
            if category_id in e.validate_id_categories():
                return e.validate_account(category_id, fields)
        return self.primary.validate_account(category_id, fields)

    # Steam-гифты, регионы ключей, проверка Steam-логина — только у основного (FazerCards)
    def steam_gift_games(self) -> list[dict[str, Any]]:
        return self.primary.steam_gift_games()

    def steam_gift_offers(self, appid: int) -> list[dict[str, Any]]:
        return self.primary.steam_gift_offers(appid)

    def gamekey_regions(self, game_id: str) -> dict[str, Any]:
        return self.primary.gamekey_regions(game_id)

    def check_steam_login(self, login: str) -> bool:
        return self.primary.check_steam_login(login)
