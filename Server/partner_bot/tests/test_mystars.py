"""Проверка выдачи через MyStars: статусы, отказы, счёт на оплату."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

os.environ["FRAGMENT_MODE"] = "mystars"
os.environ["MYSTARS_API_KEY"] = "test-key"
os.environ["MYSTARS_WAIT_TIMEOUT"] = "5"

from decimal import Decimal

from mystars_faas import MyStarsError, OrderWaitTimeout
from mystars_faas.payment import PaymentInstruction

from app.services import mystars as ms
from app.services.fragment import DeliveryError, DeliveryUncertain, build_provider

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def payment_block(address="EQAvDfWFG0oYX-flo2yxEDrb2JMoFvOaLJoDpn0mLQ2VF9zM"):
    return PaymentInstruction(
        currency="ton", chain="ton", pay_to_address=address,
        memo="order-memo-1", amount=Decimal("5.757"),
        amount_units="ton", fee=None,
    )


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeApi:
    """Подмена SDK-клиента: отдаёт заданные ответы, пишет вызовы."""

    def __init__(self, *, order_status="delivered", failure_reason=None,
                 payment=None, raise_on=None, recipient=None):
        self.order_status = order_status
        self.failure_reason = failure_reason
        self.payment = payment if payment is not None else payment_block()
        self.raise_on = raise_on or {}
        self.recipient = recipient
        self.calls: list[str] = []

    async def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self.raise_on:
            raise self.raise_on[name]

    async def create_order(self, **kw):
        await self._maybe_raise("create_order")
        return Obj(order_id="ord-1", status="awaiting_payment",
                   payment=self.payment, expires_at="2026-08-26T15:00:00Z",
                   type=kw.get("type"), quantity=kw.get("quantity"),
                   months=kw.get("months"), replayed=False)

    async def await_order(self, order_id, **kw):
        await self._maybe_raise("await_order")
        return Obj(order_id=order_id, status=self.order_status,
                   failure_reason=self.failure_reason,
                   purchase_tx="tx-1", payment_tx="tx-0")

    async def cancel_order(self, order_id):
        await self._maybe_raise("cancel_order")
        return {"status": "cancelled"}

    async def check_recipient(self, username, **kw):
        await self._maybe_raise("check_recipient")
        if self.recipient is None:
            raise MyStarsError("not found")
        return self.recipient

    async def get_pricing(self, **kw):
        await self._maybe_raise("get_pricing")
        return Obj(amount="5.757", currency="ton")

    async def get_pricing_batch(self, **kw):
        await self._maybe_raise("get_pricing_batch")
        return Obj(quotes=[Obj(quantity=q, amount=f"{q / 100:.3f}")
                           for q in kw["quantities"]])

    async def list_products(self):
        await self._maybe_raise("list_products")
        return [Obj(type="stars"), Obj(type="premium")]

    async def aclose(self):
        return None


class RecordingPayer(ms.ManualPayer):
    def __init__(self, fail=None):
        self.invoices: list[ms.PayLinks] = []
        self.fail = fail
        super().__init__(self._record)

    async def _record(self, links):
        if self.fail:
            raise self.fail
        self.invoices.append(links)


def provider_with(api, payer=None):
    payer = payer or RecordingPayer()
    p = ms.MyStarsProvider(payer)
    p.api = api
    return p


async def expect(coro, kind):
    try:
        await coro
    except kind as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


async def main() -> None:
    # ------------------------------------------------- удачная выдача
    api = FakeApi(order_status="delivered")
    payer = RecordingPayer()
    prov = provider_with(api, payer)

    result = await prov.deliver_stars("durov", 500)
    check("доставленный заказ считается успехом", result.order_id == "ord-1")
    check("владельцу отправлен счёт", len(payer.invoices) == 1)

    inv = payer.invoices[0]
    check("в счёте верная сумма", inv.amount == "5.757", inv.amount)
    check("в счёте есть memo", inv.memo == "order-memo-1", inv.memo)
    check("ссылка Tonkeeper — https (годится для кнопки)",
          inv.tonkeeper.startswith("https://"), inv.tonkeeper[:40])
    check("в ссылке сумма в нанотонах", "amount=5757000000" in inv.tonkeeper,
          inv.tonkeeper[-60:])
    check("в ссылке передано memo", "order-memo-1" in inv.tonkeeper)

    # ----------------------------------- статус completed тоже успех
    prov = provider_with(FakeApi(order_status="completed"))
    check("статус completed — тоже успех",
          (await prov.deliver_stars("durov", 100)).order_id == "ord-1")

    # ------------------------------------------- отказы MyStars
    cases = {
        "reversed": "undeliverable",
        "failed": "underpaid",
        "expired": "expired",
        "cancelled": None,
    }
    for status, reason in cases.items():
        prov = provider_with(FakeApi(order_status=status, failure_reason=reason))
        exc = await expect(prov.deliver_stars("durov", 100), DeliveryError)
        check(f"статус {status} — отказ, деньги вернутся клиенту",
              isinstance(exc, DeliveryError), type(exc).__name__)

    prov = provider_with(FakeApi(order_status="reversed", failure_reason="undeliverable"))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryError)
    check("причина отказа переведена на русский",
          "не смог доставить" in str(exc), str(exc)[:70])

    # ------------------------------ промежуточный статус не успех
    prov = provider_with(FakeApi(order_status="purchasing"))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("застрявший в работе заказ не считается доставкой",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ------------------------------------ таймаут ожидания
    prov = provider_with(FakeApi(raise_on={"await_order": OrderWaitTimeout("slow")}))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("таймаут ожидания — неопределённый исход (решает админ)",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ---------------------- счёт не ушёл → заказ отменяется
    api = FakeApi()
    prov = provider_with(api, RecordingPayer(fail=DeliveryError("некому платить")))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryError)
    check("если счёт не отправился — отказ", isinstance(exc, DeliveryError))
    check("повисший заказ отменяется в MyStars", "cancel_order" in api.calls,
          str(api.calls))

    # --------------------- заказ без реквизитов оплаты
    prov = provider_with(FakeApi(payment=Obj(pay_to_address=None)))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("заказ без адреса оплаты не считается доставкой",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ------------------------------------------- получатель
    prov = provider_with(FakeApi(recipient=Obj(
        resolved=True, eligible=True, recipient_name="Павел Дуров",
        reason=None, telegram_message=None, indeterminate=False)))
    who = await prov.resolve_recipient("durov")
    check("имя получателя приходит от MyStars",
          who is not None and who.name == "Павел Дуров", str(who))
    check("получатель помечен проверенным", who.verified)

    prov = provider_with(FakeApi(recipient=Obj(
        resolved=True, eligible=True, recipient_name="Кто-то",
        reason=None, telegram_message=None, indeterminate=True)))
    who = await prov.resolve_recipient("someone")
    check("indeterminate не выдаётся за подтверждение",
          who is not None and not who.verified)

    prov = provider_with(FakeApi(recipient=Obj(
        resolved=True, eligible=False, recipient_name=None,
        reason="already_subscribed", telegram_message=None, indeterminate=False)))
    check("недоступный получатель отклоняется",
          await prov.resolve_recipient("premiumuser") is None)

    prov = provider_with(FakeApi(recipient=None))
    check("ненайденный получатель отклоняется",
          await prov.resolve_recipient("ghost") is None)

    # ------------------------------------------------------ цены
    prov = provider_with(FakeApi())
    check("цена приходит строкой, не float",
          isinstance(await prov.quote("stars", 500), str))
    batch = await prov.quote_many([50, 100, 500])
    check("пакетные цены разбираются", set(batch) == {50, 100, 500}, str(batch))

    # ------------------------------------------------- проверка связи
    report = await provider_with(FakeApi()).healthcheck()
    check("проверка связи проходит", report["ok"] and report["mode"] == "mystars")
    check("видно, что оплата ручная",
          any("вручную" in v for _, v in report["steps"]), str(report["steps"]))

    report = await provider_with(
        FakeApi(raise_on={"list_products": MyStarsError("bad key")})
    ).healthcheck()
    check("плохой ключ виден в проверке",
          not report["ok"] and "bad key" in str(report["steps"]))

    # ------------------------------------- выдача не мгновенная
    check("бот знает, что выдача не мгновенная",
          provider_with(FakeApi()).instant is False)

    # ------------------------- режим не собирается без способа оплаты
    try:
        build_provider(None)
        built = True
    except RuntimeError:
        built = False
    check("режим mystars требует способ оплаты", not built)

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
