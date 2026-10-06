"""Игрушечный поставщик: чтобы сайт работал без ключа FazerCards и для тестов.
Заказы выполняются со второго запроса статуса и выдают фейковые коды."""

from __future__ import annotations

import secrets
import threading
from decimal import Decimal
from typing import Any, Iterable

from .base import ProductData, SupplierOrder, SupplierRejected, SupplierUnavailable, steam_gift_product

_TG_FIELD = [{"key": "telegram_username", "label": "Telegram @username", "type": "text"}]
_PLAYER_FIELD = [{"key": "player_id", "label": "Player ID", "type": "text"}]


def demo_catalog() -> list[ProductData]:
    items = [
        ProductData("tg-stars", "telegram_stars", "telegram", "Telegram", "Telegram Stars",
                    Decimal("0.015375"), unit="star", min_qty=50, max_qty=10000, fields=_TG_FIELD),
    ]
    items.append(ProductData("steam-topup", "steam_topup", "steam", "Steam", "Пополнение Steam",
                             Decimal("0.975"), unit="usd",
                             fields=[{"key": "steam_login", "label": "Логин Steam", "type": "text"},
                                     {"key": "currency", "label": "Валюта", "type": "select"},
                                     {"key": "amount", "label": "Сумма", "type": "number"}],
                             supplier_ref={"rates": {"USD": "1", "RUB": "92.5", "KZT": "520", "UAH": "41.2"},
                                           "min_usd": "0.5", "max_usd": "1000"}))
    items.append(steam_gift_product())
    for months, price in ((3, "12.2898"), (6, "16.3898"), (12, "29.7148")):
        items.append(ProductData(f"tg-premium-{months}", "telegram_premium", "telegram", "Telegram",
                                 f"Telegram Premium — {months} мес.", Decimal(price),
                                 fields=_TG_FIELD, supplier_ref={"months": months}))
    for uc, price in ((60, "0.99"), (325, "4.75"), (660, "9.40"), (1800, "23.50")):
        items.append(ProductData(f"topup-pubg-{uc}", "topup", "pubg_mobile", "PUBG Mobile",
                                 f"{uc} UC", Decimal(price), fields=_PLAYER_FIELD,
                                 supplier_ref={"category_id": "pubg_mobile", "offer_id": f"uc{uc}"}))
    for dm, price, region in ((100, "0.95", "GLOBAL"), (520, "4.60", "GLOBAL"), (100, "0.89", "TR"),
                              (520, "4.35", "TR")):
        items.append(ProductData(f"topup-ff-{region.lower()}-{dm}", "topup", "free_fire", "Free Fire",
                                 f"{dm} алмазов", Decimal(price), fields=_PLAYER_FIELD, region=region,
                                 supplier_ref={"category_id": "free_fire", "offer_id": f"d{dm}{region}"}))
    for usd, price in ((5, "5.2500"), (10, "10.5000"), (20, "20.9000")):
        items.append(ProductData(f"gc-steam-{usd}", "gift_card", "steam_usd", "Steam USD",
                                 f"Steam — ${usd}", Decimal(price), max_qty=10, stock=50,
                                 supplier_ref={"category_id": "steam_usd", "card_id": f"s{usd}"}))
    for game_id, title, appid, keys in (
        ("elden-ring", "Elden Ring", 1245620, (("Elden Ring — Steam Key (Global)", "GLOBAL", "39.90"),
                                               ("Elden Ring — Steam Key (RU/CIS)", "CIS", "24.50"))),
        ("cs2-prime", "Counter-Strike 2 Prime", 730, (("CS2 Prime Status Upgrade", "GLOBAL", "13.20"),)),
    ):
        for name, region, price in keys:
            items.append(ProductData(f"gk-{game_id}-{region.lower()}", "game_key", game_id, title, name, Decimal(price),
                                     max_qty=5, stock=20, region=region,
                                     image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
                                     supplier_ref={"game_id": game_id, "key_id": f"{game_id}-{region}",
                                                   "platform": "Steam", "region_restriction": region != "GLOBAL"}))
    return items


class MockSupplier:
    name = "mock"

    def __init__(self, catalog: list[ProductData] | None = None, balance: Decimal = Decimal("1000")):
        self._catalog = catalog if catalog is not None else demo_catalog()
        self._balance = balance
        self._orders: dict[str, dict[str, Any]] = {}
        self._by_idem: dict[str, str] = {}
        self._lock = threading.Lock()
        # Для тестов: следующий create_order упадёт так, как задано.
        self.fail_next: str | None = None  # "reject" (нет денег) | "reject_other" | "unavailable" | …
        self.fail_on_poll = False
        self.polls_to_complete = 1

    def fetch_catalog(self) -> Iterable[ProductData]:
        return list(self._catalog)

    def is_idempotent(self, kind: str) -> bool:
        return kind in ("topup", "gift_card", "steam_topup", "steam_gift")

    GIFT_GAMES = {
        730: ("Counter-Strike 2", [(54029, "Counter-Strike 2 Prime Status Upgrade",
                                    {"CIS": "11.2000", "KZ": "12.4000", "TR": "9.8000"})]),
        570: ("Dota 2", [(197846, "Dota 2 — Dota Plus 3 мес.", {"CIS": "7.9000", "KZ": "8.3000"})]),
        1245620: ("ELDEN RING", [(354151, "ELDEN RING", {"CIS": "38.5000", "KZ": "41.0000", "TR": "29.9000"}),
                                  (354152, "ELDEN RING Deluxe Edition", {"CIS": "49.9000", "KZ": "53.0000"})]),
        1091500: ("Cyberpunk 2077", [(347800, "Cyberpunk 2077", {"CIS": "27.0000", "KZ": "28.5000"}),
                                     (347801, "Cyberpunk 2077: Ultimate Edition", {"CIS": "44.0000"})]),
    }

    def steam_gift_games(self) -> list[dict[str, Any]]:
        return [{"appid": a, "name": n} for a, (n, _) in self.GIFT_GAMES.items()]

    def steam_gift_offers(self, appid: int) -> list[dict[str, Any]]:
        game = self.GIFT_GAMES.get(int(appid))
        if not game:
            raise SupplierRejected("game not found", http_status=404)
        return [{"sub_id": s, "name": n, "regions": [{"region": r, "price": p} for r, p in regs.items()]}
                for s, n, regs in game[1]]

    def validate_id_categories(self) -> list[str]:
        return ["pubg_mobile", "free_fire"]

    def validate_account(self, category_id: str, fields: dict[str, str]) -> dict[str, Any]:
        pid = next(iter(fields.values()), "")
        if not pid.isdigit() or len(pid) < 5:
            return {"valid": False, "message": "Аккаунт не найден"}
        return {"valid": True, "player_name": f"Player_{pid[-4:]}", "region": "GLOBAL"}

    def gamekey_regions(self, game_id: str) -> dict[str, Any]:
        return {"region_type": "CIS", "has_availability": True,
                "available": [{"code": c, "name": n} for c, n in (("RU", "Russia"), ("KZ", "Kazakhstan"),
                                                                    ("TJ", "Tajikistan"), ("UZ", "Uzbekistan"))],
                "unavailable": [{"code": "US", "name": "United States"}, {"code": "DE", "name": "Germany"}]}

    def check_steam_login(self, login: str) -> bool:
        return not login.lower().startswith("bad")

    def create_order(self, product, quantity, fields, idem_key) -> SupplierOrder:
        with self._lock:
            mode, self.fail_next = self.fail_next, None
            if mode == "reject":
                raise SupplierRejected("Недостаточно средств у поставщика", code="insufficient_balance",
                                       http_status=400)
            if mode == "reject_other":
                raise SupplierRejected("Игрок не найден", code="invalid_player", http_status=400)
            if mode == "unavailable":
                raise SupplierUnavailable("timeout")
            if idem_key in self._by_idem:
                oid = self._by_idem[idem_key]
            else:
                oid = f"ord-{secrets.randbelow(9_000_000) + 1_000_000}"
                self._by_idem[idem_key] = oid
                self._orders[oid] = {"product": product, "quantity": quantity, "fields": fields, "polls": 0}
            if mode == "unavailable_after_create":
                raise SupplierUnavailable("timeout after create")
            return SupplierOrder(order_id=oid, status="processing", raw_status="processing")

    def get_order(self, supplier_order_id: str) -> SupplierOrder:
        with self._lock:
            if self.fail_on_poll:
                return SupplierOrder(supplier_order_id, "failed", "failed", message="Игрок не найден")
            order = self._orders.get(supplier_order_id)
            if order is None:
                raise SupplierRejected("order not found", http_status=404)
            order["polls"] += 1
            if order["polls"] < self.polls_to_complete:
                return SupplierOrder(supplier_order_id, "processing", "processing")
            product = order["product"]
            if product["kind"] in ("gift_card", "game_key"):
                codes = [f"DEMO-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
                         for _ in range(order["quantity"])]
                delivery = {"codes": codes}
            else:
                delivery = {"message": "Зачислено на аккаунт"}
            return SupplierOrder(supplier_order_id, "completed", "completed", delivery=delivery)

    def balance(self) -> Decimal:
        return self._balance
