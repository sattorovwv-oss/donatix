"""Клиент FazerCards API v2 (см. docs/fazercards/api-reference.md).

Что умеет: каталог Telegram Stars/Premium, игровых пополнений и подарочных карт,
создание заказов, статус заказа, баланс. Steam, ключи игр и ручные услуги —
следующий шаг."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from decimal import Decimal
from typing import Any, Iterable

import httpx

from .base import (
    ProductData,
    pick_image,
    pick_region,
    steam_cover,
    steam_gift_product,
    SupplierOrder,
    SupplierRejected,
    SupplierUnavailable,
    normalize_status,
)

log = logging.getLogger(__name__)

# Для этих видов в документации заявлен Idempotency-Key: повтор с тем же ключом
# не создаст второй заказ. Для Telegram-покупок заголовок не упомянут —
# такие заказы при сбое сети не повторяем автоматически, а отдаём админу.
_IDEMPOTENT_KINDS = {"topup", "gift_card", "steam_topup", "steam_gift", "game_key"}

# Лимиты пополнения Steam в USD. Точных в документации нет — поставщик сам
# отклонит сумму вне своих пределов, и деньги клиенту вернутся.
STEAM_MIN_USD = Decimal("0.5")
STEAM_MAX_USD = Decimal("1000")


def _pid(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
    return f"{prefix}-{digest}"


def parse_balance(data: dict[str, Any]) -> Decimal:
    """Баланс из ответа: {"balance": "57.42"}, {"balance": {"amount": …}}, {"data": {"balance": …}} и т.п."""
    for holder in (data, data.get("data") or {}, data.get("result") or {}):
        if not isinstance(holder, dict):
            continue
        for key in ("balance", "balance_usd", "available", "amount"):
            value = holder.get(key)
            if isinstance(value, dict):
                value = value.get("amount") or value.get("available") or value.get("balance") or value.get("usd")
            if value is not None and str(value).strip() not in ("", "None"):
                try:
                    return Decimal(str(value).replace(",", "").replace("$", "").strip())
                except ArithmeticError:
                    continue
    raise SupplierRejected(f"в ответе /balance нет баланса: {str(data)[:200]}")


class FazerSupplier:
    name = "fazer"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.fzr.cards/api/v2",
        *,
        transport: httpx.BaseTransport | None = None,
        catalog_pause: float = 2.0,
        timeout: float = 30.0,
        steam_discount: Decimal = Decimal("0"),
        image_base: str = "",
    ):
        if not api_key:
            raise ValueError("FAZER_API_KEY не задан")
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        # Каталог читаем не чаще ~30 запросов в минуту: у ключа FazerCards общий лимит (~60/мин),
        # и им же могут пользоваться другие ваши боты — оставляем им половину.
        self._catalog_pause = catalog_pause
        # Откуда брать картинки, если поставщик отдал путь без домена (imageurl: "/uploads/…")
        origin = httpx.URL(base_url)
        self._image_base = (image_base or f"{origin.scheme}://{origin.host}").rstrip("/")
        # Скидка вашего тарифа FazerCards на пополнение Steam, % (Bronze 2.5, Silver 3, Gold 3.55).
        self._steam_discount = Decimal(steam_discount)

    # ── HTTP ──────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, retry_429: bool = True, background: bool = False,
                 wait: float | None = None, **kwargs) -> dict[str, Any]:
        from ..throttle import SUPPLIER, QueueTimeout
        for attempt in range(3):
            try:
                # Общая очередь: не больше N запросов в минуту на весь проект (админка → Настройки)
                limit = wait if wait is not None else (600 if background else 25)
                SUPPLIER.acquire(background=background, max_wait=limit)
            except QueueTimeout as exc:
                raise SupplierUnavailable(str(exc)) from exc
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise SupplierUnavailable(f"{method} {path}: {exc}") from exc
            if resp.status_code == 429 and retry_429 and attempt < 2:
                wait = float(resp.headers.get("Retry-After", "2") or 2)
                time.sleep(wait * random.uniform(1.0, 1.15))
                continue
            break
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise SupplierUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        if resp.status_code >= 400 or not data.get("ok", False):
            raise SupplierRejected(
                str(data.get("error") or f"HTTP {resp.status_code}"),
                code=str(data.get("code") or ""),
                http_status=resp.status_code,
            )
        return data

    def _catalog_get(self, path: str, **params) -> dict[str, Any] | None:
        """GET каталога; недоступные тарифу разделы (403) пропускаем."""
        time.sleep(self._catalog_pause)
        try:
            return self._request("GET", path, params=params, background=True)
        except SupplierRejected as exc:
            if exc.http_status in (403, 404):
                log.info("fazer: %s недоступен (%s)", path, exc)
                return None
            raise

    def _paged(self, path: str) -> Iterable[dict[str, Any]]:
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 50, "include_ui": 1}  # include_ui — обложки категорий
            if cursor:
                params["cursor"] = cursor
            data = self._catalog_get(path, **params)
            if not data:
                return
            yield from data.get("items", [])
            meta = data.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not meta.get("has_more") or not cursor:
                return

    # ── Каталог ───────────────────────────────────────────────

    def fetch_catalog(self) -> Iterable[ProductData]:
        yield from self._telegram()
        yield from self._steam_topup()
        if self._catalog_get("/steam-gifts/games", limit=1):  # раздел доступен вашему тарифу
            yield steam_gift_product()
        yield from self._topups()
        yield from self._giftcards()
        yield from self._gamekeys()

    def _telegram(self) -> Iterable[ProductData]:
        stars = self._catalog_get("/telegram/stars")
        if stars:
            yield ProductData(
                id="tg-stars",
                kind="telegram_stars",
                category_id="telegram",
                category_name="Telegram",
                name="Telegram Stars",
                base_price=Decimal(str(stars["price_per_star"])),
                unit="star",
                min_qty=int(stars.get("min_amount", 50)),
                max_qty=int(stars.get("max_amount", 10000)),
                fields=[{"key": "telegram_username", "label": "Telegram @username", "type": "text"}],
                supplier_ref={},
            )
        premium = self._catalog_get("/telegram/premium")
        for plan in (premium or {}).get("plans", []):
            months = int(plan["months"])
            yield ProductData(
                id=f"tg-premium-{months}",
                kind="telegram_premium",
                category_id="telegram",
                category_name="Telegram",
                name=f"Telegram Premium — {months} мес.",
                base_price=Decimal(str(plan["price_usd"])),
                fields=[{"key": "telegram_username", "label": "Telegram @username", "type": "text"}],
                supplier_ref={"months": months},
            )

    def _steam_topup(self) -> Iterable[ProductData]:
        data = self._catalog_get("/steam-topup/rates")
        if not data:
            return
        rates = {str(k).upper(): str(v) for k, v in (data.get("rates") or {}).items()}
        yield ProductData(
            id="steam-topup",
            kind="steam_topup",
            category_id="steam",
            category_name="Steam",
            name="Пополнение Steam",
            # Цена за 1 USD, зачисленный на аккаунт: номинал минус скидка тарифа.
            base_price=(Decimal(1) - self._steam_discount / Decimal(100)),
            unit="usd",
            fields=[
                {"key": "steam_login", "label": "Логин Steam", "type": "text"},
                {"key": "currency", "label": "Валюта", "type": "select"},
                {"key": "amount", "label": "Сумма", "type": "number"},
            ],
            supplier_ref={"rates": rates, "rates_updated_at": data.get("updated_at"),
                          "min_usd": str(STEAM_MIN_USD), "max_usd": str(STEAM_MAX_USD)},
        )

    def _topups(self) -> Iterable[ProductData]:
        for cat in list(self._paged("/topups")):
            cat_id = str(cat["category_id"])
            data = self._catalog_get("/topups/offers", category_id=cat_id, include_ui=1)
            if not data:
                continue
            fields = [
                {"key": str(f["key"]), "label": str(f.get("label") or f["key"]), "type": str(f.get("type") or "text")}
                for f in data.get("fields", [])
            ]
            for offer in data.get("offers", []):
                yield ProductData(
                    id=_pid("topup", cat_id, str(offer["offer_id"])),
                    kind="topup",
                    category_id=cat_id,
                    category_name=str(data.get("name") or cat.get("name") or cat_id),
                    name=str(offer["name"]),
                    base_price=Decimal(str(offer["price_usd"])),
                    fields=fields,
                    supplier_ref={"category_id": cat_id, "offer_id": str(offer["offer_id"])},
                    # Картинка — обложка игры; у пакетов («25 алмазов») свои картинки не берём
                    image_url=self._image(cat, data),
                    region=pick_region(str(offer["name"]), offer, data, cat),
                )

    def _image(self, *sources: Any) -> str | None:
        img = pick_image(*sources, base=self._image_base)
        if img:
            return img
        for src in sources:
            if isinstance(src, dict) and src.get("appid"):
                return steam_cover(src["appid"])
        return None

    def _giftcards(self) -> Iterable[ProductData]:
        for cat in list(self._paged("/giftcards")):
            cat_id = str(cat["category_id"])
            data = self._catalog_get("/giftcards/cards", category_id=cat_id, include_ui=1)
            if not data:
                continue
            for offer in data.get("offers", []):
                yield ProductData(
                    id=_pid("gc", cat_id, str(offer["card_id"])),
                    kind="gift_card",
                    category_id=cat_id,
                    category_name=str(data.get("name") or cat.get("name") or cat_id),
                    name=str(offer["name"]),
                    base_price=Decimal(str(offer["price_usd"])),
                    min_qty=int(offer.get("min_order_quantity") or 1),
                    max_qty=min(int(offer.get("max_order_quantity") or 1), 100),
                    stock=int(offer["stock"]) if offer.get("stock") is not None else None,
                    supplier_ref={"category_id": cat_id, "card_id": str(offer["card_id"])},
                    image_url=self._image(cat, data),
                    region=pick_region(str(offer["name"]), offer, data, cat),
                )

    def _gamekeys(self) -> Iterable[ProductData]:
        """Ключи игр: GET /gamekeys → игры, GET /gamekeys/keys?game_id → ключи с ценой и наличием."""
        for game in list(self._paged("/gamekeys")):
            game_id = str(game.get("game_id") or "")
            if not game_id:
                continue
            data = self._catalog_get("/gamekeys/keys", game_id=game_id, include_ui=1)
            if not data:
                continue
            title = str(data.get("GameName") or data.get("game_name") or game.get("name") or game_id)
            region = data.get("region") or game.get("region")
            platform = str(data.get("platform") or game.get("platform") or "")
            restricted = bool(data.get("region_restriction", game.get("region_restriction")))
            for key in data.get("keys") or []:
                if not key.get("key_id"):  # без key_id заказать нельзя
                    continue
                stock = key.get("stock")
                if stock is not None and int(stock) <= 0:
                    continue
                yield ProductData(
                    id=_pid("gk", game_id, str(key["key_id"])),
                    kind="game_key",
                    category_id=game_id,
                    category_name=title,
                    name=str(key.get("name") or title),
                    base_price=Decimal(str(key["price_usd"])),
                    min_qty=max(int(key.get("min_order_quantity") or 1), 1),
                    max_qty=min(int(key.get("max_order_quantity") or 1), 100),
                    stock=int(stock) if stock is not None else None,
                    supplier_ref={"game_id": game_id, "key_id": str(key["key_id"]), "platform": platform,
                                  "region_restriction": restricted, "appid": data.get("appid") or game.get("appid")},
                    image_url=self._image(data, game),
                    region=pick_region(str(key.get("name") or ""), {"region": region} if region else {}),
                )

    # ── Заказы ────────────────────────────────────────────────

    def is_idempotent(self, kind: str) -> bool:
        return kind in _IDEMPOTENT_KINDS

    def create_order(
        self, product: dict[str, Any], quantity: int, fields: dict[str, str], idem_key: str
    ) -> SupplierOrder:
        kind = product["kind"]
        ref = product["supplier_ref"]
        headers = {"Idempotency-Key": idem_key}
        if kind == "telegram_stars":
            path, body = "/telegram/stars/buy", {
                "telegram_username": fields["telegram_username"],
                "quantity": quantity,
            }
        elif kind == "telegram_premium":
            path, body = "/telegram/premium/buy", {
                "telegram_username": fields["telegram_username"],
                "months": ref["months"],
            }
        elif kind == "steam_topup":
            path, body = "/steam-topup/order", {
                "steamLogin": fields["steam_login"],
                "currency": fields["currency"],
                "amount": fields["amount"],
            }
        elif kind == "steam_gift":
            path, body = "/steam-gifts/order", {
                "invite_url": fields["invite_url"],
                "sub_id": int(fields["sub_id"]),
                "app_id": int(fields["app_id"]),
                "region": fields["region"],
            }
        elif kind == "topup":
            path, body = "/topups/order", {
                "category_id": ref["category_id"],
                "offer_id": ref["offer_id"],
                "fields": fields,
            }
        elif kind == "gift_card":
            path, body = "/giftcards/order", {
                "category_id": ref["category_id"],
                "card_id": ref["card_id"],
                "quantity": quantity,
            }
        elif kind == "game_key":
            path, body = "/gamekeys/order", {
                "game_id": ref["game_id"],
                "key_id": ref["key_id"],
                "quantity": quantity,
            }
        else:
            raise SupplierRejected(f"вид товара {kind} не поддерживается")
        # Создание заказа не повторяем на 429 сами: решение о повторе — у воркера.
        data = self._request("POST", path, json=body, headers=headers, retry_429=False)
        order = data.get("order") or {}
        order_id = order.get("id") or data.get("order_id")
        raw = str(order.get("status") or data.get("status") or "processing")
        return SupplierOrder(order_id=str(order_id) if order_id else None, status=normalize_status(raw), raw_status=raw)

    def get_order(self, supplier_order_id: str) -> SupplierOrder:
        data = self._request("GET", f"/orders/{supplier_order_id}")
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        # Статус ищем везде, где поставщик может его написать
        raw = str(order.get("status") or order.get("state") or data.get("status") or data.get("state") or "")
        if order.get("refunded") is True or data.get("refunded") is True:
            raw = raw or "refunded"
        payload = order.get("payload")
        return SupplierOrder(
            order_id=supplier_order_id,
            status=normalize_status(raw),
            raw_status=raw,
            delivery=payload if isinstance(payload, dict) else ({"payload": payload} if payload else None),
            message=str(order.get("error") or order.get("message") or ""),
        )

    def steam_gift_games(self) -> list[dict[str, Any]]:
        # Каталог большой (~12 000 игр); просим сразу много, поставщик может обрезать.
        data = self._request("GET", "/steam-gifts/games", params={"limit": 20000}, background=True)
        return [{"appid": int(g["appid"]), "name": str(g["name"])} for g in data.get("games", []) if g.get("appid")]

    def steam_gift_offers(self, appid: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/steam-gifts/games/{int(appid)}")
        return list(data.get("offers") or [])

    def validate_id_categories(self) -> list[str]:
        """Игры, где поставщик проверяет аккаунт по ID (GET /topups/validate-id)."""
        data = self._catalog_get("/topups/validate-id") or {}
        items = next((data[k] for k in ("items", "categories", "games", "data", "supported")
                      if isinstance(data.get(k), list)), [])
        out = []
        for it in items:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict) and (it.get("category_id") or it.get("id")):
                out.append(str(it.get("category_id") or it.get("id")))
        return out

    def validate_account(self, category_id: str, fields: dict[str, str]) -> dict[str, Any]:
        data = self._request("POST", "/topups/validate-id", json={"category_id": category_id, "fields": fields})
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        return {
            "valid": bool(body.get("valid")),
            "player_name": body.get("player_name") or body.get("nickname") or body.get("username"),
            "region": body.get("region"),
            "message": body.get("message") or "",
        }

    def gamekey_regions(self, game_id: str) -> dict[str, Any]:
        """Где ключ активируется: {"region_type", "available": [{code, name}], "unavailable": [...]}."""
        data = self._request("GET", "/gamekeys/region-restriction", params={"game_id": game_id})
        return {"region_type": data.get("region_type") or "", "available": data.get("available") or [],
                "unavailable": data.get("unavailable") or [], "has_availability": bool(data.get("has_availability"))}

    def check_steam_login(self, login: str) -> bool:
        data = self._request("POST", "/steam-topup/check-login", json={"steamLogin": login})
        return bool(data.get("can_refill"))

    def balance(self) -> Decimal:
        data = self._request("GET", "/balance", background=True, wait=20)  # воркер не должен ждать
        return parse_balance(data)
