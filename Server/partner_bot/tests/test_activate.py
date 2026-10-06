"""Активация по трём значениям: токен, ID админа, ключ поставщика."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.exceptions import TelegramAPIError

import setup as wizard
from app import db, runtime, texts
from app.handlers import deposit
from app.services import welcome

PASS, FAIL = [], []

TOKEN = "123456789:" + "A" * 35


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ─────────────────────────────────────────────── setup.py --quick


def wizard_checks() -> None:
    values = wizard.quick_values(TOKEN, "111; 222", "fc_key_12345")
    check("один ключ — на всё, режим fazer",
          values["FRAGMENT_MODE"] == "fazer" and values["FAZER_API_KEY"] == "fc_key_12345")
    check("адрес поставщика ставится сам",
          values["FAZER_BASE_URL"] == "https://api.fzr.cards")
    check("отдельный игровой ключ не пишется", "FAZER_GAMES_KEY" not in values)
    check("несколько админов через ; превращаются в список",
          values["ADMIN_IDS"] == "111,222")
    check("ключ с пробелом отвергается", wizard.check_key("fc key") is not None)
    check("короткий ключ отвергается", wizard.check_key("abc") is not None)
    check("нормальный ключ принимается", wizard.check_key("fc_key_12345") is None)

    real_env, real_token, real_supplier = wizard.ENV, wizard.verify_token, wizard.verify_supplier
    with tempfile.TemporaryDirectory() as tmp:
        wizard.ENV = Path(tmp) / ".env"
        try:
            wizard.verify_token = lambda t: (True, "@test_bot")
            wizard.verify_supplier = lambda k: (True, "ключ принят")
            code = wizard.quick([TOKEN, "111", "fc_key_12345"])
            written = wizard.ENV.read_text(encoding="utf-8") if wizard.ENV.exists() else ""
            check("с тремя значениями .env пишется без вопросов", code == 0 and bool(written))
            check("в .env токен, админ и ключ",
                  f"BOT_TOKEN={TOKEN}" in written and "ADMIN_IDS=111" in written
                  and "FAZER_API_KEY=fc_key_12345" in written
                  and "FRAGMENT_MODE=fazer" in written)

            other = "987654321:" + "B" * 35
            check("чужой токен поверх работающего бота — отказ",
                  wizard.quick([other, "111", "fc_key_12345"]) == 1
                  and f"BOT_TOKEN={TOKEN}" in wizard.ENV.read_text(encoding="utf-8"))
            check("stars-bot activate меняет токен сознательно",
                  wizard.quick([other, "111", "fc_key_12345"], replace=True) == 0
                  and f"BOT_TOKEN={other}" in wizard.ENV.read_text(encoding="utf-8"))
            check("тот же токен повторно — можно (смена ключа или ID)",
                  wizard.quick([other, "333", "fc_key_67890"]) == 0
                  and "FAZER_API_KEY=fc_key_67890" in wizard.ENV.read_text(encoding="utf-8"))
            written = wizard.ENV.read_text(encoding="utf-8")
            check("заготовки владельца в новый бот не попадают",
                  "PAY_CARD_NUMBER=\n" in written and "SUPPORT_USERNAME=\n" in written
                  and "ALIJON" not in written)

            wizard.ENV.unlink()
            check("кривой токен — отказ, файл не создан",
                  wizard.quick(["не-токен", "111", "fc_key_12345"]) == 1
                  and not wizard.ENV.exists())
            check("ID с буквами — отказ",
                  wizard.quick([TOKEN, "abc", "fc_key_12345"]) == 1)
            check("два значения вместо трёх — отказ",
                  wizard.quick([TOKEN, "111"]) == 1)

            wizard.verify_token = lambda t: (False, "Telegram не принял токен")
            check("Telegram не принял токен — отказ, файл не создан",
                  wizard.quick([TOKEN, "111", "fc_key_12345"]) == 1
                  and not wizard.ENV.exists())

            wizard.verify_token = lambda t: (True, "@test_bot")
            wizard.verify_supplier = lambda k: (False, "Поставщик не принял ключ")
            check("поставщик не принял ключ — отказ",
                  wizard.quick([TOKEN, "111", "fc_key_12345"]) == 1
                  and not wizard.ENV.exists())
        finally:
            wizard.ENV, wizard.verify_token, wizard.verify_supplier = (
                real_env, real_token, real_supplier)


# ─────────────────────────────────────────────── бот без реквизитов


class Alert:
    def __init__(self):
        self.answers: list[tuple[str, bool]] = []
        self.edited = []
        self.from_user = type("U", (), {"id": 555})()
        outer = self

        class Msg:
            chat = type("C", (), {"id": 555})()
            message_id = 1

            async def edit_text(self, text, **kw):
                outer.edited.append(text)

        self.message = Msg()

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class State:
    def __init__(self):
        self.state = None

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.state = None

    async def update_data(self, **kw):
        pass


class Bot:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise TelegramAPIError(method=None, message="chat not found")
        self.sent.append((chat_id, text))


async def bot_checks(conn) -> None:
    from app.main import readiness

    await runtime.set_value(conn, "pay_card_number", "")
    blockers, warnings = readiness()
    check("без реквизитов бот всё равно запускается", not blockers, str(blockers))
    check("а владельца предупреждают", any("Реквизиты" in w for w in warnings))

    call, state = Alert(), State()
    await deposit.cb_card(call, state, conn)
    check("клиенту без реквизитов — «скоро», а не пустой номер",
          call.answers == [(texts.DEPOSIT_SOON, True)] and state.state is None
          and not call.edited)

    left = welcome.todo()
    check("в списке дел — реквизиты", any("Реквизиты" in item for item in left))

    failing = Bot(fail=True)
    sent = await welcome.greet_once(failing, conn, "test_bot", [111])
    check("админ не нажал /start — флаг не ставим, напишем потом",
          sent == 0 and not runtime.get(welcome.FLAG))

    good = Bot()
    sent = await welcome.greet_once(good, conn, "test_bot", [111, 222])
    check("первый запуск — пишем всем админам", sent == 2 and len(good.sent) == 2)
    body = good.sent[0][1] if good.sent else ""
    check("в письме — что бот работает и что задать",
          "@test_bot активирован" in body and "Реквизиты" in body)

    again = Bot()
    check("второй запуск — не спамим",
          await welcome.greet_once(again, conn, "test_bot", [111]) == 0 and not again.sent)

    await runtime.set_value(conn, "pay_card_number", "0000 1111 2222 3333")
    call, state = Alert(), State()
    await deposit.cb_card(call, state, conn)
    check("с реквизитами пополнение открывается", state.state is not None and call.edited)
    check("с реквизитами в списке дел их нет",
          not any("Реквизиты" in item for item in welcome.todo()))
    await runtime.reset(conn, welcome.FLAG)


async def main() -> None:
    wizard_checks()
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await bot_checks(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
