"""Поставщик CoinDrop (coindrop.uz) — пополнение игр по ID: Standoff 2, PUBG, Blood Strike и др.

Подключается рядом с FazerCards (см. multi.py). Ключ CoinDrop — только на сервере
(DONATIX_COINDROP_API_KEY), наружу не отдаётся: партнёры и другие проекты покупают эти
игры через свой Donatix-ключ, как и всё остальное.

Берём только «обычные» игры-пополнения по числовому ID (id_type=numeric), при желании с
сервером/зоной. Amount-based (Stars/Steam), ваучеры и подарки CoinDrop пока не берём —
их у нас уже даёт FazerCards, чтобы не задваивать каталог.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Iterable

import httpx

from .base import ProductData, SupplierOrder, SupplierRejected, SupplierUnavailable, normalize_status, pick_image

log = logging.getLogger(__name__)

PREFIX = "cd"                      # id товара: cd-<game_key>-<product_id>; номер заказа: cd:<order_id>
CATEGORY_PREFIX = "cd_"           # category_id игр CoinDrop, чтобы не путать с FazerCards


class CoinDropSupplier:
    name = "CoinDrop"
    id_prefix = f"{PREFIX}-"     # все товары CoinDrop начинаются с "cd-" — для частичной загрузки

    def __init__(self, api_key: str, base_url: str = "https://coindrop.uz/api/v1", *,
                 catalog_pause: float = 0.6, only_games: list[str] | None = None):
        if not api_key:
            raise ValueError("DONATIX_COINDROP_API_KEY не задан")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(40, connect=15),
        )
        self._pause = catalog_pause
        self._only = {g.strip() for g in (only_games or []) if g.strip()}  # пусто — все игры-пополнения

    # ── HTTP ────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None, retries: int = 2) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                last = SupplierUnavailable(f"{method} {path}: {exc}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 500 or resp.status_code in (408, 429, 503):
                raise SupplierUnavailable(f"{method} {path}: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                detail = str(data.get("detail") or data.get("message") or f"HTTP {resp.status_code}")
                code = "insufficient_balance" if resp.status_code == 422 and "balance" in detail.lower() else ""
                raise SupplierRejected(detail, code=code, http_status=resp.status_code)
            return data if isinstance(data, dict) else {"data": data}
        raise last or SupplierUnavailable(f"{method} {path}")

    # ── Каталог ─────────────────────────────────────────────
    @staticmethod
    def _list(data: Any, *keys: str) -> list[dict[str, Any]]:
        """Список из ответа: сам список или первый список под одним из ключей (games/data/items…)."""
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in keys + ("data", "items", "results"):
                v = data.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
                if isinstance(v, dict):
                    inner = CoinDropSupplier._list(v, *keys)
                    if inner:
                        return inner
        return []

    def games(self) -> list[dict[str, Any]]:
        return self._list(self._request("GET", "/games"), "games")

    def fetch_catalog(self) -> Iterable[ProductData]:
        for g in self.games():
            key = str(g.get("game_key") or g.get("key") or g.get("slug") or "").strip()
            if not key:
                continue
            if self._only and key not in self._only:
                continue
            # Только пополнения по числовому ID: без amount-based, ваучеров и подарков
            if bool(g.get("amount_based")) or g.get("id_type") not in (None, "", "numeric"):
                continue
            gtype = str(g.get("type") or "").lower()
            if gtype in ("voucher", "vouchers", "giftcard", "gift", "gifts", "promocode"):
                continue
            name = str(g.get("name") or key)
            requires_server = bool(g.get("requires_server"))
            server_label = str(g.get("server_label") or "Сервер / зона")
            image = pick_image(g)
            fields: list[dict[str, str]] = [{"key": "player_id", "label": "ID игрока", "type": "text"}]
            if requires_server:
                fields.append({"key": "server_id", "label": server_label, "type": "text"})
            time.sleep(self._pause)
            try:
                products = self._list(self._request("GET", f"/games/{key}/products"), "products")
            except SupplierRejected as exc:
                log.warning("CoinDrop: пропускаю игру %s: %s", key, exc)
                continue
            for p in products:
                pid = str(p.get("product_id") or p.get("id") or "").strip()
                price = p.get("price_usd") if p.get("price_usd") is not None else p.get("price")
                if not pid or price is None:
                    continue
                try:
                    base_price = Decimal(str(price))
                except (ValueError, ArithmeticError):
                    continue
                yield ProductData(
                    id=f"{PREFIX}-{key}-{pid}",
                    kind="topup",
                    category_id=f"{CATEGORY_PREFIX}{key}",
                    category_name=name,
                    name=str(p.get("name") or p.get("title") or pid),
                    base_price=base_price,
                    unit="item",
                    fields=list(fields),
                    image_url=pick_image(p) or image,
                    region=None,
                    supplier_ref={"provider": "coindrop", "game_key": key, "product_id": pid,
                                  "requires_server": requires_server, "server_label": server_label},
                )

    # ── Заказы ──────────────────────────────────────────────
    def create_order(self, product: dict[str, Any], quantity: int, fields: dict[str, str],
                     idem_key: str) -> SupplierOrder:
        ref = product["supplier_ref"]
        if ref.get("provider") != "coindrop":
            raise SupplierRejected("не товар CoinDrop")
        player = (fields.get("player_id") or fields.get("player") or "").strip()
        if not player:
            raise SupplierRejected("нужен ID игрока")
        body: dict[str, Any] = {"game_key": ref["game_key"], "product_id": ref["product_id"],
                                "player_id": player, "external_ref": idem_key[:64]}
        if ref.get("requires_server"):
            server = (fields.get("server_id") or fields.get("server") or "").strip()
            if not server:
                raise SupplierRejected("нужен сервер / зона")
            body["server_id"] = server
        data = self._request("POST", "/orders", json=body, retries=0)   # заказ не повторяем сами
        order_id = data.get("order_id") or data.get("id")
        raw = str(data.get("status") or "processing")
        return SupplierOrder(
            order_id=f"{PREFIX}:{order_id}" if order_id is not None else None,
            status=normalize_status(raw), raw_status=raw,
            message=str(data.get("message") or ""),
        )

    def get_order(self, supplier_order_id: str) -> SupplierOrder:
        oid = supplier_order_id.split(":", 1)[1] if supplier_order_id.startswith(f"{PREFIX}:") else supplier_order_id
        data = self._request("GET", f"/orders/{oid}")
        raw = str(data.get("status") or "")
        delivery = None
        for key in ("codes", "pin_code", "payload", "delivery"):
            if data.get(key):
                delivery = {key: data[key]} if not isinstance(data.get(key), dict) else data[key]
                break
        return SupplierOrder(order_id=supplier_order_id, status=normalize_status(raw), raw_status=raw,
                             delivery=delivery, message=str(data.get("message") or data.get("detail") or ""))

    def balance(self) -> Decimal:
        data = self._request("GET", "/balance")
        for key in ("balance_usd", "usd", "balance"):
            val = data.get(key)
            if isinstance(val, dict):
                val = val.get("usd") or val.get("amount")
            if val is not None:
                try:
                    return Decimal(str(val))
                except (ValueError, ArithmeticError):
                    pass
        raise SupplierRejected(f"в ответе /balance нет баланса: {str(data)[:200]}")

    def is_idempotent(self, kind: str) -> bool:
        return False   # у CoinDrop нет заголовка идемпотентности — заказ сам не повторяем

    # ── Не поддерживается CoinDrop-поставщиком ──────────────
    def validate_id_categories(self) -> list[str]:
        return []      # предзаказная проверка ID у CoinDrop требует отдельного тарифа — пока не используем

    def validate_account(self, category_id: str, fields: dict[str, str]) -> dict[str, Any]:
        return {"valid": None, "player_name": None, "region": None, "message": ""}

    def steam_gift_games(self) -> list[dict[str, Any]]:
        return []

    def steam_gift_offers(self, appid: int) -> list[dict[str, Any]]:
        return []

    def gamekey_regions(self, game_id: str) -> dict[str, Any]:
        return {"region_type": "", "available": [], "unavailable": [], "has_availability": False}

    def check_steam_login(self, login: str) -> bool:
        return True

    def handles_product(self, product: dict[str, Any]) -> bool:
        return (product.get("supplier_ref") or {}).get("provider") == "coindrop"

    def handles_order_id(self, supplier_order_id: str) -> bool:
        return bool(supplier_order_id) and supplier_order_id.startswith(f"{PREFIX}:")
