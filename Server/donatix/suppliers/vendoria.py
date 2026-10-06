"""Поставщик Vendoria (vendoria.amadeustech.dev) — берём только выбранные игры:
по умолчанию Standoff 2 и Clash of Clans.

Как устроен их каталог: игра (service) → категории → товары; способ покупки — «форма»
(поля, которые заполняет клиент), у каждой пары (товар, форма) своя цена в USD.
У нас одна пара = один товар: vd-<productId>-<formId>.

Формы, где продавец просит у клиента код подтверждения (hasRequest), Google-подтверждение
(hasGooglePrompt) или скриншот (image), пропускаем — через наш сайт и ботов их не пройти.

Токен магазина — только на сервере (DONATIX_VENDORIA_TOKEN, вид «0:abcdef…»).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Iterable

import httpx

from .base import ProductData, SupplierOrder, SupplierRejected, SupplierUnavailable

log = logging.getLogger(__name__)

PREFIX = "vd"                  # товар vd-<productId>-<formId>; заказ vd:<orderId>
CATEGORY_PREFIX = "vd_"

REASONS = {   # почему Vendoria отменила заказ — понятным языком для клиента
    "wrong_form_data": "Неверные данные аккаунта — проверьте и оформите заново.",
    "wrong_field": "Неверно заполнено поле «{field}» — проверьте и оформите заново.",
    "item_unavailable": "Товар временно недоступен.",
    "price_mismatch": "Цена товара изменилась — оформите заказ заново.",
    "game_not_linked": "Игровой аккаунт не привязан.",
    "wrong_platform": "Заказ оформлен не для той платформы.",
    "two_factor_required": "Нужно включить двухфакторную защиту аккаунта.",
    "confirmation_not_received": "Подтверждение не получено.",
}


def _status(raw: str) -> str:
    raw = (raw or "").lower()
    if raw == "completed":
        return "completed"
    if raw == "cancelled":
        return "failed"
    return "processing"            # pending, cancellation_requested


class VendoriaSupplier:
    name = "Vendoria"
    id_prefix = f"{PREFIX}-"

    def __init__(self, token: str, base_url: str = "https://vendoria.amadeustech.dev", *,
                 only_games: list[str] | None = None, transport: httpx.BaseTransport | None = None):
        if not token:
            raise ValueError("DONATIX_VENDORIA_TOKEN не задан")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Shop {token}", "Accept": "application/json", "Accept-Language": "ru"},
            timeout=httpx.Timeout(40, connect=15), transport=transport,
        )
        self._only = [g.strip().lower() for g in (only_games or []) if g.strip()]

    # ── HTTP ────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                 retries: int = 2) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                last = SupplierUnavailable(f"{method} {path}: {exc}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429 and attempt < retries:
                wait = resp.headers.get("Retry-After")
                time.sleep(min(float(wait), 30) if wait and wait.isdigit() else 2 * (attempt + 1))
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 500 or resp.status_code in (408, 429):
                raise SupplierUnavailable(f"{method} {path}: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                msg = str((data or {}).get("message") or (data or {}).get("error") or f"HTTP {resp.status_code}") \
                    if isinstance(data, dict) else f"HTTP {resp.status_code}"
                code = "insufficient_balance" if resp.status_code == 402 or "insufficient balance" in msg.lower() \
                    else ""
                raise SupplierRejected(msg, code=code, http_status=resp.status_code)
            return data
        raise last or SupplierUnavailable(f"{method} {path}")

    def icon_url(self, icon: str | None) -> str | None:
        return f"{self.base_url}/api/icons/{icon}" if icon else None

    # ── Каталог ─────────────────────────────────────────────
    def _wanted(self, service: dict[str, Any]) -> bool:
        if service.get("isClosed"):
            return False
        if not self._only:
            return True
        names = f"{service.get('name', '')} {service.get('originalName', '')}".lower()
        return any(g in names for g in self._only)

    @staticmethod
    def _fields(form: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Поля формы для нашей страницы покупки. None — форма нам не подходит."""
        if form.get("hasRequest") or form.get("hasGooglePrompt") or form.get("isArchived"):
            return None
        out: list[dict[str, Any]] = []
        for f in form.get("fields") or []:
            ftype = f.get("type")
            if ftype == "image":
                return None
            if not f.get("required"):
                continue   # необязательные поля не спрашиваем
            spec: dict[str, Any] = {"key": str(f.get("id")), "label": str(f.get("label") or f.get("id")),
                                    "type": "select" if ftype == "select" else "text"}
            if ftype == "select":
                spec["options"] = [str(o) for o in (f.get("options") or [])]
            elif f.get("regex"):
                spec["regex"] = str(f["regex"])
            out.append(spec)
        return out

    def fetch_catalog(self) -> Iterable[ProductData]:
        services = {s["id"]: s for s in self._request("GET", "/api/services") or [] if self._wanted(s)}
        if not services:
            return
        cats = {c["id"]: c for c in self._request("GET", "/api/categories") or [] if c.get("serviceId") in services}
        forms: dict[int, dict[str, Any]] = {}
        for f in self._request("GET", "/api/forms") or []:
            if f.get("serviceId") in services and self._fields(f) is not None:
                forms[f["id"]] = f
        per_service: dict[int, int] = {}
        for c in cats.values():
            per_service[c["serviceId"]] = per_service.get(c["serviceId"], 0) + 1
        for p in self._request("GET", "/api/products", params={"prices": "true"}) or []:
            cat = cats.get(p.get("categoryId"))
            if cat is None:
                continue
            service = services[cat["serviceId"]]
            usable = [(int(fid), price) for fid, price in (p.get("prices") or {}).items()
                      if price is not None and int(fid) in forms]
            for fid, price in usable:
                form = forms[fid]
                name = str(p.get("name") or p.get("id"))
                if per_service.get(cat["serviceId"], 0) > 1 and str(cat.get("name", "")).lower() not in name.lower():
                    name = f"{cat['name']} · {name}"
                if len(usable) > 1:
                    name = f"{name} ({form.get('name')})"
                yield ProductData(
                    id=f"{PREFIX}-{p['id']}-{fid}",
                    kind="topup",
                    category_id=f"{CATEGORY_PREFIX}{service['id']}",
                    category_name=str(service.get("name") or service.get("originalName") or "Vendoria"),
                    name=name,
                    base_price=Decimal(str(price)),
                    unit="item",
                    fields=self._fields(form) or [],
                    image_url=self.icon_url(p.get("icon")) or self.icon_url(service.get("icon")),
                    region=None,
                    supplier_ref={"provider": "vendoria", "product_id": int(p["id"]), "form_id": fid,
                                  "service_id": service["id"]},
                )

    # ── Заказы ──────────────────────────────────────────────
    def create_order(self, product: dict[str, Any], quantity: int, fields: dict[str, str],
                     idem_key: str) -> SupplierOrder:
        ref = product["supplier_ref"]
        if ref.get("provider") != "vendoria":
            raise SupplierRejected("не товар Vendoria")
        # поля уже проверены при оформлении (orders._clean_fields: список вариантов и формат)
        delivery = {spec["key"]: str(fields.get(spec["key"], "")).strip() for spec in product.get("fields") or []}
        body = {"productId": ref["product_id"], "quantity": int(quantity or 1), "formId": ref["form_id"],
                "deliveryData": delivery, "shopOrderId": idem_key[:64]}
        data = self._request("POST", "/api/orders", json=body, retries=0)   # заказ сами не повторяем
        order = data.get("order", data) if isinstance(data, dict) else {}
        oid = order.get("id")
        raw = str(order.get("status") or "pending")
        return SupplierOrder(order_id=f"{PREFIX}:{oid}" if oid is not None else None,
                             status=_status(raw), raw_status=raw, delivery=self._delivery(order),
                             message=self._reason(order))

    def get_order(self, supplier_order_id: str) -> SupplierOrder:
        oid = supplier_order_id.split(":", 1)[1] if supplier_order_id.startswith(f"{PREFIX}:") else supplier_order_id
        order = self._request("GET", f"/api/orders/{oid}")
        raw = str(order.get("status") or "")
        return SupplierOrder(order_id=supplier_order_id, status=_status(raw), raw_status=raw,
                             delivery=self._delivery(order), message=self._reason(order))

    @staticmethod
    def _delivery(order: dict[str, Any]) -> dict[str, Any] | None:
        if str(order.get("status")) != "completed":
            return None
        return {"codes": [order["cdkey"]]} if order.get("cdkey") else {"message": "Зачислено на аккаунт"}

    @staticmethod
    def _reason(order: dict[str, Any]) -> str:
        reason = order.get("cancellationReason") or {}
        text = REASONS.get(reason.get("type", ""), "")
        return text.format(field=reason.get("field", "")) if text else ""

    def balance(self) -> Decimal:
        data = self._request("GET", "/api/shop/balance")
        return Decimal(str(data.get("balance", 0)))

    def is_idempotent(self, kind: str) -> bool:
        return False   # ключа идемпотентности у Vendoria нет — заказ сами не повторяем

    # ── Не нужно для Vendoria ───────────────────────────────
    def validate_id_categories(self) -> list[str]:
        return []

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
        return (product.get("supplier_ref") or {}).get("provider") == "vendoria"

    def handles_order_id(self, supplier_order_id: str) -> bool:
        return bool(supplier_order_id) and supplier_order_id.startswith(f"{PREFIX}:")
