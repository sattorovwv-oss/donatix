"""Выдача звёзд и Premium через шлюз ApiFragment (apifragment.online).

Особенность этого API: он асинхронный. POST /stars возвращает не «выдано»,
а только task_id и статус accepted — «задача поставлена в очередь». Реальный
результат узнаётся опросом GET /task/{task_id}. Поэтому успешный ответ на
POST здесь НЕ означает доставку, и считать его доставкой нельзя: клиент
получил бы «заказ выполнен» без звёзд.

Авторизация двухслойная:
  • Bearer-токен в заголовке — на каждый запрос;
  • один раз POST /cookies/login с сид-фразой кошелька — шлюз логинится
    на Fragment и хранит сессию у себя.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

LOGIN_PATH = "/cookies/login"
STARS_PATH = "/stars"
PREMIUM_PATH = "/premium"
TON_PATH = "/ton"
TASK_PATH = "/task/{task_id}"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Словари статусов задачи. Формулировки у сервиса могут отличаться, поэтому
# распознаём широкий набор, а всё незнакомое считаем «ещё в работе» и в
# конце — неопределённым исходом: лучше позвать админа, чем соврать клиенту.
DONE_STATUSES = {
    "completed", "complete", "success", "successful", "succeeded",
    "done", "ok", "finished", "delivered", "sent", "paid",
}
FAILED_STATUSES = {
    "failed", "fail", "error", "cancelled", "canceled",
    "rejected", "declined", "expired", "insufficient_funds",
}
PENDING_STATUSES = {
    "pending", "processing", "in_progress", "inprogress", "queued",
    "accepted", "new", "running", "waiting", "created", "started",
}


class DeliveryError(Exception):
    """Шлюз явно отказал: нет средств, нет получателя, плохой запрос.

    Точно известно, что выдачи НЕ было, поэтому деньги можно вернуть сразу.
    """


class DeliveryUncertain(Exception):
    """Исход неизвестен: таймаут, обрыв связи, 5xx, задача так и не завершилась.

    Автовозврат тут опасен — можно вернуть деньги за реально отправленные
    звёзды, — поэтому такой заказ уходит админу на ручную проверку.
    """


@dataclass
class DeliveryResult:
    order_id: str | None
    raw: dict


@dataclass
class Recipient:
    """Получатель. name заполняется, только если провайдер умеет его узнать."""
    username: str
    name: str = ""
    verified: bool = False

    @property
    def display(self) -> str:
        return self.name or f"@{self.username}"


@dataclass
class SteamAccount:
    """Ответ проверки логина Steam."""
    login: str
    exists: bool
    name: str = ""          # отображаемое имя, если сервис его отдал
    raw: dict | None = None

    @property
    def display(self) -> str:
        return self.name or self.login


class DeliveryProvider:
    #: Умеет ли провайдер показывать имя аккаунта до оплаты.
    supports_name_lookup: bool = False
    #: Уходит ли товар сразу. False — выдача занимает минуты, и покупателю
    #: нужно сказать об этом честно, а не показывать «отправляю…».
    instant: bool = True

    async def deliver_stars(self, username: str, amount: int) -> DeliveryResult:
        raise NotImplementedError

    async def deliver_premium(self, username: str, months: int) -> DeliveryResult:
        raise NotImplementedError

    async def deliver_steam(self, login: str, amount: int) -> DeliveryResult:
        raise NotImplementedError

    async def check_steam_login(self, login: str) -> "SteamAccount | None":
        """Существует ли такой аккаунт Steam. None — сервис не смог проверить."""
        raise NotImplementedError

    async def get_balance(self) -> str:
        raise NotImplementedError

    async def resolve_recipient(self, username: str) -> Recipient | None:
        raise NotImplementedError

    async def healthcheck(self) -> dict:
        raise NotImplementedError

    async def close(self) -> None:
        return None


# =============================================================== заглушка


class MockProvider(DeliveryProvider):
    """Ничего не отправляет. Для проверки бота без денег."""

    supports_name_lookup = True

    def __init__(self) -> None:
        self._counter = 0

    async def check_steam_login(self, login: str) -> "SteamAccount | None":
        await asyncio.sleep(0.2)
        return SteamAccount(login=login, exists=login != "notfound",
                            name=login.capitalize())

    async def deliver_steam(self, login: str, amount: int) -> DeliveryResult:
        await asyncio.sleep(0.5)
        self._counter += 1
        log.warning("MOCK: «пополнено» %s на Steam %s", amount, login)
        return DeliveryResult(order_id=f"mock-steam-{self._counter}", raw={})

    async def deliver_stars(self, username: str, amount: int) -> DeliveryResult:
        await asyncio.sleep(0.5)
        self._counter += 1
        log.warning("MOCK: «выдано» %s звёзд для @%s", amount, username)
        return DeliveryResult(order_id=f"mock-{self._counter}", raw={"mock": True})

    async def deliver_premium(self, username: str, months: int) -> DeliveryResult:
        await asyncio.sleep(0.5)
        self._counter += 1
        log.warning("MOCK: «выдан» Premium на %s мес. для @%s", months, username)
        return DeliveryResult(order_id=f"mock-{self._counter}", raw={"mock": True})

    async def get_balance(self) -> str:
        return "MOCK — реальный баланс недоступен"

    async def resolve_recipient(self, username: str) -> Recipient | None:
        if username.lower() in ("notfound", "unknown"):
            return None
        return Recipient(username=username, name=f"{username.capitalize()} (MOCK)",
                         verified=True)

    async def healthcheck(self) -> dict:
        return {
            "ok": True,
            "mode": "mock",
            "steps": [("Режим", "MOCK — реальной выдачи нет")],
            "error": "",
        }


# =============================================================== ApiFragment


class ApiFragProvider(DeliveryProvider):
    """Клиент apifragment.online.

    Имя аккаунта получателя шлюз не отдаёт — у него нет такого метода,
    поэтому supports_name_lookup остаётся False и бот честно говорит
    покупателю, что имя не проверено.
    """

    supports_name_lookup = False

    def __init__(self) -> None:
        if not settings.fragment_api_key:
            raise RuntimeError("FRAGMENT_MODE=api, но не задан FRAGMENT_API_KEY")
        self._base = settings.fragment_base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------ транспорт

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {settings.fragment_api_key}"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> dict:
        try:
            data = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001 — при ошибке может прийти HTML
            data = {}
        if not isinstance(data, dict):
            data = {"response": data}
        if not data:
            data = {"raw": (await resp.text())[:300]}
        return data

    @staticmethod
    def _error_text(data: dict) -> str:
        for key in ("error", "message", "detail", "reason", "description"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(data)[:300]

    async def _request(
        self, method: str, path: str, payload: dict | None = None, *, safe: bool = False
    ) -> dict:
        """safe=True — запрос ничего не меняет, сетевой сбой можно считать
        обычной ошибкой, а не неопределённым исходом."""
        session = await self._get_session()
        url = self._base + path
        try:
            async with session.request(method, url, json=payload) as resp:
                data = await self._read_json(resp)
                if resp.status >= 500:
                    raise DeliveryUncertain(
                        f"Шлюз вернул {resp.status}: {self._error_text(data)}"
                    )
                if resp.status >= 400:
                    raise DeliveryError(
                        f"Шлюз вернул {resp.status}: {self._error_text(data)}"
                    )
                return data
        except (TimeoutError, aiohttp.ClientError) as exc:
            message = f"Нет ответа от шлюза: {exc}"
            raise (DeliveryError if safe else DeliveryUncertain)(message) from exc

    # -------------------------------------------------------- авторизация

    async def login(self, *, force: bool = False) -> dict:
        """Отдать шлюзу сид-фразу: он логинится на Fragment и хранит сессию.

        По документации повторять не нужно, поэтому делаем один раз за запуск.
        """
        async with self._login_lock:
            if self._logged_in and not force:
                return {"status": "ok", "cached": True}
            if not settings.fragment_wallet_seed.strip():
                raise DeliveryError(
                    "Не задана сид-фраза кошелька (FRAGMENT_WALLET_SEED) — "
                    "шлюз не сможет войти на Fragment."
                )
            data = await self._request(
                "POST", LOGIN_PATH,
                {"wallet_seed": settings.fragment_wallet_seed.strip()},
                safe=True,
            )
            self._logged_in = True
            log.info("ApiFragment: сессия Fragment установлена (%s)",
                     data.get("method") or "—")
            return data

    async def _ensure_login(self) -> None:
        if not self._logged_in:
            await self.login()

    # ------------------------------------------------------------ выдача

    async def deliver_stars(self, username: str, amount: int) -> DeliveryResult:
        return await self._order(STARS_PATH, {
            "username": username,
            "quantity": amount,
            "payment_method": settings.fragment_payment_method,
        })

    async def deliver_premium(self, username: str, months: int) -> DeliveryResult:
        return await self._order(PREMIUM_PATH, {
            "username": username,
            "months": months,
            "payment_method": settings.fragment_payment_method,
        })

    async def _order(self, path: str, payload: dict) -> DeliveryResult:
        await self._ensure_login()
        data = await self._request("POST", path, payload)

        status = str(data.get("status", "")).lower()
        if status in FAILED_STATUSES:
            raise DeliveryError(f"Шлюз отклонил заказ: {self._error_text(data)}")

        task_id = data.get("task_id") or data.get("id")
        if task_id is None:
            # Без task_id проверить исход нечем — считать доставкой нельзя.
            raise DeliveryUncertain(
                f"Шлюз не вернул task_id, исход неизвестен: {str(data)[:200]}"
            )

        log.info("ApiFragment: задача %s поставлена в очередь", task_id)
        final = await self._await_task(task_id)
        return DeliveryResult(order_id=str(task_id), raw=final)

    async def _await_task(self, task_id: object) -> dict:
        """Опрашивать задачу, пока она не завершится.

        Бросает DeliveryError, если задача провалилась (деньги вернутся),
        и DeliveryUncertain, если не дождались — тогда решает админ.
        """
        deadline = settings.task_poll_timeout
        interval = max(settings.task_poll_interval, 1)
        waited = 0
        last: dict = {}
        unknown_seen: set[str] = set()

        while waited < deadline:
            await asyncio.sleep(interval)
            waited += interval
            try:
                last = await self._request(
                    "GET", TASK_PATH.format(task_id=task_id), safe=True
                )
            except DeliveryError as exc:
                # Сетевой сбой при опросе — задача может всё ещё выполняться.
                log.warning("Задача %s: опрос не удался (%s)", task_id, exc)
                continue

            status = str(
                last.get("status") or last.get("state") or last.get("result") or ""
            ).lower().strip()

            if status in DONE_STATUSES:
                log.info("Задача %s выполнена за ~%s сек", task_id, waited)
                return last
            if status in FAILED_STATUSES:
                raise DeliveryError(
                    f"Задача {task_id} провалилась: {self._error_text(last)}"
                )
            if status and status not in PENDING_STATUSES and status not in unknown_seen:
                unknown_seen.add(status)
                log.warning(
                    "Задача %s: незнакомый статус %r — жду дальше. Ответ: %s",
                    task_id, status, str(last)[:300],
                )

        raise DeliveryUncertain(
            f"Задача {task_id} не завершилась за {deadline} сек. "
            f"Последний ответ: {str(last)[:200]}"
        )

    # ------------------------------------------------------------ прочее

    async def resolve_recipient(self, username: str) -> Recipient | None:
        """У шлюза нет метода проверки юзернейма, поэтому имя неизвестно.

        Возвращаем непроверенного получателя: бот покажет это покупателю
        честно, вместо того чтобы делать вид, что аккаунт подтверждён.
        """
        return Recipient(username=username, name="", verified=False)

    async def get_balance(self) -> str:
        # Отдельного метода баланса в документации нет: он лежит на самом
        # кошельке Fragment, а не у шлюза.
        return "смотрите на кошельке Fragment"

    async def healthcheck(self) -> dict:
        steps: list[tuple[str, str]] = []
        try:
            data = await self.login(force=True)
        except (DeliveryError, DeliveryUncertain) as exc:
            steps.append(("Вход на Fragment", f"❌ {exc}"))
            return {"ok": False, "mode": "api", "steps": steps, "error": str(exc)}

        method = data.get("method") or "—"
        cookies = data.get("cookies_refreshed") or []
        steps.append(("Токен шлюза", "✅ принят"))
        steps.append(("Вход на Fragment", f"✅ сессия активна ({method})"))
        if cookies:
            steps.append(("Обновлены cookies", "✅ " + ", ".join(map(str, cookies))))
        steps.append(("Валюта оплаты", settings.fragment_payment_method))
        return {"ok": True, "mode": "api", "steps": steps, "error": ""}


#: Как владелец может назвать каждый режим в FRAGMENT_MODE. Списки живут
#: здесь: и выбор провайдера, и отчёт о готовности, и выбор счёта для API
#: спрашивают одно и то же — «какой это поставщик», — и расходиться им
#: нельзя.
FAZER_MODES = frozenset({"fazer", "fazercards", "fzr"})
MYSTARS_MODES = frozenset({"mystars", "faas"})
APIFRAG_MODES = frozenset({"api", "apifrag", "apifragment"})
#: Режимы, при которых звёзды и Premium правда уходят клиенту.
DELIVERY_MODES = FAZER_MODES | MYSTARS_MODES | APIFRAG_MODES


def mode_now() -> str:
    """Текущий режим выдачи, приведённый к сравнимому виду."""
    return settings.fragment_mode.strip().lower()


def build_provider(payer=None) -> DeliveryProvider:
    """Выбрать способ выдачи по FRAGMENT_MODE.

    fazer   — api.fzr.cards: баланс реселлера, сид-фраза не нужна;
    mystars — api.mystars.tg: оплата каждого заказа переводом из кошелька;
    api     — прежний шлюз apifragment.online (хранит сид-фразу у себя);
    mock    — ничего не отправляет.
    """
    mode = mode_now()

    if mode in FAZER_MODES:
        from app.services.fazer import FazerProvider

        return FazerProvider()

    if mode in MYSTARS_MODES:
        # Импорт внутри: SDK нужен только в этом режиме.
        from app.services.mystars import MyStarsProvider

        if payer is None:
            raise RuntimeError("Для режима mystars нужен способ оплаты заказов")
        return MyStarsProvider(payer)

    if mode in APIFRAG_MODES:
        return ApiFragProvider()

    if mode != "mock":
        log.warning("Неизвестный FRAGMENT_MODE=%r, использую mock", settings.fragment_mode)
    log.warning("Выдача работает в режиме MOCK — звёзды не отправляются")
    return MockProvider()
