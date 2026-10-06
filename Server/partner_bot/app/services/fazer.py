"""Выдача через FazerCards (api.fzr.cards).

Для владельца это самая простая схема из всех: пополняешь баланс реселлера
один раз, дальше бот списывает с него сам. Сид-фраза кошелька не нужна,
авторизация — обычный ключ X-API-Key.

ВАЖНО про асинхронность: в документации сказано «balance debited
immediately; fulfillment is asynchronous». То есть успешный ответ на
покупку означает лишь, что деньги списаны и заказ принят, а не что звёзды
у получателя. Поэтому после покупки бот дожидается статуса заказа и только
тогда пишет клиенту «выполнено».

Цены сервис отдаёт в долларах, поэтому себестоимость в сомони считается
через курс доллара, заданный в панели.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import aiohttp

from app.config import settings
from app.services.fragment import (
    DeliveryError, DeliveryProvider, DeliveryResult, DeliveryUncertain, Recipient,
    SteamAccount,
)

log = logging.getLogger(__name__)

STARS_QUOTE = "/api/v2/telegram/stars"
PREMIUM_QUOTE = "/api/v2/telegram/premium"
STARS_BUY = "/api/v2/telegram/stars/buy"
PREMIUM_BUY = "/api/v2/telegram/premium/buy"

# Игры: пополнение игровых аккаунтов
TOPUP_OFFERS = "/api/v2/topups/offers"
TOPUP_VALIDATE = "/api/v2/topups/validate-id"
TOPUP_ORDER = "/api/v2/topups/order"
ORDER_ONE = "/api/v2/orders/{order_id}"

# Полный список категорий пополнения — это НЕ то же самое, что список
# validate-id: там перечислены только игры, у которых работает проверка
# ID, а продаются и остальные. Точный путь в выжимке из документации не
# указан, поэтому пробуем вероятные и запоминаем сработавший.
TOPUP_CATEGORIES = "/api/v2/topups/categories"
CATEGORY_CANDIDATES = [
    "/api/v2/topups/categories", "/api/v2/topups", "/api/v2/topups/catalog",
    "/api/v2/topups/list", "/api/v2/categories?type=topup",
    "/api/v2/catalog/topups",
]
#: Где в ответе может лежать сам список.
CATEGORY_LIST_KEYS = ["categories", "items", "data", "topups", "list", "result"]
#: Списки поставщик отдаёт страницами: по 200 за раз, не больше 500.
#: Берём максимум — чем меньше запросов, тем быстрее открывается каталог.
PAGE_SIZE = 500
#: Сколько страниц готовы прочесть. Двух сотен категорий на странице
#: хватает на любой каталог, а предел спасает от бесконечного круга.
PAGE_LIMIT = 20

# Пополнение кошелька Steam
STEAM_RATES = "/api/v2/steam-topup/rates"
STEAM_CHECK = "/api/v2/steam-topup/check-login"
STEAM_ORDER = "/api/v2/steam-topup/order"

#: Где в ответе может лежать курс/цена единицы пополнения Steam.
STEAM_RATE_KEYS = ["rate", "price", "price_usd", "usd", "usd_rate",
                   "price_per_unit", "value"]
#: Где — валюта кошелька.
STEAM_CURRENCY_KEYS = ["currency", "wallet_currency", "code", "name"]
#: Где — признак «такой логин существует».
STEAM_OK_KEYS = ["exists", "valid", "found", "ok", "success", "is_valid"]
#: Где — отображаемое имя аккаунта.
STEAM_NAME_KEYS = ["name", "nickname", "persona", "persona_name", "display_name",
                   "account_name", "steam_name"]

#: Слова в отказе проверки ID, означающие «дело не в игроке». Такой отказ
#: покупку не блокирует: поставщик пополняет по ID, а не по нашей проверке.
NOT_ABOUT_PLAYER = (
    "unsupported", "category", "offer", "field", "required", "permission",
    "forbidden", "unauthorized", "rate limit", "too many", "timeout",
    "internal", "server error", "temporarily", "maintenance",
)

# Сроки из документации сервиса: дольше ждать нельзя — клиент сидит
# в боте и смотрит на «пополнение идёт». Создание заказа короче прочего:
# там важнее быстро узнать отказ, чем дождаться медленного ответа.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=6)
ORDER_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=6)

# Формулировки статусов могут отличаться, поэтому распознаём широкий набор,
# а незнакомое считаем «ещё в работе»: соврать клиенту дороже, чем подождать.
DONE = {"completed", "complete", "done", "delivered", "success", "successful",
        "fulfilled", "finished", "paid", "ok"}
FAILED = {"failed", "fail", "error", "cancelled", "canceled", "rejected",
          "declined", "refunded", "expired"}
PENDING = {"pending", "processing", "in_progress", "inprogress", "queued",
           "new", "created", "accepted", "waiting", "running", "fulfilling"}


# Вероятные пути к балансу и заказам. Документация в руках владельца, но
# перебрать варианты ключом быстрее, чем сверять скриншоты вручную.
BALANCE_PATH = "/api/v2/balance"
BALANCE_CANDIDATES = [
    "/api/v2/account", "/api/v2/account/balance", "/api/v2/account/me",
    "/api/v2/account/info", "/api/v2/balance", "/api/v2/me",
    "/api/v2/user", "/api/v2/user/balance", "/api/v2/profile",
    "/api/v2/reseller", "/api/v2/reseller/balance", "/api/v2/wallet",
    "/api/v2/payment/balance", "/api/v2/subscription",
]
ORDER_LIST_CANDIDATES = [
    "/api/v2/orders", "/api/v2/order", "/api/v2/orders/list",
    "/api/v2/account/orders", "/api/v2/my/orders",
]
# Шаблоны одиночного заказа. Пробуются по очереди, рабочий запоминается.
ORDER_ONE_CANDIDATES = [
    "/api/v2/orders/{order_id}", "/api/v2/order/{order_id}",
    "/api/v2/orders/{order_id}/status", "/api/v2/account/orders/{order_id}",
]


def normalize_base(url: str) -> str:
    """Адрес сервиса без хвоста /api/v2.

    В документации базовым назван https://api.fzr.cards/api/v2, а все пути
    в коде уже начинаются с /api/v2. Скопировав адрес из документации в
    .env, владелец получил бы /api/v2/api/v2/... и «404» на всё подряд.
    """
    base = (url or "").strip().rstrip("/")
    for tail in ("/api/v2", "/api/v1", "/api"):
        if base.endswith(tail):
            base = base[: -len(tail)].rstrip("/")
            break
    return base


@dataclass
class CostEstimate:
    """Во что заказ обходится владельцу. Цены FazerCards уже в долларах."""
    quantity: int
    amount: str
    currency: str
    usd_total: Decimal
    usd_per_unit: Decimal
    usdt_per_ton: str = ""


def _first(holder, keys: list[str]):
    """Первое непустое значение из набора ключей."""
    if not isinstance(holder, dict):
        return None
    for key in keys:
        value = holder.get(key)
        if value not in (None, ""):
            return value
    return None


def _decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise DeliveryError(f"FazerCards вернул нечисловое поле {field}: {value!r}") from exc


class FazerProvider(DeliveryProvider):
    supports_name_lookup = False   # проверки юзернейма у сервиса нет
    instant = True                 # деньги списываются с баланса сразу

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        """api_key задаётся явно, когда товар идёт с чужого счёта: у каждого
        партнёра свой ключ, и заказ должен списаться именно с его баланса."""
        self.api_key = (api_key or settings.fazer_api_key).strip()
        if not self.api_key:
            raise RuntimeError("Не задан FAZER_API_KEY")
        self._base = normalize_base(base_url or settings.fazer_base_url)
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------ транспорт

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers={
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _explain(data: dict, status: int) -> str:
        for key in ("error", "message", "blockReason", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                code = data.get("code")
                return f"{value.strip()}" + (f" [{code}]" if code else "")
        return str(data)[:250] or f"HTTP {status}"

    async def _request(
        self, method: str, path: str, payload: dict | None = None, *,
        safe: bool = False, headers: dict | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        urgent: bool = False,
    ) -> dict:
        """safe=True — запрос ничего не меняет, поэтому сетевой сбой можно
        считать обычной ошибкой, а не неопределённым исходом.

        urgent=True — за запросом стоит живой клиент: он пойдёт вперёд
        фоновых опросов, когда к поставщику выстроилась очередь.
        """
        from app.services.ratelimit import Busy, for_key

        try:
            waited = await for_key(self.api_key).take(urgent=urgent)
        except Busy as exc:
            # Запрос не ушёл вовсе — значит исход определён, и деньги
            # вернутся обычным путём, без разбирательств.
            raise DeliveryError(str(exc)) from exc
        if waited > 1:
            log.info("Очередь к поставщику: ждали %.1f с (%s %s)",
                     waited, method, path)

        session = await self._get_session()
        try:
            async with session.request(
                method, self._base + path, json=payload, headers=headers,
                timeout=timeout,
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 — при сбое может прийти HTML
                    data = {"raw": (await resp.text())[:250]}
                if not isinstance(data, dict):
                    data = {"response": data}

                if resp.status in (502, 503) or resp.status >= 500:
                    message = self._explain(data, resp.status)
                    raise (DeliveryError if safe else DeliveryUncertain)(
                        f"FazerCards недоступен ({resp.status}): {message}"
                    )
                if resp.status >= 400:
                    raise DeliveryError(self._explain(data, resp.status))

                # Сервис оборачивает данные в {ok: ...}; ok=false — отказ.
                if data.get("ok") is False:
                    raise DeliveryError(self._explain(data, resp.status))
                return data

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise (DeliveryError if safe else DeliveryUncertain)(
                f"Нет связи с FazerCards: {exc}"
            ) from exc

    # --------------------------------------------------------------- цены

    async def stars_quote(self) -> dict:
        return await self._request("GET", STARS_QUOTE, safe=True)

    async def premium_quote(self) -> dict:
        return await self._request("GET", PREMIUM_QUOTE, safe=True)

    async def cost_estimate(self, product_type: str, amount: int) -> CostEstimate:
        """Себестоимость заказа в долларах — цены сервис отдаёт сразу в USD."""
        if amount <= 0:
            raise DeliveryError("Количество должно быть больше нуля.")

        if product_type == "stars":
            data = await self.stars_quote()
            per_unit = _decimal(data.get("price_per_star"), "price_per_star")
            total = per_unit * amount
        else:
            data = await self.premium_quote()
            plans = {int(p["months"]): p["price_usd"] for p in data.get("plans", [])}
            if amount not in plans:
                raise DeliveryError(
                    f"FazerCards не продаёт Premium на {amount} мес. "
                    f"Доступно: {', '.join(map(str, sorted(plans))) or '—'}"
                )
            total = _decimal(plans[amount], "price_usd")
            per_unit = total / amount

        return CostEstimate(
            quantity=amount, amount=f"{total:.4f}", currency="usd",
            usd_total=total, usd_per_unit=per_unit,
        )

    # --------------------------------------------------------------- игры

    async def game_offers(self, category_id: str) -> list[dict]:
        """Пакеты пополнения для игры: что и почём продаёт сервис."""
        data = await self._request(
            "GET", f"{TOPUP_OFFERS}?category_id={category_id}&include_ui=1",
            safe=True, urgent=True,
        )
        offers = data.get("offers")
        if not isinstance(offers, list):
            raise DeliveryError(f"Сервис не вернул пакеты: {str(data)[:200]}")
        return [
            {
                "offer_id": str(offer.get("offer_id") or ""),
                "name": str(offer.get("name") or "пакет"),
                "usd": _decimal(offer.get("price_usd"), "price_usd"),
                "raw": offer,
            }
            for offer in offers
            if isinstance(offer, dict) and offer.get("offer_id")
        ]

    async def game_categories(self) -> list[dict]:
        """Все категории пополнения: что вообще можно продавать.

        Список validate-id для этого не годится — в нём только игры
        с проверкой ID. Именно поэтому Free Fire мог не найтись, а заказ
        падал с «Unknown or unavailable category_id».

        Список выдаётся страницами: без дочитывания видна только первая,
        и игра со второй страницы выглядит так, будто её у поставщика
        нет вовсе.
        """
        from app import runtime

        paths = [runtime.get("fazer_topups_path")] if runtime.get(
            "fazer_topups_path") else []
        paths += [p for p in CATEGORY_CANDIDATES if p not in paths]

        for path in paths:
            items = await self._all_pages(path)
            if not items:
                continue
            if path != runtime.get("fazer_topups_path"):
                await self._remember("fazer_topups_path", path)
            return items

        log.info("Игры: список категорий не нашёлся ни по одному адресу")
        return []

    async def _all_pages(self, path: str) -> list[dict]:
        """Дочитать список до конца, идя по курсору.

        Страниц берём ограниченное число: курсор, который перестал
        меняться, увёл бы обход в бесконечный круг.
        """
        found: list[dict] = []
        seen: set[str] = set()
        cursor = ""

        for _ in range(PAGE_LIMIT):
            query = f"limit={PAGE_SIZE}" + (f"&cursor={cursor}" if cursor else "")
            joiner = "&" if "?" in path else "?"
            status, data = await self._get_raw(f"{path}{joiner}{query}")
            if not self._looks_ok(status, data):
                return found

            page = _category_list(data)
            if not page:
                return found
            found.extend(page)

            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            nxt = str(meta.get("next_cursor") or "")
            if not meta.get("has_more") or not nxt or nxt in seen:
                break
            seen.add(nxt)
            cursor = nxt

        return found

    async def game_catalog(self) -> list[dict]:
        """Игры, у которых сервис умеет проверять ID, и какие поля им нужны."""
        data = await self._request("GET", TOPUP_VALIDATE, safe=True)
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [
            {
                "category_id": str(item.get("category_id") or ""),
                "name": str(item.get("name") or item.get("category_id") or ""),
                "fields": item.get("fields") or [],
            }
            for item in items
            if isinstance(item, dict) and item.get("category_id")
        ]

    async def validate_game_id(
        self, category_id: str, fields: dict[str, str],
    ) -> tuple[str | None, str]:
        """Проверить ID игрока. Возвращает (ник, вердикт).

        Вердикт: ok — ID верный, bad — сервис сказал «неверный»,
        unknown — проверить не удалось. Разница важна: покупку блокирует
        только явный отказ, а на «не смог» продавать всё равно можно —
        пополнение идёт по ID, ник нужен лишь для сверки глазами.
        """
        try:
            data = await self._request(
                "POST", TOPUP_VALIDATE,
                {"category_id": category_id, "fields": fields}, safe=True,
                urgent=True,
            )
        except DeliveryError as exc:
            text = str(exc).lower()
            # Сначала отсекаем отказы, которые вообще не про игрока:
            # «неизвестная категория», «не хватает поля». Принять их за
            # «такого игрока нет» — значит соврать клиенту и потерять
            # покупку на ровном месте.
            if any(word in text for word in NOT_ABOUT_PLAYER):
                return None, "unknown"
            if "invalid" in text or "not found" in text or "не найден" in text:
                return None, "bad"
            log.info("Игры: проверка ID не прошла — %s", exc)
            return None, "unknown"

        if str(data.get("status") or "").lower() == "unsupported":
            return None, "unknown"
        name = data.get("player_name") or data.get("nickname")
        if data.get("ok") is True and name:
            return str(name), "ok"
        if data.get("ok") is False:
            return None, "bad"
        return (str(name) if name else None), "unknown"

    async def order_game(
        self, *, category_id: str, offer_id: str, fields: dict[str, str],
        quantity: int, idempotency_key: str,
    ) -> dict:
        """Создать заказ. Ответ ok:true означает лишь «принят», не «доставлен»."""
        data = await self._request(
            "POST", TOPUP_ORDER,
            {
                "category_id": category_id, "offer_id": offer_id,
                "fields": fields, "quantity": quantity,
            },
            headers={"Idempotency-Key": idempotency_key}, urgent=True,
            timeout=ORDER_TIMEOUT,
        )
        order = data.get("order")
        if not isinstance(order, dict):
            raise DeliveryUncertain(
                f"Сервис принял заказ, но не вернул его данные: {str(data)[:200]}"
            )
        return order

    async def order_status(self, order_id: str) -> dict | None:
        """Статус заказа по номеру сервиса."""
        try:
            data = await self._request(
                "GET", ORDER_ONE.format(order_id=order_id), safe=True,
            )
        except DeliveryError as exc:
            log.info("Игры: статус заказа %s не прочитался — %s", order_id, exc)
            return None
        order = data.get("order")
        return order if isinstance(order, dict) else data

    # --------------------------------------------------------------- Steam

    async def steam_rates(self) -> dict:
        return await self._request("GET", STEAM_RATES, safe=True)

    async def steam_rate(self) -> tuple[Decimal, str]:
        """Во что обходится единица пополнения Steam и в какой она валюте.

        Имена полей в документации не сверялись, поэтому берём первое
        подходящее из набора — как это уже сделано с адресами заказов.
        """
        data = await self.steam_rates()
        holder = data
        for key in ("rate", "rates", "data", "result"):
            value = data.get(key)
            if isinstance(value, dict):
                holder = value
                break
            if isinstance(value, list) and value and isinstance(value[0], dict):
                holder = value[0]
                break

        raw = _first(holder, STEAM_RATE_KEYS)
        if raw is None:
            raise DeliveryError(
                f"FazerCards не вернул курс Steam: {str(data)[:200]}"
            )
        currency = str(_first(holder, STEAM_CURRENCY_KEYS) or "RUB").upper()
        return _decimal(raw, "steam rate"), currency

    async def check_steam_login(self, login: str) -> SteamAccount | None:
        """Проверить логин до оплаты. None — сервис ответить не смог."""
        try:
            data = await self._request(
                "POST", STEAM_CHECK, {"login": login}, safe=True, urgent=True,
            )
        except DeliveryError as exc:
            log.info("Steam: логин %s не проверился — %s", login, exc)
            return None

        holder = data.get("account") if isinstance(data.get("account"), dict) else data
        flag = _first(holder, STEAM_OK_KEYS)
        if flag is None:
            # Ответ есть, но признака нет — не выдаём его за проверку.
            return None
        return SteamAccount(
            login=login,
            exists=bool(flag) and str(flag).lower() not in ("0", "false", "no"),
            name=str(_first(holder, STEAM_NAME_KEYS) or ""),
            raw=data,
        )

    async def deliver_steam(self, login: str, amount: int) -> DeliveryResult:
        return await self._buy(STEAM_ORDER, {"login": login, "amount": amount})

    async def limits(self) -> tuple[int, int]:
        """Разрешённый диапазон количества звёзд у сервиса."""
        data = await self.stars_quote()
        return int(data.get("min_amount") or 50), int(data.get("max_amount") or 10_000)

    # ------------------------------------------------------------- выдача

    async def deliver_stars(self, username: str, amount: int) -> DeliveryResult:
        return await self._buy(STARS_BUY, {
            "telegram_username": username, "quantity": amount,
        })

    async def deliver_premium(self, username: str, months: int) -> DeliveryResult:
        return await self._buy(PREMIUM_BUY, {
            "telegram_username": username, "months": months,
        })

    async def _buy(self, path: str, payload: dict) -> DeliveryResult:
        data = await self._request("POST", path, payload, urgent=True)
        order = data.get("order")
        if not isinstance(order, dict):
            raise DeliveryUncertain(
                f"FazerCards принял заказ, но не вернул его данные: {str(data)[:200]}"
            )

        order_id = _order_id(order)
        status = _status(order)
        log.info("FazerCards: заказ %s принят, статус %s", order_id, status or "—")

        # Деньги уже списаны с баланса реселлера. Если статус сразу
        # финальный — незачем опрашивать.
        if status in DONE:
            return DeliveryResult(order_id=order_id, raw=order)
        if status in FAILED:
            raise DeliveryError(f"Заказ {order_id} отклонён: {_reason(order)}")

        if order_id is None:
            raise DeliveryUncertain(
                "FazerCards не вернул номер заказа — проверить выдачу нечем."
            )
        return DeliveryResult(order_id=order_id, raw=await self._await_order(order_id))

    async def _await_order(self, order_id: str) -> dict:
        """Дождаться финального статуса заказа.

        Путь к статусу задаётся в настройках: раздел Orders в документации
        сервиса ещё не сверялся, и угадывать молча нельзя.
        """
        interval = max(settings.task_poll_interval, 2)
        deadline = settings.task_poll_timeout
        waited = 0
        last: dict = {}
        unknown: set[str] = set()

        while waited < deadline:
            await asyncio.sleep(interval)
            waited += interval
            order = await self._read_order(order_id)
            if order is None:
                log.warning("Заказ %s: статус не прочитался", order_id)
                continue
            last = order
            status = _status(order)

            if status in DONE:
                log.info("Заказ %s выполнен за ~%s сек", order_id, waited)
                return order
            if status in FAILED:
                raise DeliveryError(f"Заказ {order_id}: {_reason(order)}")
            if status and status not in PENDING and status not in unknown:
                unknown.add(status)
                log.warning("Заказ %s: незнакомый статус %r — жду дальше",
                            order_id, status)

        raise DeliveryUncertain(
            f"Заказ {order_id} не завершился за {deadline} сек "
            f"(последний статус: {_status(last) or '—'})"
        )

    async def _read_order(self, order_id: str) -> dict | None:
        """Прочитать заказ: сперва известным адресом, потом перебором,
        а если одиночного адреса нет — поиском в списке заказов."""
        known = self.order_path()
        templates = ([known] if known else []) + [
            t for t in ORDER_ONE_CANDIDATES if t != known
        ]

        for template in templates:
            status, data = await self._get_raw(template.format(order_id=order_id))
            if not self._looks_ok(status, data):
                continue
            order = data.get("order") if isinstance(data.get("order"), dict) else data
            if _order_id(order) or _status(order):
                if template != known:
                    await self._remember("fazer_order_path", template)
                    log.info("FazerCards: адрес заказа найден — %s", template)
                return order

        # Одиночного адреса нет — ищем свой заказ в общем списке.
        for path in ORDER_LIST_CANDIDATES:
            status, data = await self._get_raw(path)
            if not self._looks_ok(status, data):
                continue
            found = _find_in_list(data, order_id)
            if found is not None:
                log.info("Заказ %s найден в списке %s", order_id, path)
                return found
        return None

    # ------------------------------------------------------------- прочее

    async def resolve_recipient(self, username: str) -> Recipient | None:
        """Проверки юзернейма у сервиса нет — возвращаем непроверенного,
        и бот честно скажет об этом покупателю."""
        return Recipient(username=username, name="", verified=False)

    @staticmethod
    def balance_path() -> str:
        from app import runtime

        return runtime.get("fazer_balance_path") or settings.fazer_balance_path

    @staticmethod
    def order_path() -> str:
        from app import runtime

        return runtime.get("fazer_order_path") or settings.fazer_order_path

    async def _get_raw(self, path: str) -> tuple[int, dict]:
        """GET без исключений: нужен для перебора адресов."""
        session = await self._get_session()
        try:
            async with session.get(self._base + path) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    data = {}
                if not isinstance(data, dict):
                    data = {"data": data}
                return resp.status, data
        except (TimeoutError, aiohttp.ClientError) as exc:
            return 0, {"error": str(exc)}

    @staticmethod
    def _looks_ok(status: int, data: dict) -> bool:
        return status == 200 and data.get("ok") is not False

    async def _remember(self, key: str, value: str) -> None:
        """Запомнить найденный адрес, чтобы больше не перебирать."""
        from app import runtime

        try:
            from app import db

            conn = await db.connect()
            try:
                await runtime.set_value(conn, key, value)
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — не смогли сохранить, не беда
            log.warning("Адрес %s не сохранился: %s", key, exc)
            runtime._cache[key] = value

    async def find_balance(self) -> tuple[str, dict] | None:
        """Найти рабочий адрес баланса, начиная с уже известного."""
        known = self.balance_path()
        for path in ([known] if known else []) + [
            p for p in BALANCE_CANDIDATES if p != known
        ]:
            status, data = await self._get_raw(path)
            if self._looks_ok(status, data) and _balance_of(data) is not None:
                if path != known:
                    await self._remember("fazer_balance_path", path)
                    log.info("FazerCards: адрес баланса найден — %s", path)
                return path, data
        return None

    async def probe_paths(self) -> dict:
        """Перебрать вероятные адреса и вернуть те, что отвечают.

        Запросы только читающие, ничего не меняют и денег не тратят.
        """
        found: dict[str, list[tuple[str, str]]] = {"balance": [], "orders": []}
        session = await self._get_session()

        async def try_path(path: str) -> tuple[int, dict]:
            try:
                async with session.get(self._base + path) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001
                        data = {}
                    return resp.status, data if isinstance(data, dict) else {"data": data}
            except (TimeoutError, aiohttp.ClientError) as exc:
                return 0, {"error": str(exc)}

        for path in BALANCE_CANDIDATES:
            status, data = await try_path(path)
            if status == 200 and data.get("ok") is not False:
                found["balance"].append((path, _preview(data)))
            await asyncio.sleep(0.2)   # не долбим сервис пачкой запросов

        for path in ORDER_LIST_CANDIDATES:
            status, data = await try_path(path)
            if status == 200 and data.get("ok") is not False:
                found["orders"].append((path, _preview(data)))
            await asyncio.sleep(0.2)

        return found

    async def get_balance(self) -> str:
        # Документация называет точный адрес; перебор остался запасным
        # вариантом на случай, если сервис его поменяет.
        try:
            data = await self._request("GET", BALANCE_PATH, safe=True)
            value = _balance_of(data)
            if value not in (None, ""):
                return f"{value} {data.get('currency') or 'USD'}"
        except (DeliveryError, DeliveryUncertain) as exc:
            log.info("Баланс по прямому адресу не пришёл: %s", exc)

        found = await self.find_balance()
        if found is None:
            raise DeliveryError(
                "Не нашёл адрес баланса. Нажмите «🔍 Найти адреса API» "
                "или пришлите раздел Account документации."
            )
        _, data = found
        value = _balance_of(data)
        currency = data.get("currency") or (data.get("account") or {}).get("currency") or "USD"
        return f"{value} {currency}"

    async def healthcheck(self) -> dict:
        steps: list[tuple[str, str]] = []
        try:
            stars = await self.stars_quote()
        except (DeliveryError, DeliveryUncertain) as exc:
            steps.append(("Ключ FazerCards", f"❌ {exc}"))
            return {"ok": False, "mode": "fazer", "steps": steps, "error": str(exc)}

        steps.append(("Ключ FazerCards", "✅ принят"))
        steps.append(("Цена звезды", f"✅ ${stars.get('price_per_star')} "
                                     f"({stars.get('min_amount')}–{stars.get('max_amount')} шт.)"))

        try:
            plans = (await self.premium_quote()).get("plans", [])
            steps.append(("Premium", "✅ " + ", ".join(
                f"{p['months']} мес — ${p['price_usd']}" for p in plans) or "—"))
        except (DeliveryError, DeliveryUncertain) as exc:
            steps.append(("Premium", f"⚠️ {exc}"))

        found = await self.find_balance()
        if found is None:
            steps.append(("Баланс реселлера", "⚠️ адрес не найден — нажмите «Найти адреса API»"))
        else:
            path, data = found
            steps.append((
                "Баланс реселлера",
                f"✅ {_balance_of(data)} {data.get('currency', 'USD')}  ({path})",
            ))

        # Проверяем не «записан ли путь», а можем ли мы вообще читать заказы:
        # без этого бот не подтвердит выдачу и будет дёргать владельца.
        reachable = await self._orders_reachable()
        if reachable:
            steps.append(("Чтение заказов", f"✅ {reachable}"))
            ok = True
        else:
            steps.append((
                "Чтение заказов",
                "❌ ни один адрес не отвечает — бот не сможет подтвердить "
                "выдачу и отдаст каждый заказ вам на проверку",
            ))
            ok = False
        return {
            "ok": ok, "mode": "fazer", "steps": steps,
            "error": "" if ok else "Не удалось найти адрес заказов",
        }

    async def _orders_reachable(self) -> str:
        """Есть ли вообще способ прочитать заказы. Возвращает рабочий адрес."""
        for path in ORDER_LIST_CANDIDATES:
            status, data = await self._get_raw(path)
            if self._looks_ok(status, data):
                return path
        # Списка нет — возможно, доступен только одиночный заказ. Проверить
        # его без настоящего номера нельзя, поэтому честно говорим «не знаем».
        return ""


def _category_list(data: dict) -> list[dict]:
    """Вытащить категории из ответа, как бы сервис их ни обернул."""
    raw = None
    for key in CATEGORY_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            raw = value
            break
    if raw is None:
        return []

    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = _first(item, ["category_id", "id", "slug", "code", "key"])
        if not code:
            continue
        name = _first(item, ["name", "title", "label", "display_name"])
        fields = item.get("fields") if isinstance(item.get("fields"), list) else []
        out.append({
            "category_id": str(code),
            "name": str(name or code),
            "fields": fields,
        })
    return out


def _order_id(order: dict) -> str | None:
    for key in ("id", "order_id", "uuid", "number", "orderId"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _status(order: dict) -> str:
    if not isinstance(order, dict):
        return ""
    for key in ("status", "state", "order_status"):
        value = order.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _reason(order: dict) -> str:
    for key in ("error", "reason", "failure_reason", "message", "comment"):
        value = order.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _status(order) or "причина не указана"


def _preview(data: dict) -> str:
    """Короткая выжимка ответа — чтобы владелец узнал нужный путь по виду."""
    interesting = {}
    for key in ("balance", "amount", "available", "funds", "currency",
                "orders", "items", "data", "total", "count"):
        if key in data:
            interesting[key] = data[key]
    if not interesting and isinstance(data.get("account"), dict):
        interesting = data["account"]
    text = str(interesting or data)
    return text[:160]


def _balance_of(data: dict):
    """Достать сумму баланса из ответа любой формы."""
    for source in (data, data.get("account"), data.get("data"), data.get("user")):
        if not isinstance(source, dict):
            continue
        for key in ("balance", "amount", "available", "funds", "credit"):
            value = source.get(key)
            if isinstance(value, (int, float, str)) and str(value).strip():
                return value
            if isinstance(value, dict):
                for inner in ("amount", "value", "total"):
                    if value.get(inner) is not None:
                        return value[inner]
    return None


def _find_in_list(data: dict, order_id: str) -> dict | None:
    """Найти заказ по номеру в списке заказов."""
    for key in ("orders", "items", "data", "results", "list"):
        rows = data.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and str(_order_id(row) or "") == str(order_id):
                    return row
    return None
