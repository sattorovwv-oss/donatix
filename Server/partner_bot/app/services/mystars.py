"""Выдача через MyStars FaaS поверх официального SDK (mystars-faas).

Чем это лучше прежнего шлюза: сид-фраза кошелька сервису не передаётся.
Авторизация — X-Api-Key, а за каждый заказ идёт обычный перевод на их адрес
с вашего кошелька, ключи от которого остаются только у вас.

Порядок: создаём заказ → получаем адрес, сумму и memo → платим →
ждём terminal-статус. Если доставить не вышло, MyStars возвращает перевод
на адрес отправителя, а заказ получает статус reversed/failed.

Оплату выполняет payer. Ручной payer присылает владельцу ссылку Tonkeeper —
один тап, и никакой код не подписывает транзакции.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from mystars_faas import (
    AsyncMyStarsClient, MyStarsError, Order, OrderWaitTimeout,
)
from mystars_faas.payment import build_payment_request

from app.config import settings
from app.services.fragment import (
    DeliveryError, DeliveryProvider, DeliveryResult, DeliveryUncertain, Recipient,
)

log = logging.getLogger(__name__)

DELIVERED = {"delivered", "completed"}
TERMINAL_BAD = {"failed", "reversed", "expired", "cancelled"}

FAILURE_REASONS = {
    "undeliverable": "MyStars не смог доставить товар — перевод возвращён на кошелёк",
    "underpaid": "перевели меньше нужного — перевод возвращён",
    "overpaid": "перевели больше допустимого — перевод возвращён",
    "no_memo": "перевод ушёл без memo — возвращён отправителю",
    "wrong_memo": "memo не совпало с заказом — перевод возвращён",
    "expired": "оплата не пришла вовремя, заказ закрыт",
    "already_subscribed": "у получателя уже есть активный Premium",
}


@dataclass
class CostEstimate:
    """Во что заказ обходится ВАМ — то, что MyStars спишет с кошелька."""
    quantity: int
    amount: str            # сумма к оплате, строкой
    currency: str          # ton или usdt_ton
    usd_total: Decimal     # та же сумма в долларах
    usd_per_unit: Decimal  # доллары за одну звезду / один месяц
    usdt_per_ton: str


@dataclass
class PayLinks:
    """Ссылки на оплату заказа — то, что видит владелец."""
    order_id: str
    amount: str
    currency: str
    address: str
    memo: str
    tonkeeper: str
    deeplink: str
    expires_at: str


class ManualPayer:
    """Оплата в один тап: владельцу приходит готовая ссылка Tonkeeper.

    Ни одна транзакция не подписывается кодом бота, поэтому сид-фраза
    не нужна нигде — ни у сервиса, ни на сервере.
    """

    kind = "manual"

    def __init__(self, send) -> None:
        #: send(text, links) -> awaitable — как показать владельцу счёт
        self._send = send

    async def pay(self, links: PayLinks) -> str:
        await self._send(links)
        log.info("Заказ %s: счёт на %s %s отправлен владельцу",
                 links.order_id, links.amount, links.currency)
        return "ожидает оплаты владельцем"

    async def balance_text(self) -> str:
        return "оплата вручную — баланс смотрите в кошельке"

    async def close(self) -> None:
        return None


class MyStarsProvider(DeliveryProvider):
    supports_name_lookup = True

    @property
    def instant(self) -> bool:  # type: ignore[override]
        # При ручной оплате товар ждёт, пока владелец нажмёт «оплатить».
        return self.payer.kind != "manual"

    def __init__(self, payer: ManualPayer) -> None:
        if not settings.mystars_api_key:
            raise RuntimeError("Не задан MYSTARS_API_KEY")
        self.api = AsyncMyStarsClient(
            settings.mystars_api_key, base_url=settings.mystars_base_url
        )
        self.payer = payer

    async def close(self) -> None:
        await self.api.aclose()
        await self.payer.close()

    # ------------------------------------------------------------- цены

    async def quote(self, product_type: str, amount: int) -> str:
        """Итоговая сумма к оплате строкой — во float переводить нельзя."""
        kwargs = {"quantity": amount} if product_type == "stars" else {"months": amount}
        quote = await self._call(
            self.api.get_pricing(
                type=product_type, payment_currency=settings.mystars_currency, **kwargs
            ),
            safe=True,
        )
        return str(quote.amount)

    async def cost_estimate(self, product_type: str, amount: int) -> CostEstimate:
        """Себестоимость заказа с пересчётом в доллары.

        MyStars считает в TON или USDT. Чтобы получить цену в сомони, сумму
        сначала приводим к долларам — курс TON отдаёт сам сервис в
        usdt_per_ton, а доллар в сомони владелец задаёт в панели.
        """
        kwargs = {"quantity": amount} if product_type == "stars" else {"months": amount}
        quote = await self._call(
            self.api.get_pricing(
                type=product_type, payment_currency=settings.mystars_currency, **kwargs
            ),
            safe=True,
        )

        try:
            total = Decimal(str(quote.amount))
        except (InvalidOperation, TypeError) as exc:
            raise DeliveryError(f"MyStars вернул нечисловую цену: {quote.amount!r}") from exc

        currency = str(quote.currency or settings.mystars_currency).lower()
        rate = str(quote.usdt_per_ton or "")

        if currency.startswith("usdt"):
            usd_total = total          # уже в долларах
        else:
            if not rate:
                raise DeliveryError(
                    "MyStars не отдал курс TON — попробуйте ещё раз через минуту."
                )
            usd_total = total * Decimal(rate)

        if amount <= 0:
            raise DeliveryError("Количество должно быть больше нуля.")

        return CostEstimate(
            quantity=amount,
            amount=str(quote.amount),
            currency=currency,
            usd_total=usd_total,
            usd_per_unit=usd_total / Decimal(amount),
            usdt_per_ton=rate,
        )

    async def quote_many(self, quantities: list[int]) -> dict[int, str]:
        if not quantities:
            return {}
        batch = await self._call(
            self.api.get_pricing_batch(
                quantities=sorted(set(quantities))[:200],
                payment_currency=settings.mystars_currency,
            ),
            safe=True,
        )
        return {int(item.quantity): str(item.amount) for item in batch.quotes}

    # -------------------------------------------------------- получатель

    async def resolve_recipient(self, username: str) -> Recipient | None:
        try:
            check = await self.api.check_recipient(username, type="stars")
        except MyStarsError as exc:
            log.info("MyStars: получатель @%s не проверен (%s)", username, exc)
            return None

        # indeterminate=true значит «проверка не смогла решить», и eligible
        # приходит true как разрешающее умолчание. Показывать это покупателю
        # как подтверждение нельзя — иначе он поверит непроверенному.
        if not check.eligible and not check.indeterminate:
            log.info("MyStars: @%s недоступен (%s)", username, check.reason)
            return None

        return Recipient(
            username=username,
            name=(check.recipient_name or "").strip(),
            verified=not check.indeterminate,
        )

    # ------------------------------------------------------------ выдача

    async def deliver_stars(self, username: str, amount: int) -> DeliveryResult:
        return await self._fulfil("stars", amount, username)

    async def deliver_premium(self, username: str, months: int) -> DeliveryResult:
        return await self._fulfil("premium", months, username)

    async def _fulfil(
        self, product_type: str, amount: int, username: str
    ) -> DeliveryResult:
        kwargs = {"quantity": amount} if product_type == "stars" else {"months": amount}
        created = await self._call(
            self.api.create_order(
                type=product_type, recipient=username,
                payment_currency=settings.mystars_currency, **kwargs
            )
        )
        links = self._links(created)
        log.info("MyStars: заказ %s создан, к оплате %s %s",
                 links.order_id, links.amount, links.currency)

        try:
            note = await self.payer.pay(links)
        except DeliveryError:
            # Перевод не ушёл — заказ можно закрыть, деньги не тронуты.
            await self._cancel(links.order_id)
            raise
        except Exception as exc:  # noqa: BLE001
            # Перевод мог уйти и не подтвердиться — отменять опасно.
            raise DeliveryUncertain(
                f"Оплата заказа {links.order_id} завершилась неясно: {exc}"
            ) from exc

        log.info("MyStars: заказ %s — %s", links.order_id, note)
        order = await self._wait(links.order_id)
        return DeliveryResult(order_id=links.order_id, raw=_as_dict(order))

    @staticmethod
    def _links(created) -> PayLinks:
        payment = created.payment
        if not payment or not payment.pay_to_address:
            raise DeliveryUncertain(
                f"MyStars создал заказ {created.order_id} без реквизитов оплаты"
            )
        request = build_payment_request(payment)
        return PayLinks(
            order_id=str(created.order_id),
            amount=str(payment.amount),
            currency=str(payment.currency),
            address=str(payment.pay_to_address),
            memo=str(payment.memo or created.order_id),
            tonkeeper=request.tonkeeper_link or "",
            deeplink=request.ton_deeplink or "",
            expires_at=str(created.expires_at or ""),
        )

    async def _wait(self, order_id: str) -> Order:
        try:
            order = await self.api.await_order(
                order_id,
                timeout=float(settings.mystars_wait_timeout),
                poll_interval=float(max(settings.task_poll_interval, 2)),
            )
        except OrderWaitTimeout as exc:
            raise DeliveryUncertain(
                f"Заказ {order_id} не завершился за "
                f"{settings.mystars_wait_timeout} сек — проверьте его в MyStars"
            ) from exc
        except MyStarsError as exc:
            raise DeliveryUncertain(f"Заказ {order_id}: {exc}") from exc

        status = (order.status or "").lower()
        if status in DELIVERED:
            log.info("Заказ %s доставлен", order_id)
            return order
        if status in TERMINAL_BAD:
            reason = (order.failure_reason or "").lower()
            explain = FAILURE_REASONS.get(reason, reason or status)
            # MyStars вернул перевод на кошелёк, значит для покупателя это
            # обычный отказ — его баланс вернём мы сами.
            raise DeliveryError(f"Заказ {order_id}: {explain}")
        raise DeliveryUncertain(f"Заказ {order_id} остановился в статусе {status}")

    async def _cancel(self, order_id: str) -> None:
        try:
            await self.api.cancel_order(order_id)
        except MyStarsError as exc:
            log.warning("Заказ %s не отменился: %s", order_id, exc)

    # ----------------------------------------------------------- прочее

    @staticmethod
    async def _call(coro, *, safe: bool = False):
        """Ошибки SDK в понятия бота: safe-запрос ничего не меняет, поэтому
        его сбой — обычная ошибка, а не неопределённый исход."""
        try:
            return await coro
        except MyStarsError as exc:
            raise (DeliveryError if safe else DeliveryUncertain)(str(exc)) from exc

    async def get_balance(self) -> str:
        return await self.payer.balance_text()

    async def healthcheck(self) -> dict:
        steps: list[tuple[str, str]] = []
        try:
            products = await self.api.list_products()
            names = ", ".join(str(p.type) for p in products) or "—"
            steps.append(("Ключ MyStars", "✅ принят"))
            steps.append(("Доступные товары", names))
        except MyStarsError as exc:
            steps.append(("Ключ MyStars", f"❌ {exc}"))
            hint = _diagnose(exc)
            if hint:
                steps.append(("Что делать", hint))
            return {"ok": False, "mode": "mystars", "steps": steps, "error": str(exc)}

        try:
            steps.append(("Цена 100 звёзд",
                          f"✅ {await self.quote('stars', 100)} {settings.mystars_currency}"))
        except (DeliveryError, DeliveryUncertain) as exc:
            steps.append(("Цена 100 звёзд", f"⚠️ не получена: {exc}"))

        steps.append(("Оплата заказов",
                      "🖐 вручную — счёт приходит вам в Telegram"
                      if self.payer.kind == "manual" else "автоматически"))
        return {"ok": True, "mode": "mystars", "steps": steps, "error": ""}


def _as_dict(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "status": order.status,
        "purchase_tx": order.purchase_tx,
        "payment_tx": order.payment_tx,
    }


def _diagnose(exc: Exception) -> str:
    """Подсказать по тексту ошибки, что именно не так с настройками."""
    message = str(exc).lower()
    url = settings.mystars_base_url

    if any(part in url for part in ("10.", "127.", "192.168.", "localhost", "172.16.")):
        return (
            f"В настройках указан внутренний адрес <code>{url}</code> — "
            "он доступен только внутри сети MyStars. "
            "Поставьте <code>https://api.mystars.tg/v1</code> "
            "командой <code>stars-bot setup</code>."
        )
    if any(word in message for word in ("connect", "timeout", "resolve", "network")):
        return (
            f"Сервер не отвечает по адресу <code>{url}</code>. "
            "Проверьте адрес и интернет на сервере."
        )
    if any(word in message for word in ("401", "unauthorized", "authentication", "api key")):
        return (
            "Ключ не принят. Возьмите новый в @my_stars_tg_bot "
            "(<code>/api_rotate</code>) и введите через "
            "<code>stars-bot setup</code>."
        )
    return ""
