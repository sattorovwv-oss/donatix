"""Проверка клиента ApiFragment: опрос задачи, статусы, ошибки.

Сеть подменена заглушкой, поэтому проверяется именно логика: что считается
доставкой, что — отказом, а что уходит админу на разбор.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

import os
os.environ["FRAGMENT_MODE"] = "api"
os.environ["FRAGMENT_API_KEY"] = "test-token"
os.environ["FRAGMENT_WALLET_SEED"] = " ".join(["word"] * 24)
os.environ["TASK_POLL_INTERVAL"] = "1"
os.environ["TASK_POLL_TIMEOUT"] = "4"

from app.services import fragment as fr

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeGateway(ApiF := fr.ApiFragProvider):
    """Провайдер с подменённым транспортом: вместо сети — заданные ответы."""

    def __init__(self, order_response, task_responses, *, fail_on=None):
        super().__init__()
        self.order_response = order_response
        self.task_responses = list(task_responses)
        self.fail_on = fail_on or {}
        self.calls: list[tuple[str, str]] = []

    async def _request(self, method, path, payload=None, *, safe=False):
        self.calls.append((method, path))
        if path in self.fail_on:
            raise self.fail_on[path]
        if path == fr.LOGIN_PATH:
            return {"status": "ok", "method": "ton_connect",
                    "cookies_refreshed": ["stel_ssid", "stel_token"]}
        if path in (fr.STARS_PATH, fr.PREMIUM_PATH):
            return self.order_response
        if path.startswith("/task/"):
            if self.task_responses:
                return self.task_responses.pop(0)
            return {"status": "pending"}
        raise AssertionError(f"неожиданный путь {path}")

    async def close(self):
        return None


async def expect(coro, exc_type):
    try:
        await coro
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


async def main() -> None:
    accepted = {"status": "accepted", "message": "Задача поставлена в очередь",
                "task_id": 12345}

    # ---------------------------------------------- обычная успешная выдача
    gw = FakeGateway(accepted, [{"status": "pending"}, {"status": "completed"}])
    result = await gw.deliver_stars("durov", 100)
    check("успешная задача считается доставкой",
          result.order_id == "12345", str(result.order_id))
    check("перед заказом выполняется вход на Fragment",
          (("POST", fr.LOGIN_PATH)) in gw.calls, str(gw.calls[:2]))
    check("задача опрашивается до результата",
          sum(1 for m, p in gw.calls if p.startswith("/task/")) == 2, str(gw.calls))

    # ------------------------------------- accepted ≠ доставлено (главное)
    gw = FakeGateway(accepted, [{"status": "pending"}] * 10)
    exc = await expect(gw.deliver_stars("durov", 100), fr.DeliveryUncertain)
    check("вечно висящая задача НЕ считается доставкой",
          isinstance(exc, fr.DeliveryUncertain), type(exc).__name__)
    check("в тексте ошибки есть номер задачи", "12345" in str(exc), str(exc)[:80])

    # ------------------------------------------------- задача провалилась
    gw = FakeGateway(accepted, [{"status": "failed", "error": "Недостаточно TON"}])
    exc = await expect(gw.deliver_stars("durov", 100), fr.DeliveryError)
    check("провалившаяся задача — явный отказ (деньги вернутся)",
          isinstance(exc, fr.DeliveryError), type(exc).__name__)
    check("причина отказа попадает в сообщение",
          "Недостаточно TON" in str(exc), str(exc)[:90])

    # ---------------------------------------- шлюз сразу отказал на POST
    gw = FakeGateway({"status": "error", "message": "Invalid username"}, [])
    exc = await expect(gw.deliver_stars("durov", 100), fr.DeliveryError)
    check("отказ на самом заказе — явный отказ",
          isinstance(exc, fr.DeliveryError), type(exc).__name__)

    # --------------------------------------------- ответ без task_id
    gw = FakeGateway({"status": "accepted"}, [])
    exc = await expect(gw.deliver_stars("durov", 100), fr.DeliveryUncertain)
    check("ответ без task_id не считается доставкой",
          isinstance(exc, fr.DeliveryUncertain), type(exc).__name__)

    # ------------------------------ незнакомый статус не считается успехом
    gw = FakeGateway(accepted, [{"status": "витаем_в_облаках"}] * 10)
    exc = await expect(gw.deliver_stars("durov", 100), fr.DeliveryUncertain)
    check("незнакомый статус не принимается за успех",
          isinstance(exc, fr.DeliveryUncertain), type(exc).__name__)

    # ------------------------------- разные написания успеха понимаются
    for word in ("completed", "success", "done", "ok", "delivered"):
        gw = FakeGateway(accepted, [{"status": word}])
        res = await gw.deliver_stars("durov", 100)
        check(f"статус «{word}» = успех", res.order_id == "12345")

    # --------------------------------- сбой опроса не роняет всю выдачу
    class FlakyGateway(FakeGateway):
        def __init__(self):
            super().__init__(accepted, [{"status": "completed"}])
            self._first = True

        async def _request(self, method, path, payload=None, *, safe=False):
            if path.startswith("/task/") and self._first:
                self._first = False
                raise fr.DeliveryError("сеть моргнула")
            return await super()._request(method, path, payload, safe=safe)

    res = await FlakyGateway().deliver_stars("durov", 100)
    check("сбой одного опроса переживается, ждём дальше",
          res.order_id == "12345")

    # ---------------------------------------------------- Premium
    gw = FakeGateway(accepted, [{"status": "completed"}])
    res = await gw.deliver_premium("durov", 12)
    check("Premium заказывается тем же путём", res.order_id == "12345")

    # ------------------------------------------------------ проверка связи
    gw = FakeGateway(accepted, [])
    report = await gw.healthcheck()
    check("проверка связи проходит", report["ok"] and report["mode"] == "api")
    check("в отчёте видно, что сессия активна",
          any("сессия активна" in value for _, value in report["steps"]),
          str(report["steps"]))

    gw = FakeGateway(accepted, [], fail_on={fr.LOGIN_PATH: fr.DeliveryError("Bad token")})
    report = await gw.healthcheck()
    check("неверный токен виден в проверке связи",
          not report["ok"] and "Bad token" in str(report["steps"]))

    # ------------------------------------- имя аккаунта шлюз не проверяет
    gw = FakeGateway(accepted, [])
    who = await gw.resolve_recipient("someone")
    check("получатель возвращается непроверенным",
          who is not None and not who.verified)
    check("бот знает, что имя недоступно",
          fr.ApiFragProvider.supports_name_lookup is False)
    check("у заглушки имя, наоборот, доступно",
          fr.MockProvider.supports_name_lookup is True)

    # --------------------------------------- без токена провайдер не создаётся
    saved = fr.settings.fragment_api_key
    fr.settings.fragment_api_key = ""
    try:
        fr.ApiFragProvider()
        created = True
    except RuntimeError:
        created = False
    fr.settings.fragment_api_key = saved
    check("без API-токена провайдер не запускается", not created)

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
