"""Deep Links: создание, статистика, удаление и определение источника."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.types import Chat, Message, Update, User

from app import db, links, runtime
from app.handlers import panel

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Me:
    username = "moi_stars_bot"


class FakeBot:
    """Считает обращения к Telegram: имя бота должно спрашиваться один раз."""

    def __init__(self):
        self.calls = 0

    async def me(self):
        self.calls += 1
        return Me()


class FakeState:
    def __init__(self):
        self.data, self.state = {}, None

    async def set_state(self, v):
        self.state = getattr(v, "state", v)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data.clear()
        self.state = None


class FakeMessage:
    def __init__(self, text=None, bot=None):
        self.text, self.bot = text, bot
        self.last, self.markup = "", None

    async def answer(self, text, reply_markup=None, **kw):
        self.last, self.markup = text, reply_markup

    async def edit_text(self, text, reply_markup=None, **kw):
        self.last, self.markup = text, reply_markup


class FakeCallback:
    def __init__(self, data, bot=None):
        self.data = data
        self.bot = bot or FakeBot()
        self.message = FakeMessage(bot=self.bot)
        self.alerts = []

    async def answer(self, text="", show_alert=False):
        self.alerts.append(text)

    @property
    def last(self):
        return self.message.last

    @property
    def markup(self):
        return self.message.markup


def texts_of(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def copy_of(markup) -> str | None:
    for row in markup.inline_keyboard:
        for b in row:
            if b.copy_text:
                return b.copy_text.text
    return None


async def buy(conn, user_id: int, price: int, status: str = db.ORDER_DELIVERED) -> None:
    order = await db.create_order(
        conn, user_id=user_id, product_type="stars", quantity=50,
        recipient="kto", price=price,
    )
    await db.update_order(conn, order.id, status=status)


# ───────────────────────────────────────────────────────────── имена ссылок


def names() -> None:
    check("код читается из /start", links.payload_of("/start instagram") == "instagram")
    check("код читается вместе с именем бота",
          links.payload_of("/start@moi_bot reklama1") == "reklama1")
    check("голый /start кода не даёт", links.payload_of("/start") == "")
    check("обычный текст кодом не считается", links.payload_of("привет") == "")
    check("похожая команда не в счёт", links.payload_of("/starter abc") == "")
    check("пустое сообщение не роняет", links.payload_of(None) == "")

    check("instagram подходит", links.check("instagram") is None)
    check("partner_1 подходит", links.check("partner_1") is None)
    check("reklama-2 подходит", links.check("reklama-2") is None)
    check("пустое имя отклоняется", links.check("") is not None)
    check("русские буквы отклоняются", "латин" in (links.check("реклама") or ""))
    check("пробел отклоняется", links.check("moya reklama") is not None)
    check("длиннее 64 символов отклоняется", links.check("a" * 65) is not None)
    check("ref123 отклоняется как реферальный",
          "реферальн" in (links.check("ref123") or ""))
    check("refresh не путается с рефералкой", links.check("refresh") is None)

    check("ссылка собирается",
          links.build("moi_bot", "instagram") == "https://t.me/moi_bot?start=instagram")


# ─────────────────────────────────────────────────────────────── база


async def storage(conn) -> None:
    link = await db.create_link(conn, "instagram")
    check("ссылка создаётся", link is not None and link.code == "instagram")
    check("дубль не создаётся", await db.create_link(conn, "instagram") is None)

    await db.upsert_user(conn, 10, "pervyi", "Первый")
    await db.record_link_hit(conn, "instagram", 10, True)

    check("незарегистрированный код игнорируется",
          await db.record_link_hit(conn, "chuzhoy", 10, True) is False)
    check("чужой код не создал ссылку", await db.get_link_by_code(conn, "chuzhoy") is None)

    # тот же человек вернулся по той же ссылке
    await db.record_link_hit(conn, "instagram", 10, False)

    await db.upsert_user(conn, 11, "vtoroy", "Второй")
    await db.record_link_hit(conn, "instagram", 11, True)

    stats = await db.link_stats(conn, link.id)
    check("переходы считают все запуски", stats["hits"] == 3, str(stats))
    check("уникальных считает людей", stats["people"] == 2, str(stats))
    check("новых считает только первых", stats["fresh"] == 2, str(stats))

    check("источник записан клиенту", (await db.get_user(conn, 10)).source == "instagram")

    # вторая ссылка не должна воровать уже размеченного клиента
    await db.create_link(conn, "telegram_kanal")
    await db.record_link_hit(conn, "telegram_kanal", 10, False)
    check("первый источник не перезаписывается",
          (await db.get_user(conn, 10)).source == "instagram")
    second = await db.get_link_by_code(conn, "telegram_kanal")
    check("но переход второй ссылке засчитан",
          (await db.link_stats(conn, second.id))["hits"] == 1)

    # выручка по источнику
    await buy(conn, 10, 40_00)
    await buy(conn, 10, 10_00)
    await buy(conn, 11, 25_00, db.ORDER_REFUNDED)
    stats = await db.link_stats(conn, link.id)
    check("покупатели считаются по источнику", stats["buyers"] == 1, str(stats))
    check("выручка считается по источнику", stats["revenue"] == 5000, str(stats))
    check("возврат в выручку не идёт", stats["revenue"] == 5000)
    check("вторая ссылка выручки не приписала",
          (await db.link_stats(conn, second.id))["revenue"] == 0)

    rows = await db.list_links(conn)
    check("в списке обе ссылки", len(rows) == 2)
    check("свежая ссылка сверху", rows[0][0].code == "telegram_kanal")

    check("удаление срабатывает", await db.delete_link(conn, second.id) is True)
    check("удалённой ссылки нет", await db.get_link(conn, second.id) is None)
    check("её переходы стёрты",
          (await db.link_stats(conn, second.id))["hits"] == 0)
    check("клиент и его метка на месте",
          (await db.get_user(conn, 10)).source == "instagram")
    check("повторное удаление ничего не ломает",
          await db.delete_link(conn, second.id) is False)


# ─────────────────────────────────────────────────────────────── панель


async def panel_screens(conn) -> None:
    bot = FakeBot()
    state = FakeState()

    call = FakeCallback("pn:links", bot)
    await panel.cb_links(call, state, conn)
    check("раздел открывается", "Рекламные ссылки" in call.last)
    check("в списке видно ссылку", "instagram" in call.last, call.last[:120])
    check("есть кнопка создания", "➕ Создать ссылку" in texts_of(call.markup))
    check("ссылка есть кнопкой", any("instagram" in t for t in texts_of(call.markup)))

    call = FakeCallback("pn:link:new", bot)
    await panel.cb_link_new(call, state)
    check("экран создания просит название", "Пришлите название" in call.last)
    check("шаг ожидания включён", state.state == "Panel:link")

    # неподходящее имя — шаг не закрывается
    message = FakeMessage("моя реклама", bot)
    await panel.on_link_name(message, state, conn)
    check("русское название отклонено", "❌" in message.last)
    check("после отказа всё ещё ждём название", state.state == "Panel:link")

    message = FakeMessage("instagram", bot)
    await panel.on_link_name(message, state, conn)
    check("повтор названия отклонён", "уже есть" in message.last)

    message = FakeMessage("reklama1", bot)
    await panel.on_link_name(message, state, conn)
    check("ссылка создана", "Ссылка готова" in message.last, message.last[:80])
    check("в ответе полный адрес",
          "https://t.me/moi_stars_bot?start=reklama1" in message.last)
    check("кнопка копирования отдаёт адрес",
          copy_of(message.markup) == "https://t.me/moi_stars_bot?start=reklama1")
    check("шаг закрыт", state.state is None)
    check("имя бота спросили один раз", bot.calls == 1, f"вызовов: {bot.calls}")

    link = await db.get_link_by_code(conn, "instagram")
    call = FakeCallback(f"pn:link:{link.id}", bot)
    await panel.cb_link_card(call, conn)
    for field in ("Всего запусков", "Уникальных людей", "Новых пользователей",
                  "Покупателей", "Создана"):
        check(f"в карточке есть «{field}»", field in call.last)
    check("в карточке верные переходы", "<b>3</b>" in call.last, call.last[:200])
    check("в карточке видна выручка", "50.00" in call.last)
    check("в карточке есть конверсия", "купили: <b>50%</b>" in call.last, call.last[-400:])
    check("дата создания — сегодня",
          datetime.now(timezone.utc).strftime("%Y-%m-%d") in call.last)
    check("есть кнопка копирования", "📋 Копировать ссылку" in texts_of(call.markup))
    check("есть кнопка удаления", "🗑 Удалить" in texts_of(call.markup))

    # удаление в два шага
    call = FakeCallback(f"pn:link:del:{link.id}", bot)
    await panel.cb_link_delete(call, conn)
    check("удаление сначала спрашивает", "Удалить ссылку" in call.last)
    check("предупреждает о потере статистики", "3</b> переходов" in call.last)
    check("ссылка ещё на месте", await db.get_link(conn, link.id) is not None)
    check("есть кнопка отмены", any("Отмена" in t for t in texts_of(call.markup)))

    call = FakeCallback(f"pn:link:kill:{link.id}", bot)
    await panel.cb_link_kill(call, conn)
    check("после подтверждения ссылка удалена",
          await db.get_link(conn, link.id) is None)
    check("вернулись к списку", "Рекламные ссылки" in call.last)

    call = FakeCallback(f"pn:link:{link.id}", bot)
    await panel.cb_link_card(call, conn)
    check("карточка удалённой ссылки не падает", "Рекламные ссылки" in call.last)
    check("и говорит об этом", any("удален" in a.lower() for a in call.alerts),
          str(call.alerts))

    check("раздел есть в главном меню панели",
          "🔗 Рекламные ссылки" in texts_of(panel.home_kb()))


# ────────────────────────────────────────── живой /start через диспетчер


async def live(conn) -> None:
    from aiogram import Bot, Dispatcher
    from app.handlers import menu
    from app.middlewares.escape import CommandEscapeMiddleware
    from app.middlewares.guard import UserGuardMiddleware

    dp = Dispatcher(conn=conn)
    dp.message.outer_middleware(CommandEscapeMiddleware())
    dp.message.middleware(UserGuardMiddleware())

    seen = []

    @dp.message()
    async def any_message(message, conn):
        seen.append(message.text)

    bot = Bot(token="123456:TEST")
    await db.create_link(conn, "instagram")
    await db.create_link(conn, "reklama1")

    async def start(uid: int, payload: str = "", mid: int = 1) -> None:
        user = User(id=uid, is_bot=False, first_name="Гость", username=f"gost{uid}")
        chat = Chat(id=uid, type="private")
        message = Message.model_construct(
            message_id=mid, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=chat, from_user=user, text=f"/start {payload}".strip())
        await dp.feed_update(bot, Update.model_construct(update_id=mid, message=message))

    await start(500, "instagram", 1)
    check("новый клиент размечен источником",
          (await db.get_user(conn, 500)).source == "instagram")
    check("сообщение дошло до обработчика", seen == ["/start instagram"], str(seen))

    await start(500, "reklama1", 2)
    check("вернувшийся клиент не переразмечен",
          (await db.get_user(conn, 500)).source == "instagram")

    insta = await db.get_link_by_code(conn, "instagram")
    stats = await db.link_stats(conn, insta.id)
    check("живой переход посчитан", stats["hits"] == 1, str(stats))
    check("живой новый пользователь посчитан", stats["fresh"] == 1, str(stats))

    rek = await db.get_link_by_code(conn, "reklama1")
    check("второй ссылке засчитан переход, но не новый",
          (await db.link_stats(conn, rek.id)) == {
              "hits": 1, "people": 1, "fresh": 0, "buyers": 0, "revenue": 0})

    # обычный /start без кода
    await start(501, "", 3)
    check("без кода источника нет", (await db.get_user(conn, 501)).source is None)

    # реферальная ссылка работает как раньше
    await start(502, "ref500", 4)
    user = await db.get_user(conn, 502)
    check("реферал записан по-старому", user.referrer_id == 500)
    check("реферал не попал в источники", user.source is None)
    check("реферальный код не создал ссылку",
          await db.get_link_by_code(conn, "ref500") is None)

    # незнакомый код просто игнорируется
    await start(503, "levaya_ssylka", 5)
    check("незнакомый код не размечает", (await db.get_user(conn, 503)).source is None)
    check("незнакомый код не создал ссылку",
          await db.get_link_by_code(conn, "levaya_ssylka") is None)
    check("клиент всё равно зарегистрирован", await db.get_user(conn, 503) is not None)

    await bot.session.close()


async def main() -> None:
    names()
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await storage(conn)
        await panel_screens(conn)
        await live(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
