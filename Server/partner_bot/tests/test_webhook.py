"""Вебхук поставщика: подпись, повторы, закрытие заказа."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime
from app.services import webhook as hook
from app.services.fragment import DeliveryProvider

BUYER = 777
#: Выдуманный секрет. Настоящему в исходниках не место: репозиторий
#: читают больше людей, чем кажется, и история git ничего не забывает.
SECRET = "whsec_" + "0123456789abcdef" * 4
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    def to(self, chat_id):
        return [t for cid, t in self.sent if cid == chat_id]


class Provider(DeliveryProvider):
    def __init__(self, status="completed"):
        self.status = status
        self.asked = []

    async def order_status(self, order_id):
        self.asked.append(order_id)
        return {"order_id": order_id, "status": self.status}


def sign(body: bytes, key: str = SECRET) -> str:
    return hmac.new(key.encode(), body, sha256).hexdigest()


def event(order_id: str, status: str = "completed", event_id: str = "e-1") -> bytes:
    return json.dumps({
        "event": "order.status_changed",
        "event_id": event_id,
        "timestamp": "2026-08-21T02:26:25.519Z",
        "data": {"order_id": order_id, "type": "topup",
                 "status": status, "previous_status": "processing"},
    }).encode()


# ───────────────────────────────────────────────────────────── подпись


def signature() -> None:
    body = event("ord-1")
    good = sign(body)

    check("верная подпись принимается", hook.verify(body, good, SECRET))
    check("чужая подпись отвергается",
          not hook.verify(body, sign(body, "не тот секрет"), SECRET))
    check("подделка тела ломает подпись",
          not hook.verify(body + b" ", good, SECRET))
    check("пустая подпись — отказ", not hook.verify(body, "", SECRET))
    check("без секрета не принимаем ничего",
          not hook.verify(body, good, ""))

    # приставки и base64 — сервисы пишут подпись по-разному
    check("приставка sha256= понимается",
          hook.verify(body, f"sha256={good}", SECRET))
    check("подпись в base64 понимается",
          hook.verify(body, base64.b64encode(
              hmac.new(SECRET.encode(), body, sha256).digest()).decode(), SECRET))
    check("регистр hex не подгоняем молча",
          not hook.verify(body, good.upper(), SECRET))

    # секрет из панели важнее .env
    check("адрес собирается из настройки и пути",
          hook.PATH in hook.public_url() or not hook.public_url())


# ───────────────────────────────────────────────────────── обработка


async def handling(conn) -> None:
    await runtime.set_value(conn, "fazer_webhook_secret", SECRET)
    await db.upsert_user(conn, BUYER, "buyer", "Покупатель")
    await db.credit(conn, BUYER, 200_00)

    async def make(external: str):
        order = await db.create_order(
            conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
            recipient="1724367212", price=1400, cost=1090,
        )
        await db.update_order(conn, order.id, fragment_order_id=external)
        return await db.get_order(conn, order.id)

    bot = FakeBot()
    order = await make("ord-100")
    result = await hook.apply_event(
        bot, conn, Provider("completed"),
        json.loads(event("ord-100", "completed", "e-100")),
    )
    check("выполнение доводится до конца", result == "done", result)
    check("заказ закрыт как выполненный",
          (await db.get_order(conn, order.id)).status == db.ORDER_DELIVERED)
    check("клиенту написали", bool(bot.to(BUYER)), str(bot.sent))

    # повтор того же события ничего не делает
    repeat = await hook.apply_event(
        bot, conn, Provider("completed"),
        json.loads(event("ord-100", "completed", "e-100")),
    )
    check("повтор события отбрасывается", repeat == "повтор", repeat)

    # отказ поставщика возвращает деньги
    bot = FakeBot()
    failed = await make("ord-101")
    before = (await db.get_user(conn, BUYER)).balance
    result = await hook.apply_event(
        bot, conn, Provider("failed"),
        json.loads(event("ord-101", "failed", "e-101")),
    )
    check("отказ обрабатывается", result == "failed", result)
    check("деньги вернулись",
          (await db.get_user(conn, BUYER)).balance == before + 1400)

    # промежуточный статус заказ не трогает
    waiting = await make("ord-102")
    result = await hook.apply_event(
        bot, conn, Provider("processing"),
        json.loads(event("ord-102", "processing", "e-102")),
    )
    check("processing ничего не закрывает", result == "ещё в работе", result)
    check("заказ остался в работе",
          (await db.get_order(conn, waiting.id)).status == db.ORDER_DELIVERING)

    # чужой заказ
    result = await hook.apply_event(
        bot, conn, Provider("completed"),
        json.loads(event("ord-999", "completed", "e-999")),
    )
    check("чужой заказ пропускаем", result == "чужой заказ", result)

    # уже закрытый заказ повторно не трогаем
    result = await hook.apply_event(
        bot, conn, Provider("completed"),
        json.loads(event("ord-100", "completed", "e-103")),
    )
    check("закрытый заказ не перезакрываем", result == "заказ уже закрыт", result)

    # событие без номера заказа
    result = await hook.apply_event(
        bot, conn, Provider("completed"),
        {"event": "ping", "event_id": "e-104", "data": {}},
    )
    check("событие без номера не ломает приём",
          result == "без номера заказа", result)

    # событие без event_id обрабатываем, а не выбрасываем
    plain = await make("ord-105")
    result = await hook.apply_event(
        bot, conn, Provider("completed"),
        {"event": "order.status_changed",
         "data": {"order_id": "ord-105", "status": "completed"}},
    )
    check("событие без event_id всё равно сработает", result == "done", result)

    seen, last = await db.events_seen(conn)
    check("события записаны", seen >= 5, str(seen))
    check("время последнего известно", bool(last), last)


async def http_layer(conn) -> None:
    """Сам приёмник: что он отвечает на подделку и на нормальный отчёт."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    await runtime.set_value(conn, "fazer_webhook_secret", SECRET)
    order = await db.create_order(
        conn, user_id=BUYER, product_type="game:free_fire_br", quantity=1,
        recipient="1724367212", price=1400, cost=1090,
    )
    await db.update_order(conn, order.id, fragment_order_id="ord-200")

    app = web.Application(client_max_size=hook.MAX_BODY)
    app["bot"] = FakeBot()
    app["provider"] = Provider("completed")
    app.router.add_post(hook.PATH, hook.handle)
    app.router.add_get("/healthz", lambda _: web.json_response({"ok": True}))

    async with TestClient(TestServer(app)) as client:
        body = event("ord-200", "completed", "e-200")

        resp = await client.post(hook.PATH, data=body)
        check("без подписи — 401", resp.status == 401, str(resp.status))

        resp = await client.post(hook.PATH, data=body,
                                 headers={"X-Signature": "деадбиф"})
        check("с чужой подписью — 401", resp.status == 401, str(resp.status))

        resp = await client.post(hook.PATH, data=body,
                                 headers={"X-Signature": sign(body)})
        check("с верной подписью — 200", resp.status == 200, str(resp.status))
        payload = await resp.json()
        check("заказ закрыт через HTTP", payload.get("result") == "done",
              str(payload))

        bad = "{это не json".encode()
        resp = await client.post(hook.PATH, data=bad,
                                 headers={"X-Signature": sign(bad)})
        check("кривой json — 400", resp.status == 400, str(resp.status))

        huge = b"x" * (hook.MAX_BODY + 10)
        resp = await client.post(hook.PATH, data=huge,
                                 headers={"X-Signature": sign(huge)})
        check("огромное тело не принимаем", resp.status in (413, 400),
              str(resp.status))

        resp = await client.get("/healthz")
        check("проверка живости отвечает", resp.status == 200, str(resp.status))


async def switch(conn) -> None:
    """Выключенный вебхук не мешает боту работать."""
    await runtime.set_value(conn, "webhook_port", "0")
    runner = await hook.serve(FakeBot(), Provider())
    check("без порта приёмник не поднимается", runner is None)

    await runtime.set_value(conn, "webhook_port", "8099")
    await runtime.set_value(conn, "fazer_webhook_secret", "")
    runner = await hook.serve(FakeBot(), Provider())
    check("без секрета приёмник не поднимается", runner is None)

    await runtime.set_value(conn, "fazer_webhook_secret", SECRET)
    runner = await hook.serve(FakeBot(), Provider())
    check("с портом и секретом поднимается", runner is not None)
    if runner is not None:
        await runner.cleanup()

    await runtime.set_value(conn, "webhook_public_url", "https://bot.example.com/")
    check("адрес для кабинета собран",
          hook.public_url() == "https://bot.example.com/webhook/fazer",
          hook.public_url())
    await runtime.set_value(conn, "webhook_port", "0")


async def root_page(conn) -> None:
    """Корень адреса не должен выглядеть сломанным."""
    from aiohttp import web

    from app import runtime
    from app.services import webhook as hook

    class Req:
        pass

    await runtime.set_value(conn, "api_enabled", "1")
    try:
        await hook._root(Req())
        check("с включённым API корень уводит на документацию", False,
              "перенаправления не было")
    except web.HTTPFound as moved:
        check("с включённым API корень уводит на документацию",
              moved.location.endswith("/api/v1/docs"), moved.location)

    await runtime.set_value(conn, "api_enabled", "0")
    answer = await hook._root(Req())
    check("без API корень отвечает понятно, а не пустотой",
          answer.status == 404 and "служебный" in answer.text, answer.text[:60])


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        signature()
        await handling(conn)
        await http_layer(conn)
        await switch(conn)
        await root_page(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
