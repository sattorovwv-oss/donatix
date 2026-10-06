"""Проверка FazerCards: асинхронная выдача, статусы, цены в долларах."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

os.environ["FRAGMENT_MODE"] = "fazer"
os.environ["FAZER_API_KEY"] = "fc_test"
os.environ["TASK_POLL_INTERVAL"] = "1"
os.environ["TASK_POLL_TIMEOUT"] = "4"

from decimal import Decimal

from app.services import fazer as fz
from app.services.fragment import DeliveryError, DeliveryUncertain

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


STARS_PRICE = {
    "ok": True, "kind": "telegram_stars", "price_per_star": "0.0154000",
    "min_amount": 50, "max_amount": 10000, "rates_updated_at": "2026-08-26T10:00:00Z",
}
PREMIUM_PRICE = {
    "ok": True, "kind": "telegram_premium",
    "plans": [{"months": 3, "price_usd": "13.9000"},
              {"months": 6, "price_usd": "18.5000"},
              {"months": 12, "price_usd": "32.9000"}],
    "rates_updated_at": "2026-08-26T10:00:00Z",
}


class FakeFazer(fz.FazerProvider):
    """Провайдер с подменённым транспортом."""

    def __init__(self, *, buy=None, statuses=None, raise_on=None, balance=None):
        super().__init__()
        self.buy_response = buy if buy is not None else {
            "ok": True, "order": {"id": 555, "status": "pending"}}
        self.statuses = list(statuses or [])
        self.raise_on = raise_on or {}
        self.balance = balance
        self.calls: list[str] = []

    async def _request(self, method, path, payload=None, *, safe=False,
                       headers=None, **kw):
        self.calls.append(f"{method} {path}")
        for key, exc in self.raise_on.items():
            if key in path:
                raise exc
        if path == fz.STARS_QUOTE:
            return STARS_PRICE
        if path == fz.PREMIUM_QUOTE:
            return PREMIUM_PRICE
        if path in (fz.STARS_BUY, fz.PREMIUM_BUY):
            return self.buy_response
        if path == fz.BALANCE_PATH:
            # Прямой адрес из документации. Без баланса — молчим, чтобы
            # сработал прежний перебор адресов.
            if self.balance is None:
                raise fz.DeliveryError("HTTP 404")
            return self.balance
        if self.balance is not None:
            return self.balance
        raise AssertionError(f"неожиданный путь {path}")

    async def _get_raw(self, path):
        """Опрос заказа и поиск баланса идут через этот метод — подменяем
        и его, иначе тесты полезут в настоящую сеть."""
        self.calls.append(f"GET {path}")
        for key, exc in self.raise_on.items():
            if key in path:
                raise exc
        if "/orders/" in path or "/order/" in path:
            if self.statuses:
                return 200, self.statuses.pop(0)
            return 200, {"ok": True, "order": {"id": 555, "status": "pending"}}
        if self.balance is not None and any(
            w in path for w in ("account", "balance", "me", "wallet")
        ):
            return 200, self.balance
        return 404, {"ok": False, "error": "Not Found"}

    async def _remember(self, key, value):
        self.remembered = (key, value)

    async def close(self):
        return None


async def expect(coro, kind):
    try:
        await coro
    except kind as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def order(status, **extra):
    return {"ok": True, "order": {"id": 555, "status": status, **extra}}


async def main() -> None:
    # ---------------------------------------------------- цены в долларах
    prov = FakeFazer()
    est = await prov.cost_estimate("stars", 1000)
    check("себестоимость звёзд считается из цены сервиса",
          est.usd_total == Decimal("0.0154000") * 1000, str(est.usd_total))
    check("цена за штуку верная", est.usd_per_unit == Decimal("0.0154000"))
    check("валюта — доллары", est.currency == "usd")

    est = await prov.cost_estimate("premium", 6)
    check("себестоимость Premium берётся из тарифа",
          est.usd_total == Decimal("18.5000"), str(est.usd_total))

    exc = await expect(prov.cost_estimate("premium", 9), DeliveryError)
    check("несуществующий тариф Premium отклоняется",
          isinstance(exc, DeliveryError) and "3, 6, 12" in str(exc), str(exc)[:80])

    check("лимиты берутся у сервиса", await prov.limits() == (50, 10000))

    # -------------------------- принят ≠ выдан (главная проверка)
    prov = FakeFazer(buy=order("pending"), statuses=[order("pending")] * 10)
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("вечно висящий заказ НЕ считается выданным",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    prov = FakeFazer(buy=order("pending"),
                     statuses=[order("pending"), order("completed")])
    result = await prov.deliver_stars("durov", 100)
    check("заказ подтверждается опросом статуса", result.order_id == "555")
    check("статус реально опрашивался",
          sum(1 for c in prov.calls if "/orders/" in c) == 2, str(prov.calls))

    # ------------------------------- сразу финальный статус — без опроса
    prov = FakeFazer(buy=order("completed"))
    result = await prov.deliver_stars("durov", 100)
    check("готовый заказ не опрашивается зря",
          result.order_id == "555" and not any("/orders/" in c for c in prov.calls),
          str(prov.calls))

    # -------------------------------------------------- отказы
    prov = FakeFazer(buy=order("failed", error="Insufficient balance"))
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryError)
    check("отказ сразу при покупке — деньги вернутся клиенту",
          isinstance(exc, DeliveryError) and "Insufficient balance" in str(exc),
          str(exc)[:70])

    prov = FakeFazer(buy=order("pending"),
                     statuses=[order("failed", reason="username not found")])
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryError)
    check("отказ при опросе — тоже возврат",
          isinstance(exc, DeliveryError) and "username not found" in str(exc))

    # --------------------------- ответ без данных заказа
    prov = FakeFazer(buy={"ok": True})
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("ответ без заказа не считается выдачей",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    prov = FakeFazer(buy={"ok": True, "order": {"status": "pending"}})
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("заказ без номера не считается выдачей",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ------------------- незнакомый статус не принимается за успех
    prov = FakeFazer(buy=order("pending"), statuses=[order("странное")] * 10)
    exc = await expect(prov.deliver_stars("durov", 100), DeliveryUncertain)
    check("незнакомый статус не считается успехом",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ------------------------ разные написания успеха
    for word in ("completed", "done", "delivered", "success", "fulfilled"):
        prov = FakeFazer(buy=order("pending"), statuses=[order(word)])
        res = await prov.deliver_stars("durov", 100)
        check(f"статус «{word}» = выдано", res.order_id == "555")

    # ---------------------- сбой опроса переживается
    class Flaky(FakeFazer):
        def __init__(self):
            super().__init__(buy=order("pending"), statuses=[order("completed")])
            self.first = True

        async def _request(self, method, path, payload=None, *, safe=False,
                           **kw):
            if "/orders/" in path and self.first:
                self.first = False
                raise DeliveryError("сеть моргнула")
            return await super()._request(method, path, payload, safe=safe, **kw)

    check("сбой одного опроса не роняет выдачу",
          (await Flaky().deliver_stars("durov", 100)).order_id == "555")

    # ------------------------------------------------- проверка связи
    prov = FakeFazer(balance={"ok": True, "balance": "42.50", "currency": "USD"})
    prov.statuses = []
    report = await prov.healthcheck()
    check("проверка связи проходит", report["ok"], str(report["steps"])[:150])
    check("в отчёте видна цена звезды",
          any("0.0154" in v for _, v in report["steps"]))
    check("в отчёте виден баланс",
          any("42.50" in v for _, v in report["steps"]), str(report["steps"]))

    report = await FakeFazer(raise_on={"telegram/stars": DeliveryError("Invalid API key")}).healthcheck()
    check("плохой ключ виден в проверке",
          not report["ok"] and "Invalid API key" in str(report["steps"]))

    # ----------------------------------------- получатель не проверяется
    who = await FakeFazer().resolve_recipient("someone")
    check("получатель возвращается непроверенным",
          who is not None and not who.verified)

    # ------------- заказ находится при разных формах API
    class ShapedApi(FakeFazer):
        """Отвечает только на один заданный набор адресов."""

        def __init__(self, working: dict, buy=None):
            super().__init__(buy=buy or order("pending"))
            self.working = working
            self.seen: list[str] = []

        async def _get_raw(self, path):
            self.seen.append(path)
            if path in self.working:
                return 200, self.working[path]
            return 404, {"ok": False, "error": "Not Found"}

    # а) одиночный заказ по нестандартному адресу
    api = ShapedApi({"/api/v2/order/555": {"ok": True, "order": {"id": 555, "status": "completed"}}})
    res = await api.deliver_stars("durov", 100)
    check("заказ найден по запасному адресу", res.order_id == "555", str(api.seen))

    # б) одиночного адреса нет — ищем в списке
    api = ShapedApi({"/api/v2/orders": {"ok": True, "orders": [
        {"id": 999, "status": "pending"}, {"id": 555, "status": "delivered"}]}})
    res = await api.deliver_stars("durov", 100)
    check("заказ найден в общем списке", res.order_id == "555", str(api.seen[-3:]))

    # в) заказ нигде не читается — выдача не объявляется успешной
    api = ShapedApi({})
    exc = await expect(api.deliver_stars("durov", 100), DeliveryUncertain)
    check("нечитаемый заказ не считается выданным",
          isinstance(exc, DeliveryUncertain), type(exc).__name__)

    # ---------------------------- баланс находится сам
    class BalanceApi(FakeFazer):
        def __init__(self, working):
            super().__init__()
            self.working = working

        async def _get_raw(self, path):
            return (200, self.working[path]) if path in self.working else (404, {"ok": False})

        async def _remember(self, key, value):
            self.remembered = (key, value)

    api = BalanceApi({"/api/v2/account/balance": {"ok": True, "balance": "42.50", "currency": "USD"}})
    check("баланс найден перебором", await api.get_balance() == "42.50 USD")
    check("найденный адрес запоминается",
          api.remembered == ("fazer_balance_path", "/api/v2/account/balance"),
          str(getattr(api, "remembered", None)))

    api = BalanceApi({"/api/v2/me": {"ok": True, "account": {"balance": "7.00", "currency": "EUR"}}})
    check("баланс достаётся из вложенного объекта",
          await api.get_balance() == "7.00 EUR")

    api = BalanceApi({})
    exc = await expect(api.get_balance(), DeliveryError)
    check("ненайденный баланс объясняется понятно",
          isinstance(exc, DeliveryError) and "Найти адреса" in str(exc), str(exc)[:60])

    # --------------------------------- перебор адресов API
    class ProbeFazer(FakeFazer):
        """Отвечает только на два адреса из списка кандидатов."""

        WORKING = {
            "/api/v2/account/balance": {"ok": True, "balance": "42.50", "currency": "USD"},
            "/api/v2/orders": {"ok": True, "orders": [{"id": 1, "status": "completed"}]},
        }

        async def _get_session(self):
            provider = self

            class Resp:
                def __init__(self, path):
                    self.path = path
                    self.status = 200 if path in provider.WORKING else 404

                async def json(self, **kw):
                    return provider.WORKING.get(self.path, {"ok": False, "error": "Not Found"})

                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False

            class Session:
                def get(self, url):
                    return Resp(url.replace(provider._base, ""))

            return Session()

    found = await ProbeFazer().probe_paths()
    check("перебор нашёл адрес баланса",
          [p for p, _ in found["balance"]] == ["/api/v2/account/balance"],
          str(found["balance"]))
    check("перебор нашёл адрес заказов",
          [p for p, _ in found["orders"]] == ["/api/v2/orders"], str(found["orders"]))
    check("в выжимке видно содержимое ответа",
          "42.50" in found["balance"][0][1], found["balance"][0][1])
    check("несуществующие адреса отсеяны",
          len(found["balance"]) == 1 and len(found["orders"]) == 1)

    # ------------------- путь заказов из панели перекрывает настройку
    from app import runtime as rt
    rt._cache["fazer_order_path"] = "/api/v2/my-orders/{order_id}"
    check("путь из панели перекрывает .env",
          fz.FazerProvider.order_path() == "/api/v2/my-orders/{order_id}")
    rt._cache["fazer_order_path"] = ""
    check("без настройки берётся значение из .env",
          fz.FazerProvider.order_path() == fz.settings.fazer_order_path)

    # ------------------- проверка связи реально проверяет чтение заказов
    class HealthApi(ShapedApi):
        async def _request(self, method, path, payload=None, *, safe=False,
                           **kw):
            if path == fz.STARS_QUOTE: return STARS_PRICE
            if path == fz.PREMIUM_QUOTE: return PREMIUM_PRICE
            raise AssertionError(path)

    report = await HealthApi({"/api/v2/orders": {"ok": True, "orders": []},
                              "/api/v2/account": {"ok": True, "balance": "5.00"}}).healthcheck()
    check("проверка связи подтверждает чтение заказов",
          report["ok"] and any("✅" in v for k, v in report["steps"] if "заказ" in k.lower()),
          str(report["steps"]))

    report = await HealthApi({"/api/v2/account": {"ok": True, "balance": "5.00"}}).healthcheck()
    check("без доступа к заказам проверка не даёт добро", not report["ok"])

    # ------------------- адрес сервиса: документация пишет его с /api/v2,
    #                     а пути в коде уже с ним — склеивать нельзя
    check("хвост /api/v2 из адреса убирается",
          fz.normalize_base("https://api.fzr.cards/api/v2")
          == "https://api.fzr.cards",
          fz.normalize_base("https://api.fzr.cards/api/v2"))
    check("обычный адрес не портится",
          fz.normalize_base("https://api.fzr.cards/") == "https://api.fzr.cards")
    check("лишний слэш тоже убирается",
          fz.normalize_base("https://api.fzr.cards/api/v2/")
          == "https://api.fzr.cards")
    check("адрес из документации даёт правильный путь",
          fz.FazerProvider(api_key="fc_x",
                           base_url="https://api.fzr.cards/api/v2")._base
          + fz.TOPUP_ORDER == "https://api.fzr.cards/api/v2/topups/order")

    # ------------------- сроки запросов из документации поставщика
    check("на заказ ждём не дольше 20 секунд",
          fz.ORDER_TIMEOUT.total == 20, str(fz.ORDER_TIMEOUT.total))
    check("на остальное — 25", fz.REQUEST_TIMEOUT.total == 25,
          str(fz.REQUEST_TIMEOUT.total))
    check("на соединение — 6", fz.REQUEST_TIMEOUT.connect == 6,
          str(fz.REQUEST_TIMEOUT.connect))

    check("баланс по умолчанию спрашивается там, где он есть",
          fz.settings.fazer_balance_path == "/api/v2/balance",
          fz.settings.fazer_balance_path)

    # ------------------- каталог отдаётся страницами: дочитываем до конца
    class Paged(fz.FazerProvider):
        """Поставщик, отдающий список по страницам."""

        def __init__(self, pages):
            self.pages = pages
            self.asked: list[str] = []
            self._base = "https://x"
            self.api_key = "k"
            self._session = None

        async def _get_raw(self, path):
            self.asked.append(path)
            index = len(self.asked) - 1
            if index >= len(self.pages):
                return 200, {"ok": True, "items": []}
            return 200, self.pages[index]

        async def _remember(self, key, value):
            return None

    api = Paged([
        {"ok": True, "items": [{"category_id": "a", "name": "A"}],
         "meta": {"has_more": True, "next_cursor": "c1"}},
        {"ok": True, "items": [{"category_id": "pubg_mobile_global",
                                "name": "PUBG Mobile"}],
         "meta": {"has_more": False, "next_cursor": None}},
    ])
    found = await api.game_categories()
    check("прочитаны обе страницы", len(found) == 2, str(found))
    check("игра со второй страницы видна",
          any(i["category_id"] == "pubg_mobile_global" for i in found), str(found))
    check("во втором запросе передан курсор",
          "cursor=c1" in api.asked[1], str(api.asked))
    check("размер страницы взят максимальный",
          "limit=500" in api.asked[0], api.asked[0])

    # курсор, который не меняется, не должен уводить в круг
    loop = Paged([
        {"ok": True, "items": [{"category_id": "a", "name": "A"}],
         "meta": {"has_more": True, "next_cursor": "same"}},
        {"ok": True, "items": [{"category_id": "b", "name": "B"}],
         "meta": {"has_more": True, "next_cursor": "same"}},
        {"ok": True, "items": [{"category_id": "c", "name": "C"}],
         "meta": {"has_more": True, "next_cursor": "same"}},
    ])
    found = await loop.game_categories()
    check("повторяющийся курсор обход останавливает",
          len(loop.asked) == 2, str(loop.asked))

    one = Paged([{"ok": True, "items": [{"category_id": "a", "name": "A"}]}])
    found = await one.game_categories()
    check("без страниц читается один раз",
          len(one.asked) == 1 and len(found) == 1, str(one.asked))

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
