"""Отзывы: оценка после заказа, модерация, публикация, защита от повторов."""
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

from app import db, keyboards, runtime, texts
from app.handlers import panel, reviews as rv
from app.services import reviews as service

BUYER = 777
ADMIN = 111
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class SentMessage:
    """То, что «ушло» в Telegram."""

    def __init__(self, chat_id, text, markup):
        self.chat_id, self.text, self.markup = chat_id, text, markup
        self.message_id = 1000 + len(text) % 100


class FakeBot:
    def __init__(self, fail_for=None, blocked_by=()):
        self.sent: list[SentMessage] = []
        self.fail_for = fail_for      # куда отправка не проходит
        self.blocked_by = set(blocked_by)   # кто заблокировал бота

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked_by:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        if self.fail_for is not None and chat_id == self.fail_for:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="chat not found")
        sent = SentMessage(chat_id, text, reply_markup)
        self.sent.append(sent)
        return sent

    def to(self, chat_id) -> list[SentMessage]:
        return [m for m in self.sent if m.chat_id == chat_id]


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
    def alerts(self) -> list:
        return self._alerts


def msg(text=None, uid=BUYER) -> SpyMessage:
    user = User(id=uid, is_bot=False, first_name="Покупатель", username="buyer")
    return SpyMessage.model_construct(
        message_id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=user, text=text,
    )


def call_of(data: str, uid=BUYER) -> SpyCallback:
    user = User(id=uid, is_bot=False, first_name="Покупатель", username="buyer")
    return SpyCallback.model_construct(
        id="1", from_user=user, chat_instance="x", data=data, message=msg(uid=uid),
    )


def state_for(uid: int, storage) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def make_order(conn, status=db.ORDER_DELIVERED, *, user_id=BUYER) -> db.Order:
    order = await db.create_order(
        conn, user_id=user_id, product_type="stars", quantity=100,
        recipient="kto", price=17_00,
    )
    if status != db.ORDER_DELIVERING:
        await db.update_order(conn, order.id, status=status)
    return await db.get_order(conn, order.id)


async def run(conn) -> None:
    storage = MemoryStorage()
    state = state_for(BUYER, storage)
    await db.upsert_user(conn, BUYER, "buyer", "Покупатель")
    await db.upsert_user(conn, ADMIN, "admin", "Админ")
    await runtime.set_value(conn, "reviews_channel", "@moi_otzyvy")

    # ------------------------------------------- предложение после выдачи
    order = await make_order(conn)
    bot = FakeBot()
    check("после выдачи бот предлагает оценить",
          await service.offer(bot, conn, order) is True)
    offered = bot.to(BUYER)[0]
    check("предложение ушло покупателю", "Ҳама чиз чӣ тавр гузашт" in offered.text, offered.text[:60])
    check("в предложении номер заказа", f"№{order.id}" in offered.text)
    check("пять кнопок оценки плюс отказ",
          len(buttons(offered.markup)) == 6, str(buttons(offered.markup)))
    check("есть кнопка «Не сейчас»", "Не сейчас" in buttons(offered.markup))

    await runtime.set_value(conn, "reviews_on", "0")
    check("выключённые отзывы не спрашиваются",
          await service.offer(FakeBot(), conn, order) is False)
    await runtime.set_value(conn, "reviews_on", "1")

    # ------------------------------------------------------- чужой заказ
    call = call_of(f"rv:rate:{order.id}:5", uid=999)
    await db.upsert_user(conn, 999, "chuzhoi", "Чужой")
    await rv.cb_rate(call, state_for(999, storage), conn)
    check("чужой заказ оценить нельзя",
          any("не ваш" in a for a in call.alerts), str(call.alerts))
    check("отзыв при этом не создан", await db.review_of_order(conn, order.id) is None)

    # --------------------------------------------- невыполненный заказ
    unfinished = await make_order(conn, db.ORDER_DELIVERING)
    call = call_of(f"rv:rate:{unfinished.id}:5")
    await rv.cb_rate(call, state, conn)
    check("незавершённый заказ оценить нельзя",
          any("выполненному" in a for a in call.alerts), str(call.alerts))

    # ------------------------------------------------------- оценка
    call = call_of(f"rv:rate:{order.id}:5")
    await rv.cb_rate(call, state, conn)
    review = await db.review_of_order(conn, order.id)
    check("отзыв создан", review is not None and review.rating == 5)
    check("создан со статусом «на проверке»", review.status == db.REVIEW_PENDING)
    check("бот просит текст", "ду калима нависед" in call.last, call.last[:80])
    check("предупреждает, что имя будет видно", "Номи шумо дар зери шарҳ дида мешавад" in call.last)
    check("ждём текст", await state.get_state() == "Review:text")
    check("можно отправить без текста",
          "Отправить без текста" in buttons(call.message.markup))

    # ------------------------------------------- повторная оценка того же заказа
    call = call_of(f"rv:rate:{order.id}:1")
    await rv.cb_rate(call, state, conn)
    check("второй отзыв на заказ не создаётся", "аллакай ҳаст" in call.last, call.last[:60])
    fresh = await db.review_of_order(conn, order.id)
    check("первая оценка не перезаписана", fresh.rating == 5, str(fresh.rating))

    # --------------------------------------------------------- текст отзыва
    await state.set_state(rv.Review.text)
    await state.update_data(review_id=review.id)

    long_text = msg("а" * 1001)
    await rv.on_review_text(long_text, state, conn, FakeBot())
    check("слишком длинный отзыв отклонён", "Слишком длинно" in long_text.last)
    check("после отказа всё ещё ждём текст", await state.get_state() == "Review:text")

    bot = FakeBot()
    message = msg("Всё пришло за минуту, спасибо!")
    await rv.on_review_text(message, state, conn, bot)
    saved = await db.get_review(conn, review.id)
    check("текст сохранён", saved.text == "Всё пришло за минуту, спасибо!", str(saved.text))
    check("клиенту сказано спасибо", "Ташаккур барои шарҳ" in message.last)
    check("шаг закрыт", await state.get_state() is None)

    # ---------------------------------------------------- пришло админу
    to_admin = bot.to(ADMIN)
    check("отзыв ушёл админу", len(to_admin) == 1, str(len(to_admin)))
    card = to_admin[0]
    check("в карточке оценка", "⭐️⭐️⭐️⭐️⭐️" in card.text and "(5/5)" in card.text)
    check("в карточке текст отзыва", "Всё пришло за минуту" in card.text)
    check("в карточке номер заказа", f"№{order.id}" in card.text)
    check("в карточке виден никнейм", "Никнейм: @buyer" in card.text, card.text)
    check("в карточке видно имя", "Имя: <b>Покупатель</b>" in card.text, card.text)
    check("в карточке есть ссылка на переписку",
          'tg://user?id=777' in card.text, card.text)
    check("в карточке показано, как подпишут в канале",
          "подпишут в канале: @buyer" in card.text, card.text[-120:])
    check("две кнопки решения",
          buttons(card.markup) == ["✅ Опубликовать", "🗑 Удалить"], str(buttons(card.markup)))
    check("в канал пока ничего не ушло", bot.to("@moi_otzyvy") == [])

    # ------------------------------------------- чужой не может модерировать
    call = call_of(f"rv:ok:{review.id}", uid=BUYER)
    await rv.cb_publish(call, conn, FakeBot())
    check("клиент не может публиковать сам",
          any("владельца" in a for a in call.alerts), str(call.alerts))
    check("отзыв остался на проверке",
          (await db.get_review(conn, review.id)).status == db.REVIEW_PENDING)

    # ------------------------------------------------------- публикация
    bot = FakeBot()
    call = call_of(f"rv:ok:{review.id}", uid=ADMIN)
    await rv.cb_publish(call, conn, bot)
    posted = bot.to("@moi_otzyvy")
    check("отзыв ушёл в канал", len(posted) == 1, str(len(posted)))
    post = posted[0].text
    check("в канале оценка звёздами", post.startswith("⭐️⭐️⭐️⭐️⭐️"), post[:40])
    check("в канале жирный заголовок", "<b>Шарҳи харид</b>" in post)
    check("текст отзыва в цитате",
          "<blockquote>Всё пришло за минуту, спасибо!</blockquote>" in post, post)
    check("в канале виден товар", "100 ситора" in post)
    check("в канале виден автор", "@buyer" in post)
    published = await db.get_review(conn, review.id)
    check("статус стал «опубликован»", published.status == db.REVIEW_PUBLISHED)
    check("id сообщения в канале сохранён", published.channel_msg == posted[0].message_id)
    check("на карточке отмечено решение", "Опубликован" in call.last, call.last[-40:])

    # ------------------------------------------ повторное нажатие ничего не портит
    bot = FakeBot()
    call = call_of(f"rv:ok:{review.id}", uid=ADMIN)
    await rv.cb_publish(call, conn, bot)
    check("второй раз в канал не уходит", bot.to("@moi_otzyvy") == [])
    check("и об этом сказано", any("разобран" in a for a in call.alerts), str(call.alerts))

    # ------------------------------------------------------------ удаление
    second = await make_order(conn)
    review2 = await db.create_review(conn, order_id=second.id, user_id=BUYER, rating=2)
    await db.set_review_text(conn, review2.id, "Долго ждал")

    bot = FakeBot()
    call = call_of(f"rv:no:{review2.id}", uid=ADMIN)
    await rv.cb_delete(call, conn)
    check("удалённый отзыв в канал не идёт", bot.to("@moi_otzyvy") == [])
    check("статус стал «удалён»",
          (await db.get_review(conn, review2.id)).status == db.REVIEW_DELETED)
    check("на карточке видно удаление", "Удалён" in call.last, call.last[-40:])

    call = call_of(f"rv:no:{review2.id}", uid=ADMIN)
    await rv.cb_delete(call, conn)
    check("повторное удаление отбивается",
          any("разобран" in a for a in call.alerts), str(call.alerts))

    # ------------------------------- канал недоступен: отзыв остаётся на проверке
    third = await make_order(conn)
    review3 = await db.create_review(conn, order_id=third.id, user_id=BUYER, rating=4)
    bot = FakeBot(fail_for="@moi_otzyvy")
    call = call_of(f"rv:ok:{review3.id}", uid=ADMIN)
    await rv.cb_publish(call, conn, bot)
    check("при отказе канала статус не меняется",
          (await db.get_review(conn, review3.id)).status == db.REVIEW_PENDING)
    check("админу объяснили причину", "не ушёл в канал" in call.last, call.last[:80])
    check("подсказано, что проверить", "администратором" in call.last)

    # отзыв без текста публикуется короткой формой
    bot = FakeBot()
    call = call_of(f"rv:ok:{review3.id}", uid=ADMIN)
    await rv.cb_publish(call, conn, bot)
    post = bot.to("@moi_otzyvy")[0].text
    check("отзыв без текста тоже публикуется", "Шарҳи харид" in post)
    check("пустой цитаты в нём нет", "<blockquote>" not in post, post)

    # ------------------------------------------------------------- панель
    stats = await db.review_stats(conn)
    check("статистика считает опубликованные", stats["published"] == 2, str(stats))
    check("удалённые в среднюю оценку не идут", stats["total"] == 2, str(stats))

    call = call_of("pn:reviews", uid=ADMIN)
    await panel.cb_reviews(call, state_for(ADMIN, storage), conn)
    check("раздел открывается", "Отзывы" in call.last)
    check("виден канал", "@moi_otzyvy" in call.last)
    check("видна средняя оценка", "4.5" in call.last, call.last)
    check("есть переключатель", any("отзывы" in b for b in
          [b.text for r in panel.reviews_kb([]).inline_keyboard for b in r]))

    await runtime.set_value(conn, "reviews_on", "1")
    call = call_of("pn:rev_toggle", uid=ADMIN)
    await panel.cb_reviews_toggle(call, conn)
    check("переключатель выключает отзывы", runtime.get_bool("reviews_on") is False)
    await panel.cb_reviews_toggle(call_of("pn:rev_toggle", uid=ADMIN), conn)
    check("и включает обратно", runtime.get_bool("reviews_on") is True)

    # ------------------------------------------------- кнопка в меню клиента
    check("канал даёт кнопку отзывов в меню",
          keyboards.reviews_link() == "https://t.me/moi_otzyvy",
          keyboards.reviews_link())
    check("кнопка «Отзывы» появилась",
          any("Шарҳҳо" in b.text for r in keyboards.main_menu().inline_keyboard for b in r))
    await runtime.set_value(conn, "reviews_channel", "")
    check("без канала ссылки нет", keyboards.reviews_link() == "")

    await past_buyers(conn, storage)


async def past_buyers(conn, storage) -> None:
    """Кнопка «спросить у тех, кто покупал раньше»."""
    from app.services import reviews as service

    # три старых покупателя: обычный, с двумя заказами и заблокировавший бота
    for uid in (601, 602, 603):
        await db.upsert_user(conn, uid, f"stary{uid}", "Старый")
    old_one = await make_order(conn, user_id=601)
    await make_order(conn, user_id=602)
    newest = await make_order(conn, user_id=602)      # у второго два заказа
    await make_order(conn, user_id=603)
    # четвёртый только оформил, но заказ не выдан — спрашивать не о чем
    await db.upsert_user(conn, 604, "vrabote", "В работе")
    await make_order(conn, db.ORDER_DELIVERING, user_id=604)

    targets = await db.review_targets(conn)
    ids = {order.user_id for order in targets}
    check("в список попали прошлые покупатели", {601, 602, 603} <= ids, str(ids))
    check("незавершённый заказ не попал", 604 not in ids, str(ids))
    check("у клиента с двумя заказами берётся свежий",
          any(o.id == newest.id for o in targets)
          and not any(o.user_id == 602 and o.id != newest.id for o in targets))

    bot = FakeBot(blocked_by={603})
    report = await service.ask_past_buyers(bot, conn)
    check("написали всем, кроме заблокировавшего", report["sent"] == len(targets) - 1,
          str(report))
    check("блокировка посчитана отдельно", report["blocked"] == 1, str(report))
    check("просьба ушла первому", bot.to(601), str(len(bot.to(601))))
    body = bot.to(601)[0].text
    check("в просьбе сказано, что человек уже покупал",
          "Шумо аз мо харид кардед" in body, body[:60])
    check("в просьбе виден заказ", f"№{old_one.id}" in body, body)
    check("к просьбе приложены оценки",
          len(buttons(bot.to(601)[0].markup)) == 6)

    check("повторное нажатие никого не трогает",
          await db.review_targets(conn) == [])
    again = await service.ask_past_buyers(FakeBot(), conn)
    check("вторая рассылка никому не пишет", again["sent"] == 0, str(again))

    # оценка из такой рассылки работает как обычная
    state = state_for(601, storage)
    call = call_of(f"rv:rate:{old_one.id}:4", uid=601)
    await rv.cb_rate(call, state, conn)
    review = await db.review_of_order(conn, old_one.id)
    check("отзыв из рассылки создаётся", review is not None and review.rating == 4)
    check("и просит текст", "ду калима нависед" in call.last, call.last[:60])

    bot = FakeBot()
    message = msg("Брал год назад, всё дошло", uid=601)
    await state.set_state(rv.Review.text)
    await state.update_data(review_id=review.id)
    await rv.on_review_text(message, state, conn, bot)
    check("отзыв из рассылки уходит на модерацию",
          any("проверку" in m.text for m in bot.to(ADMIN)), str(bot.to(ADMIN)))

    # заблокировавшего бота больше не дёргаем
    check("заблокировавший помечен и не вернётся в список",
          603 not in {o.user_id for o in await db.review_targets(conn)})

    # у клиента без юзернейма это должно быть сказано прямо, а не пусто
    from app.services import reviews as svc
    await db.upsert_user(conn, 605, None, "Безымянный")
    order = await make_order(conn, user_id=605)
    plain = await db.create_review(conn, order_id=order.id, user_id=605, rating=5)
    bot = FakeBot()
    await svc.to_moderation(bot, conn, plain)
    card = bot.to(ADMIN)[0].text
    check("отсутствие никнейма показано словами",
          "Никнейм: <i>не установлен</i>" in card, card)
    check("вместо него в подписи имя", "подпишут в канале: Безымянный" in card, card)


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
