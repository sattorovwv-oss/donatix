"""API для сторонних разработчиков: ключи, авторизация, покупка, вебхук.

Сервер поднимается настоящий, на свободном порту, и все проверки идут
по HTTP — так же, как пойдёт чужой код. Подменять обработчики значило бы
проверять не то, что работает у клиента.
"""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

import aiohttp
from aiohttp import web

from app import db, runtime
from app.api import catalog, guard, hooks
from app.api import keys as apikeys
from app.api import server as api_server
from app.services.fragment import DeliveryError, DeliveryProvider

BUYER = 501
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class Provider(DeliveryProvider):
    """Поставщик, который выдаёт всё и сразу."""

    def __init__(self, *, fail_stars=False):
        self.fail_stars = fail_stars
        self.delivered = []

    async def deliver_stars(self, recipient, quantity):
        if self.fail_stars:
            raise DeliveryError("нет средств на кошельке")
        self.delivered.append(("stars", recipient, quantity))
        return type("R", (), {"order_id": f"FZ-{len(self.delivered)}"})()

    async def deliver_premium(self, recipient, months):
        self.delivered.append(("premium", recipient, months))
        return type("R", (), {"order_id": "FZ-P"})()

    async def game_catalog(self):
        return []

    async def game_offers(self, category_id):
        return [{"offer_id": "off_1", "name": "100 алмазов",
                 "usd": Decimal("1.00"), "raw": {}}]


class Client:
    """Тонкая обёртка над HTTP: тело всегда JSON, ответ — (код, данные)."""

    def __init__(self, base: str, session: aiohttp.ClientSession):
        self.base, self.session = base, session

    async def call(self, method, path, *, key="", body=None, headers=None):
        head = dict(headers or {})
        if key:
            head["Authorization"] = f"Bearer {key}"
        async with self.session.request(
            method, self.base + path, headers=head,
            data=(json.dumps(body) if body is not None else None),
        ) as resp:
            try:
                return resp.status, await resp.json(), resp.headers
            except Exception:
                return resp.status, {"raw": await resp.text()}, resp.headers

    async def get(self, path, key="", **kw):
        return await self.call("GET", path, key=key, **kw)

    async def post(self, path, key="", **kw):
        return await self.call("POST", path, key=key, **kw)


async def make_key(conn, user_id=BUYER, label="test") -> str:
    raw = apikeys.generate()
    await db.add_api_key(
        conn, user_id=user_id, label=label, prefix=apikeys.prefix_of(raw),
        tail=apikeys.tail_of(raw), key_hash=apikeys.hash_key(raw),
    )
    return raw


# ───────────────────────────────────────────────── ключи


async def key_safety(conn) -> None:
    raw = apikeys.generate()
    stored = apikeys.hash_key(raw)

    check("ключ начинается с приставки", raw.startswith("sk_live_"), raw[:12])
    check("ключ достаточно длинный", len(raw) >= 40, str(len(raw)))
    check("два ключа не совпадают", apikeys.generate() != apikeys.generate())
    check("сам ключ в хеше не лежит", raw not in stored, stored[:30])
    check("свой ключ проходит", apikeys.verify(raw, stored))
    check("чужой ключ не проходит", not apikeys.verify(apikeys.generate(), stored))
    # Последний знак меняем на заведомо другой, а не на «x»: раз в шесть
    # десятков ключей «x» там и стоял бы, подделка совпала бы с настоящим
    # ключом и проверка падала бы без причины.
    forged = raw[:-1] + ("y" if raw[-1] == "x" else "x")
    check("подделка хвоста не проходит", not apikeys.verify(forged, stored),
          f"{raw[-4:]} → {forged[-4:]}")
    check("битый хеш не пропускает", not apikeys.verify(raw, "мусор"))
    check("соль у двух хешей разная",
          apikeys.hash_key(raw) != apikeys.hash_key(raw))
    check("непохожая строка отсеивается до базы",
          not apikeys.looks_like("Bearer abc") and apikeys.looks_like(raw))

    await make_key(conn)
    saved = (await db.api_keys_of(conn, BUYER))[0]
    check("в базе лежит только отпечаток",
          "scrypt$" in saved.key_hash and "sk_live_" not in saved.key_hash)
    check("видна только часть ключа",
          saved.masked.count("…") == 1 and len(saved.masked) < 24, saved.masked)
    check("дата создания сохранена", bool(saved.created_at), saved.created_at)

    await db.set_api_key_enabled(conn, saved.id, False)
    check("ключ выключается", not (await db.get_api_key(conn, saved.id)).live)
    await db.set_api_key_enabled(conn, saved.id, True)
    check("и включается обратно", (await db.get_api_key(conn, saved.id)).live)

    await db.revoke_api_key(conn, saved.id)
    check("отозванный ключ не работает",
          not (await db.get_api_key(conn, saved.id)).live)
    check("отозванный ключ пропал из списка",
          not [k for k in await db.api_keys_of(conn, BUYER) if k.id == saved.id])
    check("но строка осталась для учёта",
          await db.get_api_key(conn, saved.id) is not None)


# ───────────────────────────────────────────────── адрес вебхука


def hook_urls() -> None:
    bad = {
        "": "пустой", "http://a.tj/h": "не https",
        "https://127.0.0.1/h": "петля", "https://localhost/h": "localhost",
        "https://10.0.0.5/h": "частная сеть",
        "https://169.254.169.254/meta": "метаданные облака",
        "https://[::1]/h": "петля IPv6",
        "https://a.tj:8080/h": "чужой порт",
    }
    for url, why in bad.items():
        check(f"вебхук отклонён — {why}", bool(hooks.check_url(url)), url)
    check("внешний https принимается",
          not hooks.check_url("https://example.com/hooks"))

    body = b'{"event":"order.completed"}'
    sig = hooks.sign(body, "whsec_test")
    check("подпись считается", len(sig) == 64, sig[:16])
    check("подпись зависит от тела", hooks.sign(body + b" ", "whsec_test") != sig)
    check("подпись зависит от секрета", hooks.sign(body, "whsec_other") != sig)


# ───────────────────────────────────────────────── сам API


async def over_http(conn, bot, provider) -> None:
    app = web.Application()
    app["bot"] = bot
    app["provider"] = provider
    api_server.mount(app, bot, provider)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}{api_server.PREFIX}"

    async with aiohttp.ClientSession() as session:
        api = Client(base, session)
        try:
            await run_http(conn, api, provider)
        finally:
            await runner.cleanup()


async def run_http(conn, api: Client, provider) -> None:
    # Лимит проверяем отдельным разделом. Пока он не мешает: иначе
    # половина проверок ниже упёрлась бы в 429 и ничего не проверила.
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    key = await make_key(conn)

    # ---- авторизация ------------------------------------------------
    status, body, headers = await api.get("/balance")
    check("без ключа доступа нет", status == 401, str(status))
    check("отказ объяснён кодом", body["error"]["code"] == "invalid_key",
          str(body))
    check("в ответе есть номер запроса", "X-Request-Id" in headers,
          str(dict(headers)))

    status, body, _ = await api.get("/balance", key="sk_live_" + "A" * 32)
    check("выдуманный ключ не проходит", status == 401, str(status))
    check("о чужом ключе не рассказываем лишнего",
          body["error"]["message"] == "Ключ не принят.", str(body))

    status, _, _ = await api.get("/balance", headers={"Authorization": key})
    check("ключ без слова Bearer не принимается", status == 401, str(status))

    status, body, _ = await api.get("/balance", key=key)
    check("свой ключ проходит", status == 200, str(body))
    check("баланс приходит числом и строкой",
          isinstance(body["balance"]["amount"], int)
          and "amount_text" in body["balance"], str(body))

    # ---- чужой ключ ------------------------------------------------
    other = await make_key(conn, user_id=BUYER + 1, label="сосед")
    await db.upsert_user(conn, BUYER + 1, "neighbour", "Сосед")
    status, body, _ = await api.get("/user", key=other)
    check("у каждого ключа свой аккаунт",
          body["user"]["id"] == BUYER + 1, str(body["user"]["id"]))

    # ---- выключенный и отозванный ключ ------------------------------
    doomed_raw = await make_key(conn, label="на отзыв")
    doomed = (await db.api_keys_of(conn, BUYER))[0]
    await api.get("/balance", key=doomed_raw)          # прогреваем кеш
    await db.set_api_key_enabled(conn, doomed.id, False)
    apikeys.forget(doomed.id)
    status, body, _ = await api.get("/balance", key=doomed_raw)
    check("выключенный ключ отказывает сразу", status == 403, str(status))
    check("и говорит почему", body["error"]["code"] == "key_disabled", str(body))

    await db.revoke_api_key(conn, doomed.id)
    apikeys.forget(doomed.id)
    status, _, _ = await api.get("/balance", key=doomed_raw)
    check("отозванный ключ отказывает", status == 403, str(status))

    # ---- заблокированный клиент -------------------------------------
    await db.set_banned(conn, BUYER, True)
    status, body, _ = await api.get("/balance", key=key)
    check("заблокированному клиенту закрыто", status == 403, str(status))
    check("причина названа", body["error"]["code"] == "account_blocked", str(body))
    await db.set_banned(conn, BUYER, False)

    # ---- каталог ----------------------------------------------------
    status, body, _ = await api.get("/products", key=key)
    check("каталог отдаётся", status == 200 and body["success"], str(status))
    ids = [item["id"] for item in body["products"]]
    check("в каталоге есть звёзды", "stars" in ids, str(ids[:6]))
    check("себестоимость наружу не уходит",
          all("cost" not in item for item in body["products"]), str(ids[:3]))
    check("у товара есть валюта",
          all(item.get("currency") or item.get("unit_price")
              for item in body["products"]), str(body["products"][:1]))

    status, body, _ = await api.get("/products?type=premium", key=key)
    check("каталог фильтруется по типу",
          all(item["type"] == "premium" for item in body["products"]),
          str([i["id"] for i in body["products"]]))

    status, body, _ = await api.get("/products/stars", key=key)
    check("товар открывается по id", body["product"]["id"] == "stars", str(body))
    status, body, _ = await api.get("/products/нет-такого", key=key)
    check("несуществующий товар — 404", status == 404, str(status))

    # ---- баланс не даёт купить --------------------------------------
    status, body, _ = await api.post(
        "/order/create", key=key,
        body={"product_id": "stars", "quantity": 100, "customer": "@durov"},
    )
    check("без денег заказ не проходит", status == 402, str(body))
    check("сказано, сколько надо",
          body["error"]["required"] > 0 and body["error"]["code"] == "insufficient_funds",
          str(body["error"]))
    check("деньги не списаны",
          (await db.get_user(conn, BUYER)).balance == 0)

    # ---- покупка ----------------------------------------------------
    await db.credit(conn, BUYER, 100_000)
    before = (await db.get_user(conn, BUYER)).balance

    status, body, _ = await api.post(
        "/order/create", key=key,
        body={"product_id": "stars", "quantity": 100, "customer": "@durov"},
    )
    check("заказ создан", status == 200 and body["success"], str(body))
    ref = body.get("order_id", "")
    check("номер заказа выглядит как номер", ref.startswith("ORD-"), ref)
    check("статус вернулся", body["status"] in
          ("processing", "completed"), str(body.get("status")))
    check("сумма в ответе целым числом",
          isinstance(body["amount"], int) and body["amount"] > 0, str(body))
    check("номер транзакции вернулся",
          body["transaction_id"].startswith("TX-"), str(body.get("transaction_id")))

    after = (await db.get_user(conn, BUYER)).balance
    check("деньги списаны ровно один раз",
          after == before - body["amount"], f"{before} → {after}")
    check("товар выдан", ("stars", "@durov", 100) in provider.delivered,
          str(provider.delivered))

    # ---- журнал транзакций ------------------------------------------
    txs = await db.api_txs_of(conn, BUYER)
    check("списание попало в журнал", txs and txs[0]["kind"] == "charge", str(txs[:1]))
    check("в журнале есть баланс до и после",
          txs[0]["balance_before"] == before and txs[0]["balance_after"] == after,
          f"{txs[0]['balance_before']} → {txs[0]['balance_after']}")
    check("транзакция привязана к заказу", txs[0]["order_ref"] == ref, str(txs[0]))

    # ---- статус заказа ----------------------------------------------
    status, body, _ = await api.get(f"/order/status?order_id={ref}", key=key)
    check("статус заказа читается", status == 200 and body["order_id"] == ref,
          str(body))
    status, body, _ = await api.get(f"/orders/{ref}", key=key)
    check("заказ открывается и по пути", body["order_id"] == ref, str(body))
    status, _, _ = await api.get("/order/status?order_id=ORD-999999", key=key)
    check("чужого заказа нет", status == 404, str(status))

    status, body, _ = await api.get(f"/orders/{ref}", key=other)
    check("чужой заказ не показывается соседу", status == 404, str(status))

    status, _, _ = await api.get("/order/status", key=key)
    check("без номера заказа — 400", status == 400, str(status))

    status, body, _ = await api.get("/orders?limit=5", key=key)
    check("список заказов отдаётся",
          body["count"] >= 1 and body["orders"][0]["order_id"] == ref, str(body))
    status, body, _ = await api.get("/orders?limit=99999", key=key)
    check("огромный limit урезается", body["limit"] <= 100, str(body.get("limit")))

    # ---- идемпотентность --------------------------------------------
    before = (await db.get_user(conn, BUYER)).balance
    args = {"product_id": "stars", "quantity": 100, "customer": "@durov",
            "idempotency_key": "order-1001"}
    status, first, _ = await api.post("/order/create", key=key, body=args)
    status2, second, _ = await api.post("/order/create", key=key, body=args)
    check("повтор не создаёт второй заказ",
          first["order_id"] == second["order_id"], f"{first} / {second}")
    spent = before - (await db.get_user(conn, BUYER)).balance
    check("повтор не списал деньги дважды", spent == first["amount"],
          f"списано {spent}, цена {first['amount']}")

    status, body, _ = await api.post(
        "/order/create", key=key,
        body=dict(args, idempotency_key="кор"),
    )
    check("короткий ключ идемпотентности отклонён", status == 400, str(body))

    # тот же ключ у другого разработчика — другой заказ
    await db.credit(conn, BUYER + 1, 100_000)
    status, third, _ = await api.post("/order/create", key=other, body=args)
    check("ключи идемпотентности у разных клиентов не мешаются",
          third.get("order_id") not in ("", first["order_id"]), str(third))

    # ---- проверка входных данных ------------------------------------
    bad_cases = [
        ({}, "missing_product_id"),
        ({"product_id": "stars", "quantity": 100}, "missing_customer"),
        ({"product_id": "stars", "quantity": 0, "customer": "@a"}, "bad_quantity"),
        ({"product_id": "stars", "quantity": -5, "customer": "@a"}, "bad_quantity"),
        ({"product_id": "stars", "quantity": "сто", "customer": "@a"}, "bad_quantity"),
        ({"product_id": "stars", "quantity": 10 ** 9, "customer": "@a"}, "bad_quantity"),
        ({"product_id": "нет", "quantity": 1, "customer": "@a"}, "product_not_found"),
    ]
    for payload, expect in bad_cases:
        status, body, _ = await api.post("/order/create", key=key, body=payload)
        check(f"отклонено: {expect}",
              status >= 400 and body["error"]["code"] == expect,
              f"{payload} → {body.get('error')}")

    status, body, _ = await api.call(
        "POST", "/order/create", key=key,
        headers={"Content-Type": "application/json"},
    )
    check("пустое тело не ломает сервер", status == 400, str(body))

    async with api.session.post(
        api.base + "/order/create", data="{не json",
        headers={"Authorization": f"Bearer {key}"},
    ) as resp:
        check("кривой JSON не ломает сервер", resp.status == 400, str(resp.status))

    # ---- попытки внедрения ------------------------------------------
    sneaky = [
        "'; DROP TABLE users;--",
        "1 OR 1=1",
        "<script>alert(1)</script>",
        "../../etc/passwd",
        "%00",
    ]
    for probe in sneaky:
        status, body, _ = await api.get(
            f"/order/status?order_id={probe}", key=key)
        check(f"безвредно: {probe[:18]}", status == 404, str(status))
    check("таблица клиентов на месте",
          await db.get_user(conn, BUYER) is not None)

    status, body, _ = await api.post(
        "/order/create", key=key,
        body={"product_id": "stars", "quantity": 100,
              "customer": "<script>alert(1)</script>"},
    )
    if status == 200:
        saved = await db.api_order_by_ref(conn, body["order_id"])
        check("разметка сохраняется как есть, без выполнения",
              saved["customer"] == "<script>alert(1)</script>", str(saved))

    # ---- неизвестные адреса и методы --------------------------------
    status, body, _ = await api.get("/нет-такой-точки", key=key)
    check("неизвестный адрес — 404", status == 404, str(status))
    check("и это по-прежнему JSON", body.get("success") is False, str(body))
    status, _, _ = await api.get("/order/create", key=key)
    check("чужой метод — 405", status == 405, str(status))

    # ---- открытые точки ---------------------------------------------
    status, body, _ = await api.get("/health")
    check("проверка живости работает без ключа",
          status == 200 and body["status"] == "ok", str(body))
    status, _, _ = await api.get("/docs")
    check("документация открыта без ключа", status == 200, str(status))

    from app.api import docs as docspage

    html = docspage.html()
    check("в документации есть обозреватель каталога",
          all(mark in html for mark in ('id="browser"', 'id="apikey"',
                                        'id="chips"', "/games")))
    # Названия игр приходят от поставщика и попадают на страницу. Если
    # обозреватель когда-нибудь начнёт собирать их строкой, чужая кавычка
    # в названии станет дырой — поэтому сверяем, что он этого не делает.
    check("обозреватель не строит разметку строками",
          "innerHTML" not in docspage.JS)

    # ---- журнал запросов --------------------------------------------
    log = await db.api_requests_of(conn, BUYER, limit=200)
    check("запросы пишутся в журнал", len(log) > 10, str(len(log)))
    check("в журнале нет ключа",
          not any(key in json.dumps(row, ensure_ascii=False) for row in log))
    check("в журнале есть адрес и клиент",
          all(row["ip"] for row in log[:3]), str(log[:1]))
    check("у каждого запроса свой номер",
          len({row["request_id"] for row in log}) == len(log))

    fresh_key = await db.get_api_key(conn, (await db.api_keys_of(conn, BUYER))[-1].id)
    check("последний запрос ключа отмечен", bool(fresh_key.last_used_at),
          str(fresh_key.last_used_at))

    # ---- лимит запросов ----------------------------------------------
    await runtime.set_value(conn, "api_rate_per_min", "60")
    guard.forget_rate()
    hits = [await api.get("/health") for _ in range(3)]      # без ключа не считается
    key_id = (await db.api_keys_of(conn, BUYER))[-1].id
    codes = []
    for _ in range(guard.BURST + 5):
        status, _, headers = await api.get("/balance", key=key)
        codes.append(status)
    check("лимит срабатывает", 429 in codes, str(codes[-6:]))
    check("до лимита запросы проходят", codes[0] == 200, str(codes[:3]))

    status, body, headers = await api.get("/balance", key=key)
    if status == 429:
        check("сказано, через сколько повторить",
              "Retry-After" in headers, str(dict(headers)))
        check("код отказа машинный",
              body["error"]["code"] == "rate_limited", str(body))
    guard.forget_rate()

    # ---- защита от подбора -------------------------------------------
    guard.forget_bad()
    codes = []
    for index in range(guard.BAD_LIMIT + 2):
        status, _, _ = await api.get(
            "/balance", key="sk_live_" + "B" * 31 + str(index % 10))
        codes.append(status)
    check("подбор ключа закрывает адрес", codes[-1] == 429, str(codes))
    status, _, _ = await api.get("/balance", key=key)
    check("после блокировки не пускают и с верным ключом",
          status == 429, str(status))
    guard.forget_bad()
    status, _, _ = await api.get("/balance", key=key)
    check("после снятия блокировки всё работает", status == 200, str(status))

    # ---- выключенный API ---------------------------------------------
    await runtime.set_value(conn, "api_enabled", "0")
    status, body, _ = await api.get("/balance", key=key)
    check("выключенный API отвечает 503", status == 503, str(status))
    check("и говорит, что выключен",
          body["error"]["code"] == "api_disabled", str(body))
    status, _, _ = await api.get("/health")
    check("живость видна и при выключенном API", status == 200, str(status))
    await runtime.set_value(conn, "api_enabled", "1")


# ───────────────────────────────────────────────── возврат денег


async def refunds(conn, bot) -> None:
    """Поставщик отказал — деньги должны вернуться, и это должно быть видно."""
    from app.services import delivery

    provider = Provider(fail_stars=True)
    bot.sent.clear()
    await db.credit(conn, BUYER, 50_000)
    before = (await db.get_user(conn, BUYER)).balance

    key = await make_key(conn, label="возврат")
    key_row = (await db.api_keys_of(conn, BUYER))[0]
    ref = await db.next_api_ref(conn)
    paid, tx = await db.charge_logged(conn, BUYER, 1400, order_ref=ref,
                                      note="звёзды")
    check("списание прошло", paid and tx.startswith("TX-"), tx)

    order = await db.create_order(
        conn, user_id=BUYER, product_type="stars", quantity=100,
        recipient="@durov", price=1400, cost=1000,
    )
    await db.link_api_order(conn, ref=ref, order_id=order.id, key_id=key_row.id,
                            user_id=BUYER, product_id="stars", customer="@durov")
    await delivery.run(bot, conn, provider, order)

    fresh = await db.get_order(conn, order.id)
    check("заказ помечен возвращённым", fresh.status == db.ORDER_REFUNDED,
          fresh.status)
    check("деньги вернулись на баланс",
          (await db.get_user(conn, BUYER)).balance == before, str(before))

    txs = await db.api_txs_of(conn, BUYER, limit=3)
    check("возврат записан в журнал", txs[0]["kind"] == "refund", str(txs[0]))
    check("возврат привязан к тому же заказу", txs[0]["order_ref"] == ref,
          str(txs[0]))
    check("журнал сходится по балансу",
          txs[0]["balance_after"] == before, str(txs[0]))

    # Возврат — это ровно то, ради чего разработчика и стоит будить: он
    # теряет деньги и клиента. По умолчанию такие письма приходят, а
    # «заказ выполнен» — нет: это проверяет отдельный раздел ниже.
    check("о возврате разработчику сообщили",
          any(chat == BUYER for chat, _ in bot.sent), str(bot.sent[:2]))
    check("владельцу о сбое сообщили", bool(bot.sent), str(len(bot.sent)))

    # А если он просил тишины — молчим и на возврате тоже.
    await db.set_api_prefs(conn, BUYER, notify="off")
    bot.sent.clear()
    quiet = await db.next_api_ref(conn)
    await db.charge_logged(conn, BUYER, 1400, order_ref=quiet, note="звёзды")
    silent = await db.create_order(
        conn, user_id=BUYER, product_type="stars", quantity=100,
        recipient="@durov", price=1400, cost=1000,
    )
    await db.link_api_order(conn, ref=quiet, order_id=silent.id,
                            key_id=key_row.id, user_id=BUYER,
                            product_id="stars", customer="@durov")
    await delivery.run(bot, conn, provider, silent)
    check("выключенные уведомления уважаются и при возврате",
          not any(chat == BUYER for chat, _ in bot.sent), str(bot.sent[:2]))
    await db.set_api_prefs(conn, BUYER, notify="problems")

    # Три неудачи подряд — и бот сам гасит продажу, чтобы клиенты не
    # платили за возвраты. Это верное поведение, но следующим разделам
    # нужен рабочий магазин, поэтому счётчик сбрасываем.
    check("подряд идущие неудачи гасят продажу",
          runtime.get_bool("autostopped")
          or runtime.get_int("fail_streak") > 0,
          f"серия {runtime.get_int('fail_streak')}")
    await runtime.set_value(conn, "fail_streak", "0")
    await runtime.set_value(conn, "autostopped", "0")
    await runtime.set_value(conn, "stars_enabled", "1")
    await runtime.set_value(conn, "premium_enabled", "1")

    # заказ из бота — человеку писать надо
    bot.sent.clear()
    human = await db.create_order(
        conn, user_id=BUYER, product_type="stars", quantity=100,
        recipient="@durov", price=1400, cost=1000,
    )
    await db.charge(conn, BUYER, 1400)
    await delivery.run(bot, conn, provider, human)
    check("человеку о возврате пишут",
          any(chat == BUYER for chat, _ in bot.sent), str(bot.sent[:2]))


# ───────────────────────────────────────────────── вебхук клиенту


async def webhook_out(conn) -> None:
    """Проверяем настоящей доставкой: поднимаем сервер клиента."""
    got = []

    async def receive(request):
        body = await request.read()
        got.append((dict(request.headers), body))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    try:
        secret = hooks.new_secret()
        await db.set_api_hook(conn, BUYER, f"http://127.0.0.1:{port}/hook", secret)

        rows = await db.api_orders_to_notify(conn)
        check("есть о чём сообщить", bool(rows), str(len(rows)))

        # check_url в доставке отвергает петлю — это правильно, и это
        # ровно то, что мы хотим проверить отдельно.
        async with aiohttp.ClientSession() as session:
            row = rows[0]
            await hooks.deliver(session, conn, row)
        check("на внутренний адрес не пошли", not got, str(got))
        hook = await db.get_api_hook(conn, BUYER)
        check("причина отказа записана", bool(hook["last_error"]),
              hook["last_error"])
        check("счётчик неудач вырос", hook["fails"] >= 1, str(hook["fails"]))

        outside = await db.get_api_hook(conn, BUYER + 1)
        await db.set_api_hook(conn, BUYER + 1,
                              f"https://127.0.0.1:{port}/hook", "whsec_x")
        saved = await db.get_api_hook(conn, BUYER + 1)
        check("внутренний адрес по https тоже не вызывается",
              bool(hooks.check_url(saved["url"])), saved["url"])

        # А теперь проверим само тело и подпись, без сетевого запрета.
        body = json.dumps(hooks.payload_of(rows[0]), ensure_ascii=False).encode()
        payload = json.loads(body)
        check("в событии есть номер заказа",
              payload["order_id"].startswith("ORD-"), str(payload))
        check("в событии есть статус",
              payload["status"] in
              ("processing", "completed", "refunded"), str(payload))
        check("событие названо по статусу",
              payload["event"] == f"order.{payload['status']}", str(payload))
        check("ключа в событии нет", "sk_live_" not in body.decode())

        stamp = "1789600000"
        signature = hooks.sign(body, secret, stamp)
        check("подпись со временем отличается от подписи без него",
              hooks.sign(body, secret) != signature)
        check("другое время — другая подпись",
              hooks.sign(body, secret, "1789600001") != signature)
        check("клиент может проверить подпись",
              hooks.sign(body, secret, stamp) == signature)
        check("чужим секретом подпись не сходится",
              hooks.sign(body, "whsec_other", stamp) != signature)
    finally:
        await runner.cleanup()


async def hook_marks(conn) -> None:
    """Состояние рассылки живёт в базе и не теряется при перезапуске."""
    rows = await db.api_orders_to_notify(conn)
    if not rows:
        check("нечего проверять — очередь пуста", True)
        return
    ref, status = rows[0]["ref"], rows[0]["status"]

    await db.note_api_hook(conn, ref, sent=status)
    left = [row["ref"] for row in await db.api_orders_to_notify(conn)]
    check("отправленное уходит из очереди", ref not in left, str(left[:3]))

    await db.note_api_hook(conn, ref, failed=True)
    saved = await db.api_order_by_ref(conn, ref)
    check("неудачи считаются", saved["hook_tries"] == 1, str(saved["hook_tries"]))

    second = [row for row in await db.api_orders_to_notify(conn)][0]["ref"]
    for _ in range(10):
        await db.note_api_hook(conn, second, failed=True)
    still = [row["ref"] for row in await db.api_orders_to_notify(conn)]
    check("после восьми неудач не долбимся вечно", second not in still,
          str(still[:3]))



# ───────────────────────────────────────────── цены для разработчиков


async def wholesale_prices(conn) -> None:
    """Разработчику продаём по закупке плюс процент, а не по витрине.

    Проверяем три вещи, каждая из которых стоит денег, если сломается:
    цену видно ту же, что спишется; себестоимость наружу не уходит; там,
    где закупка неизвестна, цена витрины остаётся — продать ниже закупки
    хуже, чем продать дороже.
    """
    await runtime.set_value(conn, "star_cost_e4", "1000")    # 0.10 с. за звезду
    await runtime.set_value(conn, "star_price_e4", "1500")   # витрина 0.15
    await runtime.save_premium_plans(conn, [{"months": 3, "price": 15000},
                                            {"months": 12, "price": 40000}])
    await runtime.save_premium_costs(conn, {3: 10000})       # у 12 мес. закупки нет
    await runtime.set_value(conn, "api_margin", "0")

    retail = {item["id"]: item for item in await catalog.listing(conn, None)}
    check("без наценки цены как в боте",
          retail["premium:3"]["amount"] == 15000
          and retail["stars"]["unit_price"] == 1500,
          f"{retail['premium:3']['amount']}, {retail['stars']['unit_price']}")

    await runtime.set_value(conn, "api_margin", "8")
    api = {item["id"]: item for item in await catalog.listing(conn, None)}

    check("Premium продаётся по закупке плюс 8%",
          api["premium:3"]["amount"] == 10800, str(api["premium:3"]["amount"]))
    check("звезда считается от закупки, а не от витрины",
          api["stars"]["unit_price"] == 1080, str(api["stars"]["unit_price"]))
    check("без закупки остаётся цена витрины",
          api["premium:12"]["amount"] == 40000, str(api["premium:12"]["amount"]))

    # Списывается ровно то, что показано: иначе разработчик считает одно,
    # а платит другое, и первая же сверка превращается в спор.
    check("спишется ровно показанная цена",
          catalog.price_of(api["premium:3"], 1) == api["premium:3"]["amount"])
    check("сотня звёзд считается по той же цене за штуку",
          catalog.price_of(api["stars"], 100) == 1080,
          str(catalog.price_of(api["stars"], 100)))

    # Цена в долларах рядом с ценой в сомони: разработчику называют цены
    # в долларах, и без пересчёта он не сравнит наши со своими.
    await runtime.set_value(conn, "usd_rate_diram", "1090")
    in_usd = catalog.public(api["premium:3"])
    check("рядом с ценой есть доллары",
          abs(in_usd["usd"] - api["premium:3"]["amount"] / 1090) < 0.001,
          str(in_usd.get("usd")))
    check("у звёзд доллары за штуку",
          catalog.public(api["stars"]).get("usd_per_unit", 0) > 0,
          str(catalog.public(api["stars"]).get("usd_per_unit")))

    # Без курса не выдумываем: лучше не показать, чем показать неверное.
    await runtime.set_value(conn, "usd_rate_diram", "0")
    check("без курса долларов не показываем",
          "usd" not in catalog.public(api["premium:3"]))
    await runtime.set_value(conn, "usd_rate_diram", "1090")

    shown = catalog.public(api["premium:3"])
    check("себестоимость наружу не уходит",
          not any(key in shown for key in catalog.HIDDEN),
          ", ".join(k for k in catalog.HIDDEN if k in shown) or "чисто")
    shown_stars = catalog.public(api["stars"])
    check("и у звёзд тоже",
          "cost_unit_e4" not in shown_stars and "wholesale" not in shown_stars)

    # Отрицательная наценка — это продажа в убыток по опечатке.
    await runtime.set_value(conn, "api_margin", "-20")
    guarded = {item["id"]: item for item in await catalog.listing(conn, None)}
    check("минус в наценке не уводит цену ниже закупки",
          guarded["premium:3"]["amount"] == 15000,
          str(guarded["premium:3"]["amount"]))

    await runtime.set_value(conn, "api_margin", "0")



# ──────────────────────────────────── весь каталог поставщика


class GamesProvider(Provider):
    """Поставщик с двумя играми: одна заведена в боте, второй там нет."""

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []
        self.ordered: list[dict] = []

    async def game_categories(self):
        return [{"category_id": "in_bot", "name": "Игра из бота"},
                {"category_id": "not_in_bot", "name": "Новая игра"}]

    async def game_catalog(self):
        return [{"category_id": "not_in_bot", "name": "Новая игра",
                 "fields": [{"name": "user_id"}, {"name": "server_id"}]}]

    async def game_offers(self, category_id):
        self.asked.append(category_id)
        return [{"offer_id": "p1", "name": "100 алмазов",
                 "usd": Decimal("1.00"), "raw": {}}]

    async def order_game(self, **kw):
        self.ordered.append(kw)
        return {"order_id": "SUP-1", "status": "processing"}

    async def order_status(self, external_id):
        return {"status": "completed"}


async def full_supplier_catalog(conn) -> None:
    """Каталог целиком: игры, которых в боте нет, тоже продаются."""
    from app.services import games as gsvc

    supplier = GamesProvider()
    await runtime.set_value(conn, "games_enabled", "1")
    await runtime.set_value(conn, "margin_percent", "20")
    await db.add_game(conn, category_id="in_bot", title="Игра из бота")
    await db.update_game(conn, "in_bot", enabled=1)

    await runtime.set_value(conn, "api_all_games", "0")
    gsvc.forget_catalog()
    only_mine = [i["id"] for i in await catalog.listing(conn, supplier, "game")]
    check("по умолчанию отдаём только свои игры",
          only_mine == ["game:in_bot:p1"], str(only_mine))

    await runtime.set_value(conn, "api_all_games", "1")
    gsvc.forget_catalog()
    whole = {i["id"]: i for i in await catalog.listing(conn, supplier, "game")}
    check("весь каталог поставщика отдаётся",
          set(whole) == {"game:in_bot:p1", "game:not_in_bot:p1"},
          ", ".join(sorted(whole)))

    new = whole["game:not_in_bot:p1"]
    check("у чужой игры взяты её поля",
          new["fields"] == ["user_id", "server_id"], str(new["fields"]))
    check("название взято у поставщика",
          new["game"] == "Новая игра", new["game"])
    check("служебная пометка наружу не уходит",
          "shadow" not in catalog.public(new))

    # Игры, которой нет в боте, в списке игр владельца быть не должно:
    # иначе один заход в каталог засорил бы ему панель.
    listed = {game.category_id for game in await db.list_games(conn)}
    check("каталог не заводит игру в боте сам по себе",
          "not_in_bot" not in listed, ", ".join(sorted(listed)))

    # Второй заход не должен снова дёргать поставщика: категорий у него
    # десятки, и каждый запрос — отдельное обращение по сети.
    before = len(supplier.asked)
    await catalog.listing(conn, supplier, "game")
    check("собранный каталог не пересобирается на каждый запрос",
          len(supplier.asked) == before, f"{before} → {len(supplier.asked)}")

    # А вот при покупке игра заводится — и выключенной, чтобы не всплыть
    # в меню бота без ведома владельца.
    game = await catalog.game_for(conn, new)
    check("при покупке игра заводится сама", game is not None
          and game.category_id == "not_in_bot")
    check("и заведена выключенной", game is not None and not game.enabled,
          str(game.enabled if game else "нет"))
    check("поля перенесены как есть",
          game is not None and game.field_names == ["user_id", "server_id"],
          str(game.field_names if game else []))

    await runtime.set_value(conn, "api_all_games", "0")
    await runtime.set_value(conn, "games_enabled", "0")
    gsvc.forget_catalog()


def gsvc_forget() -> None:
    from app.services import games as gsvc

    gsvc.forget_catalog()


async def catalog_paging(conn, bot) -> None:
    """Каталог страницами: пять тысяч позиций одним ответом не отдаём."""
    supplier = GamesProvider()
    await runtime.set_value(conn, "games_enabled", "1")
    await runtime.set_value(conn, "api_all_games", "1")
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    gsvc_forget()

    app = web.Application()
    app["bot"] = bot
    app["provider"] = supplier
    api_server.mount(app, bot, supplier)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)
            key = await make_key(conn, label="paging")

            _, body, _ = await api.get("/products?type=game&limit=1", key=key)
            check("страница отдаёт ровно столько, сколько просили",
                  body.get("count") == 1, str(body.get("count")))
            check("и говорит, сколько всего нашлось",
                  body.get("total") == 2, str(body.get("total")))

            _, second, _ = await api.get("/products?type=game&limit=1&offset=1",
                                         key=key)
            check("вторая страница — другой товар",
                  second["products"][0]["id"] != body["products"][0]["id"],
                  second["products"][0]["id"])

            _, one, _ = await api.get("/products?game=not_in_bot", key=key)
            check("каталог фильтруется по игре",
                  [i["game_id"] for i in one["products"]] == ["not_in_bot"],
                  str([i["game_id"] for i in one["products"]]))

            _, glist, _ = await api.get("/games", key=key)
            names = sorted(row["id"] for row in glist.get("games", []))
            check("список игр отдаётся отдельно",
                  names == ["in_bot", "not_in_bot"], str(names))
            check("у игры видно, сколько у неё пакетов",
                  all(row["packs"] >= 1 for row in glist.get("games", [])))
    finally:
        await runner.cleanup()

    await runtime.set_value(conn, "api_all_games", "0")
    await runtime.set_value(conn, "games_enabled", "0")
    gsvc_forget()



# ──────────────────────────────────── покупка игры


async def game_order(conn, bot) -> None:
    """Покупка игрового пакета через API — весь путь целиком.

    Этот путь не был закрыт проверками, и на нём жила настоящая поломка:
    разбор ID вызывался не так, как объявлен, и любая покупка игры через
    API падала внутренней ошибкой. Поэтому проверяем не кусок, а дорогу
    от запроса до списания.
    """
    supplier = GamesProvider()
    await runtime.set_value(conn, "games_enabled", "1")
    await runtime.set_value(conn, "api_all_games", "0")
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    gsvc_forget()

    # Игра с одним полем и игра с парой чисел: у Magic Chess и Mobile
    # Legends аккаунт задаётся ID и сервером, и разбор там другой.
    await db.add_game(conn, category_id="one_field", title="Одно поле")
    await db.update_game(conn, "one_field", enabled=1)
    await db.add_game(conn, category_id="two_fields", title="Два поля",
                      field="user_id,server_id")
    await db.update_game(conn, "two_fields", enabled=1)

    await db.credit(conn, BUYER, 100_000)

    app = web.Application()
    app["bot"] = bot
    app["provider"] = supplier
    api_server.mount(app, bot, supplier)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)
            key = await make_key(conn, label="games")

            before = (await db.get_user(conn, BUYER)).balance
            status, body, _ = await api.post(
                "/order/create", key=key,
                body={"product_id": "game:one_field:p1", "quantity": 1,
                      "customer": "1724367212"},
            )
            check("покупка игры проходит", status == 200 and body.get("success"),
                  str(body)[:200])
            check("заказ получил номер", bool(body.get("order_id")),
                  str(body.get("order_id")))
            check("ID игрока ушёл поставщику как есть",
                  supplier.ordered and
                  supplier.ordered[-1]["fields"] == {"user_id": "1724367212"},
                  str(supplier.ordered[-1]["fields"] if supplier.ordered else {}))

            after = (await db.get_user(conn, BUYER)).balance
            check("деньги списаны ровно на цену заказа",
                  before - after == body.get("amount"),
                  f"{before} - {after} vs {body.get('amount')}")

            status, second, _ = await api.post(
                "/order/create", key=key,
                body={"product_id": "game:two_fields:p1", "quantity": 1,
                      "customer": "1724367212 2001"},
            )
            check("игра с двумя полями тоже покупается",
                  status == 200 and second.get("success"), str(second)[:200])
            check("оба числа разошлись по своим полям",
                  supplier.ordered[-1]["fields"] ==
                  {"user_id": "1724367212", "server_id": "2001"},
                  str(supplier.ordered[-1]["fields"]))

            status, bad, _ = await api.post(
                "/order/create", key=key,
                body={"product_id": "game:two_fields:p1", "quantity": 1,
                      "customer": "1724367212"},
            )
            check("без второго числа заказ отклонён с понятным ответом",
                  status == 400 and bad["error"]["code"] == "bad_customer",
                  str(bad)[:160])

            paid = (await db.get_user(conn, BUYER)).balance
            check("за отклонённый заказ денег не взяли", paid == after - second["amount"],
                  f"{paid} vs {after - second['amount']}")
    finally:
        await runner.cleanup()

    await runtime.set_value(conn, "games_enabled", "0")
    gsvc_forget()



# ──────────────────────────────────── кабинет и периоды


async def cabinet_view(conn, bot) -> None:
    """Данные для кабинета: периоды, сводка, выписка, сама страница."""
    from app.api import cabinet as cab

    check("минус в сумме не ломает показ",
          catalog.money(-779)["amount_text"] == "-7.79",
          catalog.money(-779)["amount_text"])
    check("ноль показывается как 0.00",
          catalog.money(0)["amount_text"] == "0.00")

    supplier = GamesProvider()
    await runtime.set_value(conn, "games_enabled", "1")
    await runtime.set_value(conn, "api_all_games", "0")
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    gsvc_forget()
    await db.credit(conn, BUYER, 100_000)

    app = web.Application()
    app["bot"] = bot
    app["provider"] = supplier
    api_server.mount(app, bot, supplier)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)
            key = await make_key(conn, label="cabinet")

            _, before, _ = await api.get("/orders/summary?period=today", key=key)
            was = before["orders"]

            for _ in range(2):
                await api.post("/order/create", key=key,
                               body={"product_id": "game:one_field:p1",
                                     "quantity": 1, "customer": "1724367212"})

            _, now, _ = await api.get("/orders/summary?period=today", key=key)
            check("сводка за сегодня видит новые заказы",
                  now["orders"] == was + 2, f"{was} → {now['orders']}")
            check("и считает выполненные",
                  now["completed"] >= 2, str(now["completed"]))
            check("потрачено показано суммой",
                  now["spent"]["amount"] > 0, str(now["spent"]))

            _, past, _ = await api.get("/orders/summary?period=yesterday", key=key)
            check("во вчера сегодняшние заказы не попали",
                  past["orders"] == 0, str(past["orders"]))

            _, page, _ = await api.get("/orders?period=today&limit=1", key=key)
            check("заказы отдаются страницами",
                  len(page["orders"]) == 1 and page["total"] >= 2,
                  f"{len(page['orders'])} из {page['total']}")

            status, bad, _ = await api.get("/orders?period=позавчера", key=key)
            check("непонятный период — понятный отказ, а не молчание",
                  status == 400 and bad["error"]["code"] == "bad_period",
                  str(bad)[:120])

            _, txs, _ = await api.get("/transactions?period=today", key=key)
            charges = [x for x in txs["transactions"] if x["kind"] == "charge"]
            check("выписка показывает списания", len(charges) >= 2,
                  str(len(charges)))
            check("списание показано со знаком минус",
                  charges[0]["amount"] < 0
                  and charges[0]["amount_text"].startswith("-"),
                  charges[0]["amount_text"])
            check("у списания видно, к какому заказу оно",
                  bool(charges[0]["order_id"]), charges[0]["order_id"])

            # Часовой пояс клиента: в Душанбе день начинается на пять
            # часов раньше UTC, и «сегодня» должно считаться по нему.
            _, east, _ = await api.get(
                "/orders/summary?period=today&tz=300", key=key)
            check("часовой пояс учитывается", "orders" in east, str(east)[:80])

            status, _, _ = await api.get("/cabinet", key="")
            check("кабинет открывается без ключа", status == 200, str(status))
    finally:
        await runner.cleanup()

    page = cab.html()
    for mark in ('id="apikey"', 'data-tab="orders"', 'data-p="7d"',
                 'id="periods"', 'id="whose"', "/orders/summary"):
        check(f"в кабинете есть {mark}", mark in page)
    check("кабинет не строит разметку строками", "innerHTML" not in cab.JS)
    # Отказ должен убирать с экрана всё, что относится ко входу: иначе
    # при непринятом ключе остаются вкладки и периоды, как будто пустило.
    check("при отказе экран возвращается в исходное",
          "function shut()" in cab.JS and "shut();" in cab.JS)
    check("негодный ключ не остаётся во вкладке",
          "removeItem('k')" in cab.JS)

    await runtime.set_value(conn, "games_enabled", "0")
    gsvc_forget()



# ──────────────────────────────────── вход по ссылке из бота


async def cabinet_pass(conn, bot) -> None:
    """Пропуск в кабинет: смотреть — можно, тратить — нет."""
    from app.api import keys as ak

    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    guard.forget_bad()
    ak.forget_passes()

    token = ak.generate_pass()
    check("пропуск отличается от ключа приставкой",
          token.startswith("cab_") and not ak.looks_like(token), token[:8])
    check("а ключ не сходит за пропуск",
          not ak.looks_like_pass(ak.generate()))

    await db.add_api_pass(conn, user_id=BUYER, prefix=ak.pass_prefix_of(token),
                          token_hash=ak.hash_key(token))

    app = web.Application()
    app["bot"] = bot
    app["provider"] = Provider()
    api_server.mount(app, bot, app["provider"])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)

            status, body, _ = await api.get("/user", key=token)
            check("по ссылке из бота кабинет открывается без ключа",
                  status == 200 and body["user"]["id"] == BUYER, str(status))
            check("и видно, что это только просмотр",
                  body["user"]["read_only"] is True and body["user"]["key"] is None,
                  str(body["user"].get("read_only")))

            status, _, _ = await api.get("/orders?period=all", key=token)
            check("история по ссылке видна", status == 200, str(status))

            status, denied, _ = await api.post(
                "/order/create", key=token,
                body={"product_id": "stars", "quantity": 50,
                      "customer": "@durov"},
            )
            check("но заказ по ссылке сделать нельзя",
                  status == 403 and denied["error"]["code"] == "read_only",
                  str(denied)[:140])

            # Ссылка может утечь — пересылом, историей браузера, скриншотом.
            # Поэтому её отзыв должен срабатывать сразу, а не через минуту
            # жизни кеша.
            await db.drop_api_passes(conn, BUYER)
            ak.forget_passes()
            status, _, _ = await api.get("/user", key=token)
            check("отозванная ссылка перестаёт работать сразу",
                  status == 401, str(status))

            status, _, _ = await api.get("/user", key="cab_" + "x" * 32)
            check("выдуманный пропуск не пускает", status == 401, str(status))
    finally:
        await runner.cleanup()
        guard.forget_bad()

    from app.api import cabinet as cab

    check("страница умеет входить по ссылке", "t=" in cab.JS
          and "history.replaceState" in cab.JS)
    check("и даёт выйти на устройстве", 'id="out"' in cab.html())



# ──────────────────────────────────── деньги не пропадают


async def money_never_vanishes(conn, bot) -> None:
    """Заказ не завёлся после списания — деньги должны вернуться.

    Между списанием и заведением заказа есть щель: занятая база,
    кончившийся диск, любая ошибка записи. Раньше в этой щели деньги
    исчезали молча — ни заказа, ни следа, только пропавшая сумма.
    """
    supplier = Provider()
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    guard.forget_rate()
    await db.credit(conn, BUYER, 100_000)

    app = web.Application()
    app["bot"] = bot
    app["provider"] = supplier
    api_server.mount(app, bot, supplier)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    real = db.create_order

    async def broken(*a, **kw):
        raise RuntimeError("база занята")

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)
            key = await make_key(conn, label="money")

            before = (await db.get_user(conn, BUYER)).balance
            db.create_order = broken
            try:
                status, body, _ = await api.post(
                    "/order/create", key=key,
                    body={"product_id": "stars", "quantity": 50,
                          "customer": "@durov"},
                )
            finally:
                db.create_order = real

            check("поломка записи не притворяется удачей",
                  status == 500 and not body.get("success"), str(status))
            after = (await db.get_user(conn, BUYER)).balance
            check("деньги вернулись на баланс", after == before,
                  f"{before} → {after}")

            txs = await db.api_txs_of(conn, BUYER, limit=2)
            check("возврат записан в выписку, а не сделан молча",
                  any(row["kind"] == "refund" for row in txs),
                  ", ".join(row["kind"] for row in txs))

            # А обычная покупка после этого должна проходить как ни в чём
            # не бывало: откат не должен ломать следующий заказ.
            status, good, _ = await api.post(
                "/order/create", key=key,
                body={"product_id": "stars", "quantity": 50,
                      "customer": "@durov"},
            )
            check("следующий заказ проходит обычным порядком",
                  status == 200 and good.get("success"), str(status))
    finally:
        await runner.cleanup()



# ──────────────────────────────────── уведомления и сводка


class Told:
    """Бот, который запоминает, кому что написал."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def notifications(conn) -> None:
    """Разработчику пишем ровно столько, сколько он просил."""
    from app.api import digest
    from app.services import delivery

    bot = Told()
    ref = await db.next_api_ref(conn)
    order = await db.create_order(
        conn, user_id=BUYER, product_type="stars", quantity=50,
        recipient="@durov", price=1000, cost=800,
    )
    key = await make_key(conn, label="notify")
    row = await db.api_keys_of(conn, BUYER)
    await db.link_api_order(conn, ref=ref, order_id=order.id, key_id=row[0].id,
                            user_id=BUYER, product_id="stars",
                            customer="@durov", idem_key="")

    check("по умолчанию только проблемы",
          (await db.api_prefs(conn, BUYER))["notify"] == "problems")

    # Заказ выполнен — это не проблема, и в личку идти не должно.
    await db.transition_order(conn, order.id, expected=db.ORDER_DELIVERING,
                              new=db.ORDER_DELIVERED)
    await delivery.tell_buyer(bot, conn, order, "заказ выполнен")
    check("на «выполнен» разработчика не будим", bot.sent == [], str(bot.sent))

    # А возврат — будим.
    await db.transition_order(conn, order.id, expected=db.ORDER_DELIVERED,
                              new=db.ORDER_REFUNDED)
    await delivery.tell_buyer(bot, conn, order, "деньги вернулись")
    check("о возврате сообщаем", len(bot.sent) == 1, str(len(bot.sent)))

    await db.set_api_prefs(conn, BUYER, notify="off")
    await delivery.tell_buyer(bot, conn, order, "и об этом молчим")
    check("выключено — значит тишина даже на возврате",
          len(bot.sent) == 1, str(len(bot.sent)))

    await db.set_api_prefs(conn, BUYER, notify="all")
    await delivery.tell_buyer(bot, conn, order, "теперь обо всём")
    check("«все заказы» — пишем и об удачных", len(bot.sent) == 2,
          str(len(bot.sent)))

    # Человеку из бота пишем всегда, что бы ни стояло у разработчика.
    plain = await db.create_order(
        conn, user_id=BUYER + 7, product_type="stars", quantity=10,
        recipient="@someone", price=200, cost=100,
    )
    await db.upsert_user(conn, BUYER + 7, "human", "Человек")
    await delivery.tell_buyer(bot, conn, plain, "обычному клиенту")
    check("обычного покупателя настройки API не касаются",
          len(bot.sent) == 3, str(len(bot.sent)))

    # ---- сводка за сутки ----
    await db.set_api_prefs(conn, BUYER, notify="off", digest=1, digest_on="")
    bot.sent.clear()
    from datetime import datetime, timedelta, timezone

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    sent = await digest.run_once(bot, conn, tomorrow)
    check("сводка за прошедшие сутки уходит", sent == 1, str(sent))
    check("в сводке видно заказы и возвраты",
          "Сводка за сутки" in bot.sent[0][1], bot.sent[0][1][:60])

    bot.sent.clear()
    again = await digest.run_once(bot, conn, tomorrow)
    check("дважды за день сводку не шлём", again == 0, str(again))

    # Пустые сутки — молчим: «за сутки 0 заказов» каждое утро приучает
    # не читать наши сообщения вовсе.
    await db.set_api_prefs(conn, BUYER, digest_on="")
    bot.sent.clear()
    far = datetime.now(timezone.utc) + timedelta(days=40)
    check("пустую сводку не присылаем",
          await digest.run_once(bot, conn, far) == 0, str(bot.sent))

    await db.set_api_prefs(conn, BUYER, notify="problems", digest=0)



# ──────────────────────────────────── проверка ID и заявки на возврат


class Checker(GamesProvider):
    """Поставщик, который умеет отвечать про ник игрока."""

    def __init__(self, verdict="ok", name="AlijonTJ"):
        super().__init__()
        self.verdict, self.name = verdict, name
        self.asked = []
        self.looks = 0

    async def validate_game_id(self, category_id, fields):
        self.looks += 1
        return (self.name if self.verdict == "ok" else None), self.verdict


async def check_and_refund(conn, bot) -> None:
    """Ник до покупки и просьба о возврате, которую решает владелец."""
    from app.api import routes as api_routes

    supplier = Checker()
    await runtime.set_value(conn, "games_enabled", "1")
    await runtime.set_value(conn, "api_all_games", "0")
    await runtime.set_value(conn, "api_rate_per_min", "100000")
    await runtime.set_value(conn, "volsever_key", "")
    guard.forget_rate()
    gsvc_forget()
    api_routes.forget_checks()
    await db.credit(conn, BUYER, 100_000)

    await db.add_game(conn, category_id="one_field", title="Одно поле")
    await db.update_game(conn, "one_field", enabled=1)

    app = web.Application()
    app["bot"] = bot
    app["provider"] = supplier
    api_server.mount(app, bot, supplier)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}{api_server.PREFIX}"

    try:
        async with aiohttp.ClientSession() as session:
            api = Client(base, session)
            key = await make_key(conn, label="проверка")

            _, body, _ = await api.get(
                "/check-id?product_id=game:one_field:p1&customer=1724367212",
                key=key)
            check("ник приходит до покупки",
                  body.get("nickname") == "AlijonTJ" and body.get("valid"),
                  str(body)[:120])

            # Тот же ID второй раз не должен тратить чужой справочник:
            # у бесплатного из них сотня запросов в месяц.
            was = supplier.looks
            await api.get(
                "/check-id?product_id=game:one_field:p1&customer=1724367212",
                key=key)
            check("повтор берётся из памяти, а не из справочника",
                  supplier.looks == was, f"{was} → {supplier.looks}")

            status, bad, _ = await api.get(
                "/check-id?product_id=stars&customer=@durov", key=key)
            check("для звёзд проверка ID не предлагается",
                  status == 400 and bad["error"]["code"] == "not_checkable",
                  str(bad)[:100])

            # ---- заявка на возврат ----
            status, order, _ = await api.post(
                "/order/create", key=key,
                body={"product_id": "game:one_field:p1", "quantity": 1,
                      "customer": "1724367212"})
            ref = order.get("order_id")
            check("заказ для проверки возврата создан",
                  status == 200 and order.get("status") == "completed",
                  str(order)[:100])

            status, short, _ = await api.post(
                "/order/refund", key=key, body={"order_id": ref, "reason": "нет"})
            check("без внятной причины заявку не принимаем",
                  status == 400 and short["error"]["code"] == "missing_reason",
                  str(short)[:100])

            status, ask, _ = await api.post(
                "/order/refund", key=key,
                body={"order_id": ref, "reason": "алмазы не пришли игроку"})
            check("заявка принята и ждёт владельца",
                  status == 200 and ask["refund"]["status"] == "pending",
                  str(ask)[:120])

            paid = (await db.get_user(conn, BUYER)).balance
            status, again, _ = await api.post(
                "/order/refund", key=key,
                body={"order_id": ref, "reason": "ещё раз прошу"})
            check("повторная просьба не плодит заявки",
                  status == 200
                  and len(await db.refund_requests(conn, "pending")) == 1,
                  str(len(await db.refund_requests(conn, "pending"))))
            check("сама заявка денег не возвращает",
                  (await db.get_user(conn, BUYER)).balance == paid,
                  f"{paid} → {(await db.get_user(conn, BUYER)).balance}")

            status, foreign, _ = await api.post(
                "/order/refund", key=key,
                body={"order_id": "ORD-999999", "reason": "чужой заказ"})
            check("на чужой заказ заявку не оставить",
                  status == 404, str(status))

            # ---- решение владельца ----
            row = await db.api_order_by_ref(conn, ref)
            before = (await db.get_user(conn, BUYER)).balance
            check("решение записывается один раз",
                  await db.decide_refund(conn, ref, approved=True)
                  and not await db.decide_refund(conn, ref, approved=True))

            order_row = await db.get_order(conn, row["order_id"])
            await db.transition_order(conn, order_row.id,
                                      expected=order_row.status,
                                      new=db.ORDER_REFUNDED, error="по заявке")
            from app.services import delivery

            await delivery._give_back(conn,
                                      await db.get_order(conn, order_row.id))
            after = (await db.get_user(conn, BUYER)).balance
            check("после согласия деньги вернулись",
                  after == before + order_row.price,
                  f"{before} → {after}")

            status, done, _ = await api.post(
                "/order/refund", key=key,
                body={"order_id": ref, "reason": "уже вернули"})
            check("за возвращённый заказ заявку не принимаем",
                  status == 409 and done["error"]["code"] == "already_refunded",
                  str(done)[:100])
    finally:
        await runner.cleanup()

    await runtime.set_value(conn, "games_enabled", "0")
    gsvc_forget()


# ───────────────────────────────────────────────── запуск


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    bot = FakeBot()
    provider = Provider()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await runtime.set_value(conn, "api_enabled", "1")
        await runtime.set_value(conn, "stars_enabled", "1")
        await runtime.set_value(conn, "premium_enabled", "1")
        await runtime.set_value(conn, "usd_rate_diram", "1090")
        await db.upsert_user(conn, BUYER, "dev", "Разработчик")

        await key_safety(conn)
        hook_urls()
        await over_http(conn, bot, provider)
        await refunds(conn, bot)
        await webhook_out(conn)
        await hook_marks(conn)
        await wholesale_prices(conn)
        await full_supplier_catalog(conn)
        await catalog_paging(conn, bot)
        await game_order(conn, bot)
        await cabinet_view(conn, bot)
        await cabinet_pass(conn, bot)
        await money_never_vanishes(conn, bot)
        await notifications(conn)
        await check_and_refund(conn, bot)
    finally:
        await conn.close()

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
