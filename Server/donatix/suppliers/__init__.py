from __future__ import annotations

from ..config import Config
from .base import (
    KIND_TITLES,
    KINDS,
    ProductData,
    Supplier,
    SupplierError,
    SupplierOrder,
    SupplierRejected,
    SupplierUnavailable,
    region_title,
)


def make_supplier(config: Config) -> Supplier:
    if config.supplier == "fazer":
        from .fazer import FazerSupplier

        primary: Supplier = FazerSupplier(
            config.fazer_api_key, config.fazer_base_url, steam_discount=config.fazer_steam_discount,
            image_base=config.fazer_image_base, catalog_pause=config.fazer_catalog_pause)
    elif config.supplier == "mock":
        from .mock import MockSupplier

        primary = MockSupplier()
    else:
        raise ValueError(f"DONATIX_SUPPLIER: неизвестный поставщик {config.supplier!r} (fazer или mock)")

    extras: list = []
    if config.coindrop_api_key:
        from .coindrop import CoinDropSupplier
        games = [g for g in config.coindrop_games.split(",") if g.strip()]
        extras.append(CoinDropSupplier(config.coindrop_api_key, config.coindrop_base_url, only_games=games))
    if config.vendoria_token:
        from .vendoria import VendoriaSupplier
        games = [g for g in config.vendoria_games.split(",") if g.strip()]
        extras.append(VendoriaSupplier(config.vendoria_token, config.vendoria_base_url, only_games=games))
    if extras:
        from .multi import MultiSupplier
        return MultiSupplier(primary, extras)
    return primary


__all__ = [
    "KIND_TITLES",
    "KINDS",
    "ProductData",
    "Supplier",
    "SupplierError",
    "SupplierOrder",
    "SupplierRejected",
    "SupplierUnavailable",
    "make_supplier",
    "region_title",
]
