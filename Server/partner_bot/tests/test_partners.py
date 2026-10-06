"""Партнёры: доли прибыли, взносы, выплаты и что у кого на руках."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import PrivateAttr

from app import db, runtime
from app.handlers import panel
from app.money import fmt

ADMIN = 111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class SpyMessage(Message):
    _log: list = PrivateAttr(default_factory=list)

    async def answer(self, text, reply_markup=None, **kw):
        self._log.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self._log.append((text, reply_markup))
        return self

    @property
    def last(self) -> str:
        return self._log[-1][0] if self._log else ""

    @property
    def markup(self):
        return self._log[-1][1] if self._log else None


class SpyCallback(CallbackQuery):
    _alerts: list = PrivateAttr(default_factory=list)

    async def answer(self, text="", **kw):
        if text:
            self._alerts.append(text)

    @property
    def last(self) -> str:
        return self.message.last

    @property
    def markup(self):
        return self.message.markup

    @property
    def alerts(self) -> list:
        return self._alerts


def msg(text=None) -> SpyMessage:
    user = User(id=ADMIN, is_bot=False, first_name="Админ", username="admin")
    return SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=ADMIN, type="private"), from_user=user, text=text,
    )


def call_of(data: str) -> SpyCallback:
    user = User(id=ADMIN, is_bot=False, first_name="Админ", username="admin")
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=msg(),
    )


def buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def sale(conn, price: int, cost: int, product="stars", user_id: int = 900) -> None:
    order = await db.create_order(
        conn, user_id=user_id, product_type=product, quantity=100,
        recipient="kto", price=price, cost=cost,
    )
    await db.update_order(conn, order.id, status=db.ORDER_DELIVERED)


async def run(conn) -> None:
    storage = MemoryStorage()
    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    await db.upsert_user(conn, 900, "klient", "Клиент")

    # ------------------------------------------------ пустой раздел
    call = call_of("pn:partners")
    await panel.cb_partners(call, state, conn)
    check("раздел открывается", "Партнёры" in call.last)
    check("пустой список объясняется", "Партнёров пока нет" in call.last)
    check("есть кнопка добавления", "➕ Добавить партнёра" in buttons(call.markup))

    # ------------------------------------------------ добавление
    call = call_of("pn:pt_new")
    await panel.cb_partner_new(call, state)
    check("спрашивает имя", "Пришлите имя" in call.last)
    check("ждём имя", await state.get_state() == "PartnerNew:name")

    short = msg("я")
    await panel.on_partner_name(short, state)
    check("слишком короткое имя отклонено", "❌" in short.last)

    await panel.on_partner_name(msg("Алиджон"), state)
    check("спрашивает долю", await state.get_state() == "PartnerNew:share")

    bad = msg("сто")
    await panel.on_partner_share(bad, state, conn)
    check("нечисловая доля отклонена", "❌" in bad.last)
    over = msg("150")
    await panel.on_partner_share(over, state, conn)
    check("доля больше 100 отклонена", "❌" in over.last)

    done = msg("60")
    await panel.on_partner_share(done, state, conn)
    partners = await db.list_partners(conn)
    check("партнёр создан", len(partners) == 1 and partners[0].name == "Алиджон")
    check("доля сохранена", partners[0].share == 60, str(partners[0].share))
    check("шаг закрыт", await state.get_state() is None)

    # второй партнёр
    await panel.cb_partner_new(call_of("pn:pt_new"), state)
    await panel.on_partner_name(msg("Напарник"), state)
    await panel.on_partner_share(msg("40"), state, conn)
    check("второй партнёр добавлен", len(await db.list_partners(conn)) == 2)

    # ------------------------------------------------ продажи по товарам
    await sale(conn, price=100_00, cost=80_00, product="stars")    # +20.00
    await sale(conn, price=200_00, cost=170_00, product="stars")   # +30.00
    await sale(conn, price=70_00, cost=60_00, product="steam")     # +10.00
    money = await db.total_profit(conn)
    check("прибыль считается из заказов", money["profit"] == 60_00, str(money))
    check("выручка отдельно", money["revenue"] == 370_00, str(money))

    rows = await db.sales_by_product(conn)
    stars = next(r for r in rows if r["product_type"] == "stars")
    check("продажи сгруппированы по товару", stars["orders"] == 2, str(stars))
    check("выручка по товару верна", stars["revenue"] == 300_00, str(stars))
    check("прибыль по товару верна", stars["profit"] == 50_00, str(stars))
    check("у товара человеческое название", stars["title"] == "⭐ Звёзды")

    # ------------------------------------------------ закрепление товаров
    first, second = await db.list_partners(conn)
    call = call_of(f"pn:pt_goods:{first.id}")
    await panel.cb_partner_goods(call, conn)
    check("экран товаров открывается", "Товары: Алиджон" in call.last)
    check("все товары перечислены",
          all(t in call.last for t in ("⭐ Звёзды", "👑 Telegram Premium", "🎮 Steam")))
    check("пока все ничьи", call.last.count("ничей") == 3, call.last)

    await panel.cb_partner_take(call_of(f"pn:pt_take:{first.id}:stars"), conn)
    check("товар закреплён", (await db.product_owners(conn)).get("stars") == first.id)
    await panel.cb_partner_take(call_of(f"pn:pt_take:{second.id}:steam"), conn)
    check("второй товар у второго партнёра",
          (await db.product_owners(conn)).get("steam") == second.id)

    call = call_of(f"pn:pt_goods:{second.id}")
    await panel.cb_partner_goods(call, conn)
    check("чужой товар подписан именем владельца",
          "Алиджон" in call.last, call.last)

    call = call_of("pn:partners")
    await panel.cb_partners(call, state, conn)
    check("первому засчитаны его звёзды",
          "⭐ Звёзды: <b>2</b> шт. на <b>300.00 с.</b>" in call.last, call.last)
    check("первый заработал прибыль со звёзд",
          "Заработал: <b>50.00 с.</b>" in call.last, call.last)
    check("второму засчитан Steam",
          "🎮 Steam: <b>1</b> шт. на <b>70.00 с.</b>" in call.last, call.last)
    check("второй заработал прибыль со Steam",
          "Заработал: <b>10.00 с.</b>" in call.last, call.last)
    check("общая прибыль показана", "Общая прибыль: 60.00 с." in call.last)
    check("нераспределённого не осталось", "Не закреплено" not in call.last)

    # снять товар — он снова ничей
    await panel.cb_partner_take(call_of(f"pn:pt_take:{second.id}:steam"), conn)
    check("товар снимается", "steam" not in await db.product_owners(conn))
    call = call_of("pn:partners")
    await panel.cb_partners(call, state, conn)
    check("ничей товар показан отдельно", "Не закреплено" in call.last, call.last)
    check("снятый Steam попал именно туда",
          call.last.index("Не закреплено") < call.last.index("🎮 Steam: <b>1</b>"),
          call.last)

    # с долями нераспределённое делится
    await db.update_partner(conn, first.id, share=50)
    await db.update_partner(conn, second.id, share=50)
    call = call_of("pn:partners")
    await panel.cb_partners(call, state, conn)
    check("доля от нераспределённого посчитана",
          "Доля от общего (50%): <b>5.00 с.</b>" in call.last, call.last)
    check("заработок вырос на эту долю",
          "Заработал: <b>55.00 с.</b>" in call.last, call.last)
    await db.set_product_owner(conn, "steam", second.id)
    await db.update_partner(conn, first.id, share=0)
    await db.update_partner(conn, second.id, share=0)

    # ------------------------------------------------ деньги на реквизиты
    deposit = await db.create_deposit(
        conn, user_id=900, amount=150_00, method="card", receipt_file_id="x")
    await db.resolve_deposit(conn, deposit.id, approved=True, admin_id=ADMIN)
    call = call_of("pn:partners")
    await panel.cb_partners(call, state, conn)
    check("видно, сколько пришло на реквизиты",
          "Всего: <b>150.00 с.</b>" in call.last, call.last[-400:])
    check("и сколько было пополнений", "Пополнений: <b>1</b>" in call.last)

    # отменённый заказ в прибыль не идёт
    refunded = await db.create_order(
        conn, user_id=900, product_type="stars", quantity=100,
        recipient="kto", price=500_00, cost=400_00,
    )
    await db.update_order(conn, refunded.id, status=db.ORDER_REFUNDED)
    check("возврат в прибыль не попал",
          (await db.total_profit(conn))["profit"] == 60_00,
          str((await db.total_profit(conn))["profit"]))

    # ------------------------------------------------ взносы и выплаты
    first = (await db.list_partners(conn))[0]
    call = call_of(f"pn:pt_in:{first.id}")
    await panel.cb_partner_in(call, state, conn)
    check("спрашивает сумму взноса", "внёс в оборот" in call.last, call.last[:80])
    check("ждём сумму", await state.get_state() == "PartnerMove:amount")

    bad = msg("много")
    await panel.on_partner_amount(bad, state, conn)
    check("нечисловая сумма отклонена", "❌" in bad.last)

    await panel.on_partner_amount(msg("500 пополнил FazerCards"), state, conn)
    totals = await db.partner_totals(conn, first.id)
    check("взнос записан", totals["put_in"] == 500_00, str(totals))
    check("примечание сохранено",
          (await db.partner_moves(conn, first.id))[0]["note"] == "пополнил FazerCards")

    call = call_of(f"pn:pt_out:{first.id}")
    await panel.cb_partner_out(call, state, conn)
    check("спрашивает сумму выплаты", "забрал себе" in call.last)
    await panel.on_partner_amount(msg("200"), state, conn)
    totals = await db.partner_totals(conn, first.id)
    check("выплата записана", totals["took_out"] == 200_00, str(totals))
    check("взнос при этом не изменился", totals["put_in"] == 500_00)

    # на руках = прибыль его товаров + внёс − забрал = 50 + 500 − 200 = 350
    call = call_of(f"pn:pt:{first.id}")
    await panel.cb_partner_card(call, conn)
    check("на руках посчитано верно", "На руках: <b>350.00 с.</b>" in call.last,
          call.last)
    check("в карточке видна история", "Движение денег" in call.last)
    check("в истории видны и плюс, и минус",
          "+500.00" in call.last and "−200.00" in call.last, call.last)
    check("есть обе кнопки", "➕ Внёс в оборот" in buttons(call.markup)
          and "➖ Забрал себе" in buttons(call.markup))

    # ------------------------------------------------ правка доли
    call = call_of(f"pn:pt_share:{first.id}")
    await panel.cb_partner_share(call, state, conn)
    check("экран правки доли", "Сейчас: <b>" in call.last, call.last[:120])
    await panel.on_partner_share(msg("70"), state, conn)
    check("доля изменена", (await db.get_partner(conn, first.id)).share == 70)
    check("нового партнёра при этом не создалось",
          len(await db.list_partners(conn)) == 2)

    # ------------------------------------------------ удаление
    call = call_of(f"pn:pt_del:{first.id}")
    await panel.cb_partner_delete(call, conn)
    check("партнёр убран из списка", len(await db.list_partners(conn)) == 1)
    check("история его движений сохранена",
          (await db.partner_totals(conn, first.id))["put_in"] == 500_00)
    check("карточка удалённого не падает", "Партнёры" in call.last)

    check("раздел есть в главном меню панели",
          any("Партнёры" in b.text for r in panel.home_kb().inline_keyboard for b in r))


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


asyncio.run(main())
