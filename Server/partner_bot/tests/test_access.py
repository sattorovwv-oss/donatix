"""Доступ к админ-панели: владельцы из .env и админы из панели."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime
from app.handlers import panel
from app.services import access

OWNER = 111          # он же админ из .env в тестовом окружении
HELPER = 555_666_777
STRANGER = 999_000_111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid, username=None):
        self.id, self.username, self.first_name = uid, username, "Кто-то"


class FakeMessage:
    def __init__(self, text=None, uid=OWNER):
        self.text = text
        self.from_user = FakeUser(uid)
        self.replies: list[str] = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kw):
        return await self.answer(text, **kw)

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""


class FakeCallback:
    def __init__(self, data, uid=OWNER, bot=None):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage(uid=uid)
        self.bot = bot or FakeBot()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)

    @property
    def last(self):
        return self.message.last

    @property
    def markup(self):
        return getattr(self.message, "_markup", None)


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


class FakeBot:
    def __init__(self, broken=False):
        self.sent: list[tuple[int, str]] = []
        self.menus: list[tuple[int, int]] = []     # (кому, сколько команд)
        self.broken = broken

    async def set_my_commands(self, commands, scope=None, **kw):
        self.menus.append((getattr(scope, "chat_id", 0), len(commands)))

    async def send_message(self, chat_id, text, **kw):
        if self.broken:
            from aiogram.exceptions import TelegramAPIError

            raise TelegramAPIError(method=None, message="bot was blocked")
        self.sent.append((chat_id, text))


async def same_rights(conn) -> None:
    """Добавленный админ получает то же, что и владелец.

    Раньше рассылки брали список из .env, и админ из панели открывал
    её, но не получал ни заявок, ни чеков, ни тревог: админ на словах.
    """
    import re

    from app.services.delivery import notify_admins

    await runtime.set_value(conn, "extra_admins", str(HELPER))
    bot = FakeBot()

    # Команда /panel должна появиться у него в меню сразу, а не после
    # ближайшего перезапуска бота.
    await access.apply_menu(bot, HELPER)
    await access.apply_menu(bot, STRANGER)
    check("админу в меню кладут команду больше",
          bot.menus and bot.menus[0][1] > bot.menus[1][1], str(bot.menus))

    await notify_admins(bot, "🔔 Заявка на пополнение")
    got = {chat_id for chat_id, _ in bot.sent}
    check("уведомление дошло владельцу", OWNER in got, str(got))
    check("и добавленному админу тоже", HELPER in got, str(got))

    await runtime.set_value(conn, "extra_admins", "")
    bot = FakeBot()
    await notify_admins(bot, "🔔 Ещё одна")
    check("после снятия доступа не пишем",
          HELPER not in {chat_id for chat_id, _ in bot.sent}, str(bot.sent))

    # Чтобы это не отросло заново: ни одна рассылка не должна брать
    # список получателей из .env в обход общего списка админов.
    sites = ("app/services/delivery.py", "app/services/reviews.py",
             "app/services/billing.py", "app/services/pricing.py",
             "app/handlers/deposit.py", "app/main.py")
    # Ищем именно использование списка как получателей: перебор или
    # копию. Упоминания в пояснениях и проверка «задан ли он вообще»
    # тут ни при чём.
    used = re.compile(r"(?:for\s+\w+\s+in|list\()\s*settings\.admin_ids")
    guilty = []
    for path in sites:
        for line in io.open(path, encoding="utf-8").read().split("\n"):
            if used.search(line):
                guilty.append(f"{path}: {line.strip()}")
    check("рассылки не берут список из .env", not guilty, "; ".join(guilty))


async def run(conn) -> None:
    await runtime.set_value(conn, "extra_admins", "")

    # ---------------------------------------------- кто есть кто
    check("владелец из .env — владелец", access.is_owner(OWNER))
    check("и он же админ", access.is_admin(OWNER))
    check("посторонний не админ", not access.is_admin(HELPER))
    check("список пока только из владельцев",
          access.admins() == access.owners(), str(access.admins()))

    # ---------------------------------------------- выдача доступа
    state = FakeState()
    bot = FakeBot()
    call = FakeCallback("pn:admin_add")
    await panel.cb_admin_add(call, state)
    check("бот просит ID", "Пришлите" in call.last, call.last[:60])
    check("предупреждает про полный доступ",
          "доверяете деньги" in call.last, call.last)

    message = FakeMessage("@vasya")
    await panel.on_admin_add(message, state, conn, bot)
    check("по юзернейму доступ не даётся",
          "числовой ID" in message.last, message.last[:60])
    check("и объяснено почему", "можно сменить" in message.last, message.last)

    message = FakeMessage(str(HELPER))
    await panel.on_admin_add(message, state, conn, bot)
    check("доступ выдан", access.is_admin(HELPER))
    check("владельцем он при этом не стал", not access.is_owner(HELPER))
    check("владельцу сказано", "Доступ выдан" in message.last, message.last[:60])
    check("новому админу написали", bot.sent and bot.sent[0][0] == HELPER,
          str(bot.sent))
    check("в письме сказано, как открыть",
          bot.sent and "/panel" in bot.sent[0][1], str(bot.sent[:1]))

    message = FakeMessage(str(HELPER))
    await panel.on_admin_add(message, state, conn, FakeBot())
    check("повторная выдача ничего не ломает",
          "уже есть" in message.last, message.last[:60])
    check("и в списке он один раз",
          access.extra().count(HELPER) == 1, str(access.extra()))

    # ---------------------------------------------- права админа
    check("админ проходит в панель", access.is_admin(HELPER))
    call = FakeCallback("pn:admin_add", uid=HELPER)
    await panel.cb_admin_add(call, FakeState())
    check("но раздавать доступ не может",
          any("только владелец" in a for a in call.alerts), str(call.alerts))

    message = FakeMessage(str(STRANGER), uid=HELPER)
    await panel.on_admin_add(message, FakeState(), conn, FakeBot())
    check("и через сообщение тоже не может",
          not access.is_admin(STRANGER), str(access.extra()))

    call = FakeCallback(f"pn:admin_del:{OWNER}", uid=HELPER)
    await panel.cb_admin_del(call, FakeState(), conn)
    check("и не может снять владельца",
          access.is_owner(OWNER) and any("владелец" in a for a in call.alerts),
          str(call.alerts))

    # ---------------------------------------------- владельца не снять
    ok = await access.revoke(conn, OWNER)
    check("владельца не снять даже напрямую", ok is False and access.is_admin(OWNER))

    call = FakeCallback(f"pn:admin_del:{OWNER}")
    await panel.cb_admin_del(call, FakeState(), conn)
    check("владелец не снимает и сам себя", access.is_owner(OWNER))
    check("и ему это объяснили",
          any("нельзя" in a for a in call.alerts), str(call.alerts))

    # ---------------------------------------------- снятие админа
    call = FakeCallback(f"pn:admin_del:{HELPER}")
    await panel.cb_admin_del(call, FakeState(), conn)
    check("админа снять можно", not access.is_admin(HELPER))
    check("список опустел", access.extra() == [], str(access.extra()))

    ok = await access.revoke(conn, STRANGER)
    check("снятие того, у кого доступа нет, ничего не делает", ok is False)

    # ---------------------------------------------- экран списка
    await access.grant(conn, HELPER)
    await db.upsert_user(conn, HELPER, "pomoshnik", "Помощник")
    call = FakeCallback("pn:admins")
    await panel.cb_admins(call, FakeState(), conn)
    check("владелец помечен короной", "👑" in call.last, call.last[:200])
    check("админ виден в списке", str(HELPER) in call.last, call.last[:300])
    check("виден его юзернейм", "pomoshnik" in call.last, call.last[:300])
    check("объяснено, чем владелец отличается",
          "нельзя снять отсюда" in call.last, call.last)

    call = FakeCallback("pn:admins", uid=HELPER)
    await panel.cb_admins(call, FakeState(), conn)
    check("админу сказано, что список меняет владелец",
          "только владелец" in call.last, call.last[-250:])

    # ---------------------------------------------- мелочи разбора
    await runtime.set_value(conn, "extra_admins", "5; 7 ,, abc,5")
    check("мусор в настройке не ломает список",
          access.extra() == [5, 7], str(access.extra()))
    await runtime.set_value(conn, "extra_admins", "")

    # ---------------------------------------------- не дошло письмо
    bot = FakeBot(broken=True)
    message = FakeMessage(str(STRANGER))
    await panel.on_admin_add(message, FakeState(), conn, bot)
    check("доступ выдан, даже если письмо не дошло",
          access.is_admin(STRANGER))
    check("и владельцу об этом сказано",
          "не открывал бота" in message.last, message.last[-120:])
    await access.revoke(conn, STRANGER)


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
        await same_rights(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
