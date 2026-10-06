"""Открытая заявка: «Чек фиристодан» и «‹ Реквизитҳо» работают и после очистки состояния."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401,E402

from app import db, runtime  # noqa: E402
from app.handlers import deposit as dep  # noqa: E402
from app.services import paymethods as pm  # noqa: E402

PASS, FAIL = [], []
UID = 70501


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class User:
    id, username, first_name = UID, "buyer", "Покупатель"


class Msg:
    def __init__(self):
        self.texts, self.markups = [], []
        self.chat, self.message_id = type("C", (), {"id": UID})(), 5

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return self


class Call:
    def __init__(self, data):
        self.data, self.from_user, self.message, self.alerts = data, User(), Msg(), []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)


class State:
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


async def main() -> None:
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await conn.execute("DELETE FROM deposits WHERE user_id = ?", (UID,))
    await conn.commit()
    await runtime.set_value(conn, pm.KEY, "")
    await pm.add(conn, "Душанбе Сити", "5058 2700 1234 5678", "Алиджон М.")
    alif = await pm.add(conn, "Алиф", "+992900123456", "Алиджон М.")
    d = await db.create_deposit(conn, user_id=UID, amount=1000, method="Перевод · Алиф",
                                receipt_file_id="", reference="TOP1234")

    # Экран «У вас уже есть заявка» очищает состояние — как после перезапуска бота
    state = State()
    call = Call("dep:paid")
    await dep.cb_paid(call, state, conn)
    shown = (call.message.texts or [""])[-1]
    check("«Чек фиристодан» с экрана открытой заявки работает",
          "скриншоти чекро" in shown and not call.alerts, shown[:200])
    check("состояние — ждём чек", state.state == "Deposit:receipt", str(state.state))
    check("заявка та же", state.data.get("deposit_id") == d.id)
    check("банк заявки узнан", state.data.get("pm") == alif["id"], str(state.data))

    call = Call("dep:back")
    await dep.cb_back_to_requisites(call, state, conn)
    body = call.message.texts[-1]
    check("«‹ Реквизитҳо» — реквизиты выбранного банка", "+992900123456" in body and "Алиф" in body, body[:300])
    check("а не первого в списке", "5058 2700" not in body)

    # Без чека владелец зачислить не может — заявка остаётся ждать
    from app.handlers.admin import NO_RECEIPT, _resolve_deposit

    class Bot:
        async def send_message(self, *a, **kw):
            return None
    report = await _resolve_deposit(conn, Bot(), d.id, 111, approved=True)
    check("без чека не зачисляется", report == NO_RECEIPT.format(deposit_id=d.id), report)
    check("заявка всё ещё ждёт", (await db.get_deposit(conn, d.id)).status == db.DEP_PENDING)

    await db.cancel_deposit(conn, d.id, UID)
    state = State()
    call = Call("dep:paid")
    await dep.cb_paid(call, state, conn)
    check("заявки нет — понятное сообщение", call.alerts and "аз нав сар кунед" in call.alerts[-1], str(call.alerts))
    await conn.close()


asyncio.run(main())
print("\n" + "=" * 52)
print(f"Пройдено: {len(PASS)}   Провалено: {len(FAIL)}")
if FAIL:
    print("ПРОВАЛЫ: " + ", ".join(FAIL))
    sys.exit(1)
