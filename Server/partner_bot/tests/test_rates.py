"""Курс доллара: разбор ответов источников, надбавка, отказы, панель."""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from datetime import datetime, timezone

from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import PrivateAttr

from app import db, runtime
from app.handlers import panel
from app.services import pricing, rates

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class SpyMessage(Message):
    _log: list = PrivateAttr(default_factory=list)

    async def edit_text(self, text, reply_markup=None, **kw):
        self._log.append(text)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self._log.append(text)
        return self

    @property
    def last(self) -> str:
        return self._log[-1] if self._log else ""


class SpyCallback(CallbackQuery):
    _alerts: list = PrivateAttr(default_factory=list)

    async def answer(self, text="", **kw):
        if text:
            self._alerts.append(text)

    @property
    def last(self) -> str:
        return self.message.last

    @property
    def alerts(self) -> list:
        return self._alerts


def call_of(data: str) -> SpyCallback:
    user = User(id=111, is_bot=False, first_name="Админ")
    message = SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=111, type="private"), from_user=user,
    )
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=message,
    )


# ────────────────────────────── подменяем сеть, оставляя разбор настоящим


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def json(self, content_type=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Отдаёт заготовленные ответы по URL и запоминает, кого спрашивали."""

    def __init__(self, replies: dict):
        self.replies, self.asked = replies, []

    def get(self, url):
        self.asked.append(url)
        reply = self.replies.get(url)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return FakeResponse({}, status=404)
        return reply if isinstance(reply, FakeResponse) else FakeResponse(reply)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def with_session(session):
    """Подменить aiohttp-сессию внутри модуля курса."""
    rates.aiohttp.ClientSession = lambda *a, **kw: session


URLS = [url for _, url, _ in rates.SOURCES]
REAL_SESSION = rates.aiohttp.ClientSession


async def rate_tests() -> None:
    # первый источник отвечает
    session = FakeSession({URLS[0]: {"rates": {"TJS": 10.9, "EUR": 0.9}}})
    with_session(session)
    rate = await rates.fetch()
    check("курс разобран из первого источника", rate.diram == 1090, str(rate.diram))
    check("источник назван", rate.source == "open.er-api.com", rate.source)
    check("лишние источники не дёргаются", len(session.asked) == 1, str(session.asked))

    # второй формат ответа
    session = FakeSession({URLS[1]: {"usd": {"tjs": 11.25}}})
    with_session(session)
    rate = await rates.fetch()
    check("второй формат ответа тоже понят", rate.diram == 1125, str(rate.diram))
    check("до него дошли через первый", len(session.asked) == 2, str(session.asked))

    # надбавка
    session = FakeSession({URLS[0]: {"rates": {"TJS": 10}}})
    with_session(session)
    rate = await rates.fetch(spread_percent=3)
    check("надбавка прибавляется", rate.diram == 1030, str(rate.diram))
    check("биржевой курс сохранён отдельно", rate.value == Decimal("10"))
    rate = await rates.fetch(spread_percent=0)
    check("без надбавки курс чистый", rate.diram == 1000)

    # мусор в ответе
    session = FakeSession({URLS[0]: {"rates": {"TJS": 0.0001}},
                           URLS[1]: {"usd": {"tjs": 10.5}}})
    with_session(session)
    rate = await rates.fetch()
    check("бессмысленный курс отбрасывается", rate.diram == 1050, str(rate.diram))

    session = FakeSession({URLS[0]: {"rates": {"EUR": 0.9}},
                           URLS[1]: {"usd": {"tjs": 10.5}}})
    with_session(session)
    rate = await rates.fetch()
    check("ответ без TJS пропускается", rate.diram == 1050)

    session = FakeSession({URLS[0]: {"rates": {"TJS": "не число"}},
                           URLS[2]: {"usd": {"tjs": 12}}})
    with_session(session)
    rate = await rates.fetch()
    check("нечисловой курс пропускается", rate.diram == 1200, str(rate.diram))
    check("дошли до третьего источника", len(session.asked) == 3)

    # все молчат
    session = FakeSession({})
    with_session(session)
    try:
        await rates.fetch()
        check("молчание источников — ошибка", False)
    except RuntimeError as exc:
        check("молчание источников — ошибка", True)
        check("в ошибке перечислены источники", "open.er-api.com" in str(exc), str(exc))

    # сеть упала
    session = FakeSession({url: OSError("сеть недоступна") for url in URLS})
    with_session(session)
    try:
        await rates.fetch()
        check("обрыв сети не роняет разбор", False)
    except RuntimeError:
        check("обрыв сети не роняет разбор", True)


async def saving(conn) -> None:
    with_session(FakeSession({URLS[0]: {"rates": {"TJS": 10.9}}}))
    await runtime.set_value(conn, "usd_rate_spread", "0")

    rate = await pricing.refresh_rate(conn)
    check("курс сохранён в настройки", runtime.usd_rate() == 1090, str(runtime.usd_rate()))
    check("источник записан", runtime.get("usd_rate_source") == rate.source)
    check("время записано", runtime.get("usd_rate_at").startswith("20"),
          runtime.get("usd_rate_at"))

    await runtime.set_value(conn, "usd_rate_spread", "5")
    await pricing.refresh_rate(conn)
    check("надбавка учтена при сохранении", runtime.usd_rate() == 1144,
          str(runtime.usd_rate()))
    await runtime.set_value(conn, "usd_rate_spread", "0")

    # выключённое автообновление не трогает курс при пересчёте цен
    await runtime.set_value(conn, "usd_rate_diram", "999")
    await runtime.set_value(conn, "usd_auto", "0")
    await pricing.refresh_once(conn, provider=object())
    check("при выключенном авто курс не трогается", runtime.usd_rate() == 999)

    # включённое — обновляет, даже если цены потом не посчитались
    await runtime.set_value(conn, "usd_auto", "1")
    result = await pricing.refresh_once(conn, provider=object())
    check("при включённом авто курс обновился", runtime.usd_rate() == 1090)
    check("отсутствие цен у провайдера не мешает", result["ok"] is False, str(result))

    # источники легли — старый курс остаётся, бот работает
    with_session(FakeSession({}))
    await pricing.refresh_once(conn, provider=object())
    check("при отказе источников курс остаётся прежним", runtime.usd_rate() == 1090)


async def panel_screens(conn) -> None:
    with_session(FakeSession({URLS[0]: {"rates": {"TJS": 12.5}}}))
    await runtime.set_value(conn, "usd_rate_diram", "1000")
    await runtime.set_value(conn, "usd_rate_spread", "3")

    call = call_of("pn:rate")
    await panel.cb_rate(call, conn)
    check("показан источник", "open.er-api.com" in call.last, call.last[:200])
    check("показан биржевой курс", "12.5" in call.last)
    check("показана надбавка", "Надбавка: <b>3%</b>" in call.last)
    check("показан курс в работе", "12.87" in call.last, call.last)
    check("видно изменение", "Было <b>10.00 с.</b>" in call.last, call.last)
    check("курс сохранён", runtime.usd_rate() == 1287, str(runtime.usd_rate()))

    # отказ показывается честно
    with_session(FakeSession({}))
    call = call_of("pn:rate")
    await panel.cb_rate(call, conn)
    check("при отказе так и сказано", "не пришёл" in call.last, call.last[:120])
    check("предлагает задать вручную", "вручную" in call.last)
    check("старый курс уцелел", runtime.usd_rate() == 1287)

    # переключатель
    await runtime.set_value(conn, "usd_auto", "0")
    call = call_of("pn:rate_auto")
    await panel.cb_rate_auto(call, conn)
    check("переключатель включает авто", runtime.get_bool("usd_auto") is True)
    check("в кнопке видно режим",
          any("Курс: авто" in b.text
              for r in panel.prices_kb().inline_keyboard for b in r))
    await panel.cb_rate_auto(call_of("pn:rate_auto"), conn)
    check("и выключает обратно", runtime.get_bool("usd_auto") is False)
    check("в кнопке снова ручной режим",
          any("Курс: вручную" in b.text
              for r in panel.prices_kb().inline_keyboard for b in r))

    check("в разделе цен видно источник курса",
          "open.er-api.com" in panel.prices_text(), panel.rate_line())
    check("и надбавку", "надбавкой 3%" in panel.prices_text())


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await rate_tests()
        await saving(conn)
        await panel_screens(conn)
    finally:
        rates.aiohttp.ClientSession = REAL_SESSION
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
