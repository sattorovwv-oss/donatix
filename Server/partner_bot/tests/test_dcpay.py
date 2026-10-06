"""Кнопка оплаты «Душанбе Сити»: сборка ссылки и код платежа."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, keyboards, runtime, texts
from app.handlers import deposit as dep_h
from app.handlers import panel
from app.money import fmt
from app.services import dcpay

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=555, username="buyer", first_name="Покупатель"):
        self.id, self.username, self.first_name = uid, username, first_name


class FakeMessage:
    def __init__(self, text=None, user=None, photo=False):
        self.text = text
        self.from_user = user or FakeUser()
        self.photo = [type("P", (), {"file_id": "f1"})()] if photo else None
        self.document = None
        self.chat = type("C", (), {"id": self.from_user.id})()
        self.message_id = 1
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))
        return self

    async def edit_text(self, text, **kw):
        return await self.answer(text, **kw)

    async def copy_to(self, chat_id, **kw):
        return None

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""

    @property
    def last_markup(self):
        return self.markups[-1] if self.markups else None


class FakeCallback:
    def __init__(self, data, user=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.message = FakeMessage(user=self.from_user)
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)

    @property
    def last(self):
        return self.message.last


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_state(self, value):
        self.state = getattr(value, "state", value)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data.clear()
        self.state = None


class FakeBot:
    async def send_message(self, *a, **kw):
        return None


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    try:
        await db.init(conn)
        await runtime.load(conn)
        await run(conn)
    finally:
        await conn.close()
    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


async def run(conn) -> None:
    # ------------------------------------------------ ссылка собирается верно
    sample = "https://pay.dc.tj/?a=9999000011112222&c=%40uwayscoder%20TOP2248&f1=133&s=50"
    built = dcpay.build_link("9999000011112222", 5000, "@uwayscoder TOP2248")
    check("ссылка совпадает с образцом", built == sample, built)

    check("пробелы в номере счёта не мешают",
          dcpay.build_link("9999 0000 1111 2222", 5000, "x") ==
          dcpay.build_link("9999000011112222", 5000, "x"))

    q = parse_qs(urlparse(built).query)
    check("счёт попадает в параметр a", q["a"] == ["9999000011112222"])
    check("сумма попадает в параметр s", q["s"] == ["50"])
    check("код услуги по умолчанию 133", q["f1"] == ["133"])

    for diram, expected in ((5000, "50"), (5050, "50.50"), (12345, "123.45"),
                            (150000, "1500"), (0, "0")):
        check(f"сумма {diram} дирам -> {expected}",
              dcpay.amount_text(diram) == expected, dcpay.amount_text(diram))

    link = dcpay.build_link("9999000011112222", 5050, "@shop TOP1234", "200")
    q = parse_qs(urlparse(link).query)
    check("копейки передаются", q["s"] == ["50.50"], str(q["s"]))
    check("свой код услуги подставляется", q["f1"] == ["200"])
    check("комментарий кодируется", q["c"] == ["@shop TOP1234"], str(q["c"]))

    ref = dcpay.make_reference()
    check("код платежа короткий и опознаваемый",
          ref.startswith("TOP") and len(ref) == 7 and ref[3:].isdigit(), ref)
    check("коды платежей разные",
          len({dcpay.make_reference() for _ in range(20)}) > 10)

    check("короткий номер не считается счётом", not dcpay.is_ready("123"))
    check("нормальный счёт принимается", dcpay.is_ready("9999000011112222"))

    # ------------------------------------------ кнопка появляется в пополнении
    await db.upsert_user(conn, 555, "buyer", "Покупатель")
    await runtime.set_value(conn, "pay_card_number", "8888 7777 6666 5555")
    await runtime.set_value(conn, "dc_account", "9999000011112222")
    await runtime.set_value(conn, "dc_comment", "@uwayscoder")

    state = FakeState()
    await state.set_state("Deposit:amount")
    msg = FakeMessage("120")
    await dep_h.on_amount(msg, state, conn)

    # Юзербота нет — оплату проверяет владелец по чеку, сумма ровная.
    shown = state.data["amount"]
    check("клиент платит ровно столько, сколько просил",
          shown == 12000, str(shown))
    check("сумма принята и показаны реквизиты",
          fmt(shown) in msg.last, msg.last[:200])
    labels = [b.text for row in msg.markups[-1].inline_keyboard for b in row]
    check("предложена быстрая оплата",
          any("Душанбе Сити" in b for b in labels), str(labels))
    check("и подтверждение оплаты рядом",
          any("пардохт кардам" in b for b in labels), str(labels))
    check("код платежа показан клиенту",
          state.data.get("reference", "") in msg.last, str(state.data))

    buttons = [b for row in msg.last_markup.inline_keyboard for b in row]
    pay = next((b for b in buttons if b.url), None)
    check("кнопка с ссылкой есть", pay is not None)
    check("кнопка зелёная", pay.style == "success", str(pay.style))

    q = parse_qs(urlparse(pay.url).query)
    check("в ссылке счёт из настроек", q["a"] == ["9999000011112222"], str(q["a"]))
    check("в ссылке та же сумма, что на экране",
          q["s"] == [dcpay.amount_text(shown)], str(q["s"]))
    check("в комментарии подпись и код платежа",
          q["c"][0].startswith("@uwayscoder TOP"), str(q["c"]))

    # -------------------------------- код платежа сохраняется в заявке
    receipt = FakeMessage(photo=True)
    await dep_h.on_receipt(receipt, state, conn, FakeBot())
    deposits = await db.list_deposits(conn, status=db.DEP_PENDING)
    check("заявка создана", len(deposits) == 1)
    check("код платежа сохранён в заявке",
          deposits[0].reference and deposits[0].reference.startswith("TOP"),
          str(deposits[0].reference))
    check("сумма заявки совпадает с показанной",
          deposits[0].amount == shown, str(deposits[0].amount))

    # -------------- счёт берётся из реквизитов, если отдельно не задан
    await runtime.set_value(conn, "dc_account", "")
    check("без отдельной настройки счёт берётся из карты",
          dcpay.account() == "8888777766665555", dcpay.account())
    check("кнопка работает без отдельной настройки", dcpay.is_ready())

    state = FakeState()
    msg = FakeMessage("120")
    await dep_h.on_amount(msg, state, conn)
    pay = next((b for row in msg.last_markup.inline_keyboard for b in row if b.url), None)
    check("кнопка появляется сразу после заполнения реквизитов", pay is not None)
    check("в ссылке карта из реквизитов",
          "a=8888777766665555" in (pay.url if pay else ""), pay.url if pay else "—")

    await runtime.set_value(conn, "dc_account", "9999000011112222")
    check("отдельный счёт перекрывает карту",
          dcpay.account() == "9999000011112222")

    # ---------------------------- без реквизитов вовсе кнопки нет
    await runtime.set_value(conn, "dc_account", "")
    await runtime.set_value(conn, "pay_card_number", "")
    state = FakeState()
    msg = FakeMessage("120")
    await dep_h.on_amount(msg, state, conn)
    check("без счёта быстрой оплаты не предлагаем",
          "Душанбе Сити" not in msg.last)
    check("реквизиты показываются даже без кнопки",
          "реквизиты не заданы" in msg.last or "8888" in msg.last)
    check("кнопки со ссылкой нет",
          not any(b.url for row in msg.last_markup.inline_keyboard for b in row))

    # ------------------------------------------------ проверка в панели
    await runtime.set_value(conn, "pay_card_number", "8888 7777 6666 5555")
    await runtime.set_value(conn, "dc_account", "9999000011112222")
    call = FakeCallback("pn:dctest")
    await panel.cb_dc_test(call)
    check("панель показывает пробную ссылку", "pay.dc.tj" in call.last)
    check("проба на 50 сомони", "s=50" in call.last, call.last[-90:])

    await runtime.set_value(conn, "dc_account", "")
    await runtime.set_value(conn, "pay_card_number", "")
    call = FakeCallback("pn:dctest")
    await panel.cb_dc_test(call)
    check("без реквизитов панель объясняет, что вписать",
          "параметр" in call.last or "счёт" in call.last.lower())
    await runtime.set_value(conn, "pay_card_number", "8888 7777 6666 5555")

    check("экран реквизитов показывает состояние кнопки",
          "Душанбе Сити" in panel.pay_text())


asyncio.run(main())
