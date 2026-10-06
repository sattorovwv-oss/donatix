"""Юзербот: разбор уведомлений банка, сопоставление заявок, защита от дублей.

Настоящих банковских данных здесь нет и быть не может: номера карт и
отправителей выдуманы, суммы взяты из задачи.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, runtime, texts
from app.money import fmt
from app.userbot import parser, processor
from app.userbot.log import scrub
from app.config import settings as _settings

# Здесь бот читает уведомления банка — только тогда к сумме добавляются копейки
_settings.tg_api_id, _settings.tg_api_hash, _settings.bank_bot = 1, "x" * 32, "bank_test_bot"

CLIENT = 901
OTHER = 902
SOURCE = "bank_test_bot"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeBot:
    """Вместо Telegram — список писем, чтобы видеть, что бот сказал."""

    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ""

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edited.append((chat_id, message_id, text))

    async def edit_message_reply_markup(self, chat_id, message_id, **kw):
        self.edited.append((chat_id, message_id, "<кнопки убраны>"))

    def clear(self) -> None:
        self.sent.clear()
        self.edited.clear()


# ────────────────────────────────────────────────── образцы сообщений


REAL = """Zachislenie
Summa 617.00 TJS
Komis 0.00 TJS
Zachislenie 617.00 TJS
Data 19:40 15.09.26
Otpravitel 9990000***1111
Kod 10000000001
Karta 9999000011112222
Balans 3 686.07 TJS"""


def sample(**changes) -> str:
    """Собрать уведомление из кусочков: так проще менять по одному полю."""
    parts = {
        "head": "Zachislenie",
        "summa": "Summa 100.00 TJS",
        "komis": "Komis 0.00 TJS",
        "zach": "Zachislenie 100.00 TJS",
        "data": "Data 19:40 15.09.26",
        "otpr": "Otpravitel 9990000***1111",
        "kod": "Kod 10000000001",
        "karta": "Karta 9999000011112222",
        "balans": "Balans 3 686.07 TJS",
    }
    parts.update(changes)
    return "\n".join(value for value in parts.values() if value)


# ────────────────────────────────────────────────── разбор


def parsing() -> None:
    # 1. обычное зачисление 617.00
    n = parser.parse(REAL)
    check("1. обычное зачисление 617.00", n.ok and n.amount == 61700, str(n.amount))
    check("   сумма взята из поля Zachislenie",
          n.source_field == "zachislenie", n.source_field)
    check("   код операции найден", n.op_code == "10000000001", n.op_code)
    check("   время найдено", n.bank_time == "19:40 15.09.26", n.bank_time)
    check("   баланс суммой не стал", n.amount != 368607, str(n.amount))
    check("   комиссия суммой не стала", n.amount != 0)

    # 2. зачисление 100 TJS
    n = parser.parse(sample())
    check("2. зачисление 100 TJS", n.ok and n.amount == 10000, str(n.amount))

    # 3. сумма с запятой
    n = parser.parse(sample(zach="Zachislenie 617,50 TJS"))
    check("3. сумма с запятой", n.ok and n.amount == 61750, str(n.amount))

    # 4. лишние пробелы
    n = parser.parse("  Zachislenie  \n\n   Zachislenie    617.00   TJS   \n"
                     "   Komis   0.00 TJS  \n")
    check("4. лишние пробелы не мешают", n.ok and n.amount == 61700, str(n.amount))

    # 5. изменённый порядок строк
    shuffled = "\n".join([
        "Zachislenie", "Balans 3 686.07 TJS", "Kod 10000000001",
        "Zachislenie 617.00 TJS", "Karta 9999000011112222",
        "Komis 0.00 TJS", "Data 19:40 15.09.26", "Summa 617.00 TJS",
    ])
    n = parser.parse(shuffled)
    check("5. порядок строк не важен", n.ok and n.amount == 61700, str(n.amount))

    # 6. нет Kod
    n = parser.parse(sample(kod=""))
    check("6. без кода операции всё равно разбирается",
          n.ok and n.amount == 10000 and n.op_code == "", str(n.error))

    # 7. нет Otpravitel
    n = parser.parse(sample(otpr=""))
    check("7. без отправителя всё равно разбирается",
          n.ok and n.sender == "", str(n.error))

    # 8. нет суммы
    n = parser.parse("Zachislenie\nData 19:40 15.09.26\nKod 1234")
    check("8. без суммы — отказ", not n.ok, n.error)

    # 9. нет слова Zachislenie
    n = parser.parse("Spisanie\nSumma 617.00 TJS\nKomis 0.00 TJS")
    check("9. списание не принимается за зачисление", not n.ok, n.error)
    n = parser.parse("Summa 617.00 TJS\nKomis 0.00 TJS\nBalans 10.00 TJS")
    check("   и без слова о зачислении тоже отказ", not n.ok, n.error)

    # 10. комиссия 0, зачисление 617
    n = parser.parse(sample(summa="Summa 620.00 TJS", komis="Komis 3.00 TJS",
                            zach="Zachislenie 617.00 TJS"))
    check("10. берём зачисленное, а не отправленное",
          n.ok and n.amount == 61700, str(n.amount))

    # 14. 617.01 — это другая сумма
    n = parser.parse(sample(zach="Zachislenie 617.01 TJS"))
    check("14. 617.01 не равно 617.00", n.amount == 61701, str(n.amount))

    # 15. очень большая сумма
    n = parser.parse(sample(zach="Zachislenie 999999999999.00 TJS"))
    check("15. неправдоподобная сумма отклонена", not n.ok, n.error)
    n = parser.parse(sample(zach="Zachislenie 50 000.00 TJS"))
    check("   но большая настоящая проходит",
          n.ok and n.amount == 5_000_000, str(n.amount))

    # 16. мусор
    for junk in ("", "   ", "привет", "Zachislenie\nSumma много TJS",
                 "Zachislenie 617.00.00 TJS", "Zachislenie -617.00 TJS",
                 "Zachislenie 0.00 TJS"):
        n = parser.parse(junk)
        check(f"16. мусор отклонён: {junk[:22]!r}", not n.ok, n.error)

    # без Zachislenie-суммы, но с комиссией — зачисленное неизвестно
    n = parser.parse("Zachislenie\nSumma 617.00 TJS\nKomis 3.00 TJS")
    check("с комиссией и без суммы зачисления — отказ", not n.ok, n.error)
    n = parser.parse("Zachislenie\nSumma 617.00 TJS\nKomis 0.00 TJS")
    check("без комиссии сумма берётся из Summa",
          n.ok and n.amount == 61700 and n.source_field == "summa", n.source_field)

    # регистр
    n = parser.parse("ZACHISLENIE\nZACHISLENIE 617.00 TJS\nKOMIS 0.00 TJS")
    check("регистр букв не важен", n.ok and n.amount == 61700, str(n.error))

    # лишние строки банка
    n = parser.parse(sample() + "\nSpasibo za ispolzovanie\nwww.bank.tj")
    check("лишние строки не мешают", n.ok and n.amount == 10000, str(n.error))


def masking() -> None:
    n = parser.parse(REAL)
    check("номер карты обрезан до четырёх знаков",
          n.card_tail == "2222" and len(n.card_tail) == 4, n.card_tail)
    check("полного номера карты нет нигде",
          "9999000011112222" not in str(n), str(n))

    body = parser.safe_body(REAL)
    check("в сохранённом тексте нет номера карты",
          "9999000011112222" not in body, body[-60:])
    check("но хвост карты виден для сверки", "2222" in body)
    check("сумма в сохранённом тексте осталась", "617.00" in body)

    check("логи чистят номер карты",
          "9999000011112222" not in scrub("Karta 9999000011112222"))
    check("логи чистят токен бота",
          "AAHdq" not in scrub("8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))
    check("логи чистят api_hash",
          "0123456789abcdef0123456789abcdef" not in
          scrub("hash 0123456789abcdef0123456789abcdef"))
    check("сумму логи не портят", "617.00" in scrub("Summa 617.00 TJS"))


# ────────────────────────────────────────────────── сопоставление


#: Как выглядит уведомление о переводе из России. Отличий от обычного
#: два, и оба ломали разбор: суммы зачисления отдельной строкой нет, и
#: строки о комиссии нет вовсе — банк удержал своё на той стороне.
SBER = """Zachislenie
Summa 10.08 TJS
Data 15:47 18.09.26
Comment: Cбербанк
Kod 19214356988
Karta 9999000011112222
Balans 2 805.06 TJS"""


def sberbank() -> None:
    """Перевод из России должен разбираться, а не отвергаться."""
    notice = parser.parse(SBER)
    check("уведомление Сбербанка разобрано", notice.ok, notice.error)
    check("сумма взята из Summa", notice.amount == 1008, str(notice.amount))
    check("видно, откуда сумма", notice.source_field == "summa",
          notice.source_field)
    check("приписка банка сохранена", notice.comment == "Cбербанк",
          repr(notice.comment))
    check("полного номера карты в разборе нет",
          notice.card_tail == "2222", notice.card_tail)

    # Комиссия названа — сумма зачисления снова неизвестна, и гадать
    # нельзя: Summa тогда больше того, что легло на счёт.
    with_fee = SBER.replace("Summa 10.08 TJS", "Summa 10.08 TJS\nKomis 0.50 TJS")
    check("с комиссией по-прежнему отказ", not parser.parse(with_fee).ok,
          parser.parse(with_fee).error)

    zero_fee = SBER.replace("Summa 10.08 TJS", "Summa 10.08 TJS\nKomis 0.00 TJS")
    check("нулевая комиссия не мешает", parser.parse(zero_fee).ok,
          parser.parse(zero_fee).error)

    # Латинская C в «Cбербанк» — прямо из настоящего чека. Глазом не
    # отличить, для сравнения это разные строки.
    check("латинские двойники букв не мешают опознать банк",
          processor.from_russia("Cбербанк") and processor.from_russia("Тинькофф"),
          "Cбербанк")
    check("чужая приписка за Сбербанк не сходит",
          not processor.from_russia("Perevod Dushanbe"))


def log_filter() -> None:
    """Чистка секретов не должна ломать сами записи журнала.

    Фильтр приводил все подставляемые значения к строке — и запись вида
    «id=%d» с числом внутри переставала складываться: logging роняло её
    с ошибкой формата, а вместе с ней и всё, что в ней было написано.
    """
    import io
    import logging

    from app.userbot.log import Scrubber

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(Scrubber())
    log = logging.getLogger("проверка-фильтра")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False

    log.info("Run polling for bot @%s id=%d - %r", "mybot", 8651298254, "Имя")
    written = stream.getvalue()
    check("запись с числом складывается",
          "id=8651298254" in written, written.strip()[:60])

    log.info("ключ %s принят", "sk_live_" + "A" * 32)
    check("а ключ из неё всё равно вычищен",
          "sk_live_<скрыт>" in stream.getvalue(),
          stream.getvalue().splitlines()[-1][:60])

    log.info("сумма %.2f", 10.5)
    check("дробное число тоже переживает чистку",
          "10.50" in stream.getvalue(), stream.getvalue().splitlines()[-1][:40])


async def matching(conn, bot) -> None:
    await db.upsert_user(conn, CLIENT, "client", "Клиент")
    await db.upsert_user(conn, OTHER, "other", "Второй")

    # ---- ровно одна заявка: зачисляем
    deposit = await db.create_deposit(
        conn, user_id=CLIENT, amount=61700, method="card", receipt_file_id="x",
    )
    before = (await db.get_user(conn, CLIENT)).balance
    bot.clear()

    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=1001, text=REAL)
    check("одна заявка — оплата подтверждена", result.confirmed, result.status)
    check("закрыта именно она", result.deposit_id == deposit.id,
          str(result.deposit_id))
    check("деньги зачислены клиенту",
          (await db.get_user(conn, CLIENT)).balance == before + 61700,
          str((await db.get_user(conn, CLIENT)).balance))
    check("заявка помечена зачисленной",
          (await db.get_deposit(conn, deposit.id)).status == db.DEP_APPROVED)
    check("клиенту пришло сообщение",
          any(chat == CLIENT for chat, _ in bot.sent), str(bot.sent[:1]))
    check("владельцу пришло сообщение",
          any("подтверждена" in text for _, text in bot.sent), bot.last()[:60])

    saved = (await db.list_bank_payments(conn))[0]
    check("платёж записан в базу", saved.amount == 61700, str(saved.amount))
    check("код операции сохранён", saved.op_code == "10000000001", str(saved.op_code))
    check("в базе нет полного номера карты",
          "9999000011112222" not in (saved.body or "") and saved.card_tail == "2222",
          saved.card_tail)
    check("платёж связан с заявкой", saved.deposit_id == deposit.id)

    # ---- 12/13. повтор того же сообщения
    balance = (await db.get_user(conn, CLIENT)).balance
    again = await processor.handle(
        conn, bot, source=SOURCE, message_id=1001, text=REAL)
    check("12. повторное сообщение отброшено", again.status == "duplicate",
          again.status)
    check("13. деньги второй раз не зачислены",
          (await db.get_user(conn, CLIENT)).balance == balance)

    # ---- тот же код операции, но другой message_id
    third = await processor.handle(
        conn, bot, source=SOURCE, message_id=1002, text=REAL)
    check("тот же код банка не проходит дважды", third.status == "duplicate",
          third.status)
    check("и денег не прибавилось",
          (await db.get_user(conn, CLIENT)).balance == balance)

    # ---- 11. два заказа с одинаковой суммой
    first = await db.create_deposit(
        conn, user_id=CLIENT, amount=10000, method="card", receipt_file_id="a")
    second = await db.create_deposit(
        conn, user_id=OTHER, amount=10000, method="card", receipt_file_id="b")
    balance = (await db.get_user(conn, CLIENT)).balance
    other_balance = (await db.get_user(conn, OTHER)).balance
    bot.clear()

    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2001,
        text=sample(kod="Kod 77001", otpr="Otpravitel 9991110001111"))
    check("11. две заявки на одну сумму — не выбираем наугад",
          result.status == db.BANK_AMBIGUOUS, result.status)
    check("    деньги никому не зачислены",
          (await db.get_user(conn, CLIENT)).balance == balance
          and (await db.get_user(conn, OTHER)).balance == other_balance)
    check("    обе заявки остались на проверке",
          (await db.get_deposit(conn, first.id)).status == db.DEP_PENDING
          and (await db.get_deposit(conn, second.id)).status == db.DEP_PENDING)
    check("    владельцу показали обе",
          f"№{first.id}" in bot.last() and f"№{second.id}" in bot.last(),
          bot.last()[:200])
    check("    клиенту ничего не писали",
          not any(chat in (CLIENT, OTHER) for chat, _ in bot.sent),
          str([c for c, _ in bot.sent]))

    # ---- 14. сумма не совпала на копейку
    await db.resolve_deposit(conn, second.id, approved=False, admin_id=1)
    balance = (await db.get_user(conn, CLIENT)).balance
    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2002,
        text=sample(zach="Zachislenie 100.01 TJS", kod="Kod 77002",
                    otpr="Otpravitel 9991110002222"))
    # 14. Сумма разошлась на копейку. Раньше это был отказ, теперь —
    # закрытие ближайшей заявки, если она рядом ровно одна: бот сам
    # просит платить с копейками, и человек, отправивший круглую сумму,
    # не должен из-за этого ждать владельца. Двух рядом это не касается.
    check("14. копейка разницы закрывает единственную соседнюю заявку",
          result.confirmed, result.status)
    check("    зачислено столько, сколько пришло",
          (await db.get_user(conn, CLIENT)).balance == balance + 10001,
          str((await db.get_user(conn, CLIENT)).balance))
    check("    и заявка стала на ту же сумму",
          (await db.get_deposit(conn, first.id)).amount == 10001,
          str((await db.get_deposit(conn, first.id)).amount))

    # А вот когда рядом двое — по-прежнему решает владелец.
    near1 = await db.create_deposit(conn, user_id=CLIENT, amount=7001,
                                    method="card", receipt_file_id="")
    near2 = await db.create_deposit(conn, user_id=OTHER, amount=7002,
                                    method="card", receipt_file_id="")
    balance = (await db.get_user(conn, CLIENT)).balance
    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2009,
        text=sample(zach="Zachislenie 70.00 TJS", summa="Summa 70.00 TJS",
                    kod="Kod 77009", otpr="Otpravitel 9991110009999"))
    check("    но двух соседей наугад не разбираем",
          result.status == db.BANK_AMBIGUOUS, result.status)
    check("    и деньги никому не ушли",
          (await db.get_user(conn, CLIENT)).balance == balance
          and (await db.get_deposit(conn, near1.id)).status == db.DEP_PENDING
          and (await db.get_deposit(conn, near2.id)).status == db.DEP_PENDING)
    await db.resolve_deposit(conn, near1.id, approved=False, admin_id=1)
    await db.resolve_deposit(conn, near2.id, approved=False, admin_id=1)

    # ---- заявки нет вовсе
    bot.clear()
    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2003,
        text=sample(zach="Zachislenie 777.00 TJS", summa="Summa 777.00 TJS",
                    kod="Kod 77003", otpr="Otpravitel 9991110003333"))
    check("оплата без заявки помечена неопознанной",
          result.status == db.BANK_UNKNOWN, result.status)
    check("владельцу сказали про неё", "без заявки" in bot.last(), bot.last()[:60])

    # ---- неразобранное сообщение
    bot.clear()
    open_one = await db.create_deposit(conn, user_id=CLIENT, amount=9500,
                                       method="card", receipt_file_id="")
    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2004, text="Spisanie\nSumma 50.00 TJS")
    check("списание не закрывает заявок", result.status == db.BANK_FAILED,
          result.status)
    check("владельцу сказали, что не разобрал",
          "не разобрал" in bot.last(), bot.last()[:60])
    check("заявка по-прежнему ждёт",
          (await db.get_deposit(conn, open_one.id)).status == db.DEP_PENDING)

    # ---- гонка: заявку закрыли раньше нас
    await db.resolve_deposit(conn, first.id, approved=True, admin_id=777)
    balance = (await db.get_user(conn, CLIENT)).balance
    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=2005,
        text=sample(kod="Kod 77005", otpr="Otpravitel 9991110005555"))
    check("закрытую заявку второй раз не оплачиваем",
          result.status in (db.BANK_UNKNOWN, db.BANK_AMBIGUOUS), result.status)
    check("и денег не прибавилось",
          (await db.get_user(conn, CLIENT)).balance == balance)


async def sources() -> None:
    """Слушаем только свой источник — чужие сообщения не трогаем."""
    from app.userbot import runner

    class Msg:
        def __init__(self, sender_id=0, username=""):
            self.sender_id = sender_id
            self.sender = type("S", (), {"username": username})()

    check("свой бот по юзернейму опознан",
          runner._from_source(Msg(username="bank_bot"), 0, "bank_bot"))
    check("регистр юзернейма не важен",
          runner._from_source(Msg(username="Bank_Bot"), 0, "bank_bot"))
    check("свой бот по id опознан",
          runner._from_source(Msg(sender_id=12345), 12345, "12345"))
    check("чужой бот не принят",
          not runner._from_source(Msg(username="other_bot"), 0, "bank_bot"))
    check("чужой id не принят",
          not runner._from_source(Msg(sender_id=999), 12345, "12345"))
    check("без отправителя не принят",
          not runner._from_source(Msg(), 12345, "bank_bot"))


async def worker_loop(bot) -> None:
    """Очередь: строго по одному, и одно кривое сообщение не валит поток."""
    from app.userbot import runner

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(runner._drain(queue, bot))
    try:
        # первое — кривое, второе — нормальное. Второе должно пройти.
        queue.put_nowait((3001, None))            # None вместо текста
        queue.put_nowait((3002, sample(zach="Zachislenie 55.00 TJS",
                                       summa="Summa 55.00 TJS",
                                       kod="Kod 88001")))
        await asyncio.wait_for(queue.join(), timeout=10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    check("очередь пережила кривое сообщение", task.cancelled() or task.done())
    conn = await db.connect()
    try:
        saved = {row.message_id: row for row in
                 await db.list_bank_payments(conn, limit=50)}
    finally:
        await conn.close()
    check("кривое сообщение записано, но не оплачено",
          3001 not in saved or saved[3001].status == db.BANK_FAILED,
          str(saved.get(3001)))
    check("следующее за ним обработано",
          3002 in saved and saved[3002].amount == 5500,
          str(saved.get(3002)))


async def reconnect(bot) -> None:
    """Обрыв связи не ошибка, а вот пять обрывов подряд — уже повод."""
    import app.userbot.runner as runner

    # Подменяем Telegram: клиент, который всегда отказывается подключаться.
    class DeadClient:
        def __init__(self, *a, **kw):
            pass

        def on(self, _event):
            return lambda fn: fn

        async def start(self):
            raise ConnectionError("сеанс завершён")

        async def disconnect(self):
            return None

    import types
    fake = types.ModuleType("telethon")
    fake.TelegramClient = DeadClient
    fake.events = types.SimpleNamespace(NewMessage=lambda **kw: None)
    real = sys.modules.get("telethon")
    sys.modules["telethon"] = fake

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)
        if len(slept) > runner.ALERT_AFTER + 1:
            raise asyncio.CancelledError

    real_sleep = asyncio.sleep
    asyncio.sleep = no_sleep
    settings_ready = None
    try:
        from app.config import settings
        settings_ready = (settings.tg_api_id, settings.tg_api_hash,
                          settings.bank_bot)
        settings.tg_api_id, settings.tg_api_hash = 1, "x" * 32
        settings.bank_bot = "bank_test_bot"
        bot.clear()
        with contextlib.suppress(asyncio.CancelledError):
            await runner.run(bot)
    finally:
        asyncio.sleep = real_sleep
        if real is not None:
            sys.modules["telethon"] = real
        else:
            sys.modules.pop("telethon", None)
        if settings_ready:
            from app.config import settings as back
            back.tg_api_id, back.tg_api_hash, back.bank_bot = settings_ready

    check("после обрыва пробует снова", len(slept) > 1, str(slept[:4]))
    check("пауза растёт, а не долбит Telegram",
          slept[:3] == sorted(slept[:3]) and slept[0] == runner.RETRY_MIN,
          str(slept[:4]))
    check("пауза не растёт бесконечно",
          all(value <= runner.RETRY_MAX for value in slept), str(slept))
    warned = [text for _, text in bot.sent if "не может подключиться" in text]
    check("после пяти неудач владельца зовут", len(warned) >= 1,
          str(len(warned)))
    check("но зовут один раз, а не каждые пять секунд", len(warned) <= 2,
          str(len(warned)))
    check("в письме сказано, что делать",
          warned and "userbot login" in warned[0], warned[0][-90:] if warned else "")


async def unique_kopeck(conn, bot) -> None:
    """Главный сценарий: копейки делают платёж узнаваемым.

    Клиент просит 10 сомони, платит 10.04 — и такой суммы прямо сейчас
    не ждёт больше никто. Тогда уведомление банка опознаётся одной
    цифрой: ни чека, ни подтверждения владельцем.
    """
    from app.handlers import deposit as dep_h
    from app.states import Deposit

    class State:
        def __init__(self):
            self.data, self.name = {}, None

        async def set_state(self, value):
            self.name = getattr(value, "state", value)

        async def update_data(self, **kw):
            self.data.update(kw)

        async def get_data(self):
            return dict(self.data)

        async def get_state(self):
            return self.name

        async def clear(self):
            self.data.clear()
            self.name = None

    class Msg:
        def __init__(self, text=None, uid=CLIENT, message_id=700):
            self.text = text
            self.message_id = message_id
            self.from_user = type("U", (), {"id": uid, "username": "client",
                                            "first_name": "Клиент"})()
            self.chat = type("C", (), {"id": uid})()
            self.photo = None
            self.document = None
            self.replies = []

        async def answer(self, text, **kw):
            self.replies.append(text)
            answer = Msg(uid=self.from_user.id, message_id=self.message_id + 1)
            return answer

        async def copy_to(self, *a, **kw):
            return None

        @property
        def last(self):
            return self.replies[-1] if self.replies else ""

    class Edits(FakeBot):
        """Бот, который помнит правки чужих сообщений."""

        def __init__(self):
            super().__init__()
            self.edits = []

        async def edit_message_text(self, chat_id, message_id, text, **kw):
            self.edits.append((chat_id, message_id, text))

    watcher = Edits()
    await runtime.set_value(conn, "min_deposit_diram", "100")
    await runtime.set_value(conn, "pay_card_number", "9999000011112222")

    # ---- трое просят одну и ту же сумму
    amounts = []
    for index in range(3):
        state = State()
        await state.set_state(Deposit.amount)
        ask = Msg("10", uid=CLIENT + index)
        await db.upsert_user(conn, CLIENT + index, f"c{index}", "К")
        await dep_h.on_amount(ask, state, conn)
        amounts.append(state.data["amount"])

    check("троим выдали разные суммы", len(set(amounts)) == 3, str(amounts))
    check("все начинаются с запрошенных 10",
          all(1000 < value <= 1010 for value in amounts), str(amounts))
    check("клиенту объяснили, зачем копейки",
          "Тангаҳоро нагиред" in ask.last, ask.last[-200:])

    saved = await db.get_deposit(conn, state.data["deposit_id"])
    check("бот запомнил, где показаны реквизиты",
          saved.pay_chat and saved.pay_msg, f"{saved.pay_chat}/{saved.pay_msg}")

    # ---- платит второй: попадает ровно в свою заявку
    payer = CLIENT + 1
    target = await db.pending_deposits_for(conn, amounts[1])
    check("на эту сумму ждёт ровно один", len(target) == 1, str(len(target)))
    before = (await db.get_user(conn, payer)).balance
    watcher.clear()
    watcher.edits.clear()

    kopecks = amounts[1] % 100
    result = await processor.handle(
        conn, watcher, source=SOURCE, message_id=6001,
        text=sample(zach=f"Zachislenie 10.{kopecks:02d} TJS",
                    summa=f"Summa 10.{kopecks:02d} TJS",
                    kod="Kod 66001", otpr="Otpravitel 9993330001111"))

    check("платёж зачислен без чека", result.confirmed, result.status)
    check("деньги ушли нужному клиенту", result.deposit_id == target[0].id,
          str(result.deposit_id))
    check("сумма зачислена полностью",
          (await db.get_user(conn, payer)).balance == before + amounts[1],
          str((await db.get_user(conn, payer)).balance))
    others_open = [
        (await db.get_deposit(conn, d.id)).status
        for d in await db.pending_deposits_for(conn, amounts[0])
    ]
    check("чужие заявки не тронуты",
          others_open and all(value == db.DEP_PENDING for value in others_open),
          str(others_open))

    check("реквизиты убраны с экрана", bool(watcher.edits), str(watcher.edits))
    screen = watcher.edits[0][2] if watcher.edits else ""
    check("на их месте — ответ об оплате",
          "Пардохт гирифта шуд" in screen, screen[:80])
    check("и новый баланс", "Баланси ҳозира" in screen, screen[:160])
    check("номера карты на экране больше нет",
          "9999000011112222" not in screen, screen[:160])

    # ---- копейка освободилась: её можно выдать снова
    state = State()
    await state.set_state(Deposit.amount)
    again = Msg("10", uid=CLIENT + 5)
    await db.upsert_user(conn, CLIENT + 5, "c5", "К")
    await dep_h.on_amount(again, state, conn)
    check("освободившаяся копейка снова в ходу",
          state.data["amount"] == amounts[1], 
          f"{state.data['amount']} против {amounts[1]}")

    # ---- заплатил круглую сумму вместо названной
    await db.resolve_deposit(conn, state.data["deposit_id"], approved=False,
                             admin_id=1)
    for extra in await db.pending_deposits_for(conn, amounts[0]):
        await db.resolve_deposit(conn, extra.id, approved=False, admin_id=1)
    for extra in await db.pending_deposits_for(conn, amounts[2]):
        await db.resolve_deposit(conn, extra.id, approved=False, admin_id=1)

    state = State()
    await state.set_state(Deposit.amount)
    lone = Msg("20", uid=CLIENT + 6)
    await db.upsert_user(conn, CLIENT + 6, "c6", "К")
    await dep_h.on_amount(lone, state, conn)
    asked = state.data["amount"]
    before = (await db.get_user(conn, CLIENT + 6)).balance

    result = await processor.handle(
        conn, watcher, source=SOURCE, message_id=6002,
        text=sample(zach="Zachislenie 20.00 TJS", summa="Summa 20.00 TJS",
                    kod="Kod 66002", otpr="Otpravitel 9993330002222"))
    check("круглая сумма закрыла ближайшую заявку", result.confirmed,
          result.status)
    check("зачислено ровно столько, сколько пришло",
          (await db.get_user(conn, CLIENT + 6)).balance == before + 2000,
          str((await db.get_user(conn, CLIENT + 6)).balance))
    check("а не столько, сколько просили", asked != 2000, str(asked))

    # ---- рядом две заявки — гадать нельзя
    first = await db.create_deposit(conn, user_id=CLIENT + 7, amount=5001,
                                    method="card", receipt_file_id="")
    second = await db.create_deposit(conn, user_id=CLIENT + 8, amount=5002,
                                     method="card", receipt_file_id="")
    await db.upsert_user(conn, CLIENT + 7, "c7", "К")
    await db.upsert_user(conn, CLIENT + 8, "c8", "К")
    result = await processor.handle(
        conn, watcher, source=SOURCE, message_id=6003,
        text=sample(zach="Zachislenie 50.00 TJS", summa="Summa 50.00 TJS",
                    kod="Kod 66003", otpr="Otpravitel 9993330003333"))
    check("две заявки рядом — решает владелец",
          result.status == db.BANK_AMBIGUOUS, result.status)
    check("никому ничего не зачислено",
          (await db.get_deposit(conn, first.id)).status == db.DEP_PENDING
          and (await db.get_deposit(conn, second.id)).status == db.DEP_PENDING)


async def known_sender(conn, bot) -> None:
    """Знакомый плательщик разбирает спор одинаковых сумм.

    С уникальными копейками такой спор почти невозможен, но старые
    заявки — заведённые до этой затеи — суммы не метили. Для них
    остаётся вторая примета: счёт, с которого пришли деньги.
    """
    REGULAR = 930
    await db.upsert_user(conn, REGULAR, "regular", "Постоянный")

    # ---- первый платёж: сумма уникальна, чека не просим
    await db.create_deposit(
        conn, user_id=REGULAR, amount=1507, method="card", receipt_file_id="")
    before = (await db.get_user(conn, REGULAR)).balance
    bot.clear()

    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=5001,
        text=sample(zach="Zachislenie 15.07 TJS", summa="Summa 15.07 TJS",
                    kod="Kod 55001", otpr="Otpravitel 9990001112233"))
    check("первый платёж проходит без чека", result.confirmed, result.status)
    check("деньги зачислены",
          (await db.get_user(conn, REGULAR)).balance == before + 1507,
          str((await db.get_user(conn, REGULAR)).balance))

    bound = await db.senders_of(conn, REGULAR)
    check("счёт плательщика закреплён за клиентом", len(bound) == 1, str(bound))
    check("закреплён именно тот счёт",
          bound[0].sender == "999000***2233", bound[0].sender)

    # ---- две старые заявки на одну и ту же сумму
    mine = await db.create_deposit(
        conn, user_id=REGULAR, amount=3300, method="card", receipt_file_id="")
    stranger = await db.create_deposit(
        conn, user_id=OTHER, amount=3300, method="card", receipt_file_id="")
    others = (await db.get_user(conn, OTHER)).balance

    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=5003,
        text=sample(zach="Zachislenie 33.00 TJS", summa="Summa 33.00 TJS",
                    kod="Kod 55003", otpr="Otpravitel 9990001112233"))
    check("две заявки на одну сумму — но плательщик знаком",
          result.confirmed, result.status)
    check("деньги ушли своему", result.deposit_id == mine.id,
          str(result.deposit_id))
    check("чужая заявка не тронута",
          (await db.get_user(conn, OTHER)).balance == others
          and (await db.get_deposit(conn, stranger.id)).status == db.DEP_PENDING)
    await db.resolve_deposit(conn, stranger.id, approved=False, admin_id=1)

    # ---- знакомый счёт, но заявки только у чужих — не отдаём
    only_other = await db.create_deposit(
        conn, user_id=OTHER, amount=4401, method="card", receipt_file_id="")
    second_other = await db.create_deposit(
        conn, user_id=OTHER + 1, amount=4402, method="card", receipt_file_id="")
    await db.upsert_user(conn, OTHER + 1, "third", "Третий")
    others = (await db.get_user(conn, OTHER)).balance

    result = await processor.handle(
        conn, bot, source=SOURCE, message_id=5004,
        text=sample(zach="Zachislenie 44.00 TJS", summa="Summa 44.00 TJS",
                    kod="Kod 55004", otpr="Otpravitel 9990001112233"))
    check("чужие заявки знакомому не отдаём",
          result.status == db.BANK_AMBIGUOUS, result.status)
    check("и деньги никому не ушли",
          (await db.get_user(conn, OTHER)).balance == others
          and (await db.get_deposit(conn, only_other.id)).status == db.DEP_PENDING)
    await db.resolve_deposit(conn, only_other.id, approved=False, admin_id=1)
    await db.resolve_deposit(conn, second_other.id, approved=False, admin_id=1)



# ────────────────────────────────────── оплата из России


async def from_russia(conn, bot) -> None:
    """Деньги пришли раньше клиента — заявка должна найти их сама.

    При переводе на таджикскую карту сумму назначаем мы, и заявка ждёт
    денег. Из России наоборот: человек сначала переводит, банк пересчитал
    рубли в сомони, и только потом клиент открывает бота с чеком на
    руках. Уведомление к этому моменту уже лежит непривязанным.
    """
    await db.upsert_user(conn, CLIENT, "client", "Клиент")
    bot.clear()

    # Банк написал раньше, чем клиент пришёл: заявки на эту сумму нет.
    arrived = await processor.handle(
        conn, bot, source=SOURCE, message_id=8001,
        text=sample(summa="Summa 107.43 TJS", zach="Zachislenie 107.43 TJS",
                    kod="Kod 20000000001"))
    check("без заявки деньги не зачисляются",
          arrived.status == db.BANK_UNKNOWN, arrived.status)

    free = await db.unclaimed_bank_payments(conn, 10743, hours=6)
    check("непривязанный платёж находится по сумме", len(free) == 1,
          str(len(free)))
    check("а по чужой сумме — нет",
          not await db.unclaimed_bank_payments(conn, 10744, hours=6))

    # Клиент пришёл и назвал сумму из чека.
    before = (await db.get_user(conn, CLIENT)).balance
    deposit = await db.create_deposit(
        conn, user_id=CLIENT, amount=10743,
        method=texts.RU_METHOD, receipt_file_id="",
    )
    bot.clear()
    done = await processor.claim_for_deposit(conn, bot, deposit)
    check("пришедшие раньше деньги находятся по сумме из чека",
          done is not None and done.confirmed, str(done and done.status))
    check("зачислено ровно столько, сколько пришло в банк",
          (await db.get_user(conn, CLIENT)).balance == before + 10743,
          str((await db.get_user(conn, CLIENT)).balance - before))
    check("платёж больше не свободен",
          not await db.unclaimed_bank_payments(conn, 10743, hours=6))
    check("клиенту сообщили", any(chat == CLIENT for chat, _ in bot.sent),
          str(bot.sent[:1]))

    # Второй раз тот же платёж забрать нельзя.
    twin = await db.create_deposit(
        conn, user_id=CLIENT, amount=10743,
        method=texts.RU_METHOD, receipt_file_id="",
    )
    balance = (await db.get_user(conn, CLIENT)).balance
    check("дважды одни и те же деньги не зачисляются",
          await processor.claim_for_deposit(conn, bot, twin) is None
          and (await db.get_user(conn, CLIENT)).balance == balance)

    # Две одинаковые суммы висят непривязанными — выбирать наугад нельзя.
    for number, code in ((8002, "20000000002"), (8003, "20000000003")):
        await processor.handle(
            conn, bot, source=SOURCE, message_id=number,
            text=sample(summa="Summa 55.55 TJS", zach="Zachislenie 55.55 TJS",
                        kod=f"Kod {code}"))
    pair = await db.create_deposit(
        conn, user_id=CLIENT, amount=5555,
        method=texts.RU_METHOD, receipt_file_id="",
    )
    steady = (await db.get_user(conn, CLIENT)).balance
    check("две одинаковые суммы — ничего не выбираем",
          await processor.claim_for_deposit(conn, bot, pair) is None
          and (await db.get_user(conn, CLIENT)).balance == steady)

    # Экран, на котором спрашивали сумму, должен смениться ответом.
    # Иначе под ним остаётся красная «Отмена»: человек возвращается к
    # ней и отменяет оплату, которая уже прошла.
    await processor.handle(
        conn, bot, source=SOURCE, message_id=8004,
        text=sample(summa="Summa 77.77 TJS", zach="Zachislenie 77.77 TJS",
                    kod="Kod 20000000004"))
    screened = await db.create_deposit(
        conn, user_id=CLIENT, amount=7777,
        method=texts.RU_METHOD, receipt_file_id="",
    )
    await db.set_deposit_screen(conn, screened.id, CLIENT, 4242)
    bot.clear()
    ready = await processor.claim_for_deposit(
        conn, bot, await db.get_deposit(conn, screened.id))
    check("экран с вопросом о сумме заменяется ответом",
          ready is not None and any(
              chat == CLIENT and msg == 4242 for chat, msg, _ in bot.edited),
          str(bot.edited[:2]))
    check("и на нём написано про зачисление",
          any("Пардохт гирифта шуд" in text for _, _, text in bot.edited),
          str([text[:30] for _, _, text in bot.edited]))


async def main() -> None:
    for sfx in ("", "-wal", "-shm"):
        Path(str(db.settings.db_file) + sfx).unlink(missing_ok=True)
    conn = await db.connect()
    bot = FakeBot()
    try:
        await db.init(conn)
        await runtime.load(conn)
        parsing()
        masking()
        sberbank()
        log_filter()
        await matching(conn, bot)
        await sources()
        await unique_kopeck(conn, bot)
        await known_sender(conn, bot)
        await from_russia(conn, bot)
    finally:
        await conn.close()
    await worker_loop(bot)
    await reconnect(bot)

    print(f"\n{'=' * 52}\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
    if FAIL:
        print("ПРОВАЛЫ:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
