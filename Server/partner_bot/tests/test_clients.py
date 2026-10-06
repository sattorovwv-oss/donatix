"""Работа с клиентами из панели: поиск, начисление, списание, блокировка."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, reports, runtime
from app.handlers import panel

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=111):
        self.id, self.username, self.first_name = uid, "admin", "Админ"


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.from_user = FakeUser()
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
    def __init__(self, data):
        self.data = data
        self.from_user = FakeUser()
        self.message = FakeMessage()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        if text:
            self.alerts.append(text)

    @property
    def last(self):
        return self.message.last


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
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


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


async def transfer(conn) -> None:
    """Перенос балансов со старого бота."""
    from app.services import importer

    # ---- разбор: список выгружают кто во что горазд
    rows, skipped = importer.parse_balances(
        "123456789 100\n"
        "987654321 - 45.50\n"
        "id: 111222333, balance: 12,30\n"
        "5  444555666  250 с.\n"
        "Итого: 500\n"
        "\n"
        "123456789 999\n"
    )
    found = {row["id"]: row["amount"] for row in rows}
    check("простая строка разобрана", found.get(123456789) == 99900,
          str(found.get(123456789)))
    check("тире не мешает", found.get(987654321) == 4550, str(found.get(987654321)))
    check("слова и запятая не мешают", found.get(111222333) == 1230,
          str(found.get(111222333)))
    check("номер строки за ID не принят", found.get(444555666) == 25000,
          str(found.get(444555666)))
    check("повтор не удваивает, берётся последняя строка",
          found.get(123456789) == 99900)
    check("шапка и итоги не считаются ошибкой", skipped == [], str(skipped))

    check("совсем пустой список — пусто",
          importer.parse_balances("") == ([], []))
    check("строка без суммы не проходит",
          importer.parse_balances("123456789")[0] == [])

    # ---- настоящая выгрузка: три числа в конце строки
    table = (
        "=== КОРБАРОН · 1096 ===\n"
        "Ҳамагӣ дар ҳамёнҳо : 617.96 Tjs\n"
        "Бо пул дар ҳамён   : 168\n"
        "──────────────────────────────────────────\n"
        "TELEGRAM ID   USERNAME    НОМ      ТЕЛЕФОН       ҲАМЁН   ХАРҶ  ФРМ\n"
        "──────────────────────────────────────────\n"
        "1000000001    —           Клиент1   —          70.00    0.00    0\n"
        "1000000002    @client_a   Клиент2   992000000001   60.50   58.50    8\n"
        "1000000003    —           VIP     992000000002    6.91  192.59    7\n"
        "1000000004    —           40205       —           1.00   36.00    4\n"
        "1000000005    —           Клиент4   —           0.00    0.00    0\n"
        "1000000006    @client_b   Клиент3   992000000003    0.90 1087.10    5\n"
    )
    rows, skipped = importer.parse_balances(table)
    money = {row["id"]: row["amount"] for row in rows}
    check("баланс взят, а не число заказов",
          money.get(1000000001) == 7000, str(money.get(1000000001)))
    check("потраченное за баланс не принято",
          money.get(1000000002) == 6050, str(money.get(1000000002)))
    check("копейки на месте", money.get(1000000003) == 691,
          str(money.get(1000000003)))
    check("ник из одних цифр не спутан с суммой",
          money.get(1000000004) == 100, str(money.get(1000000004)))
    check("длинный ник-число тоже не спутан",
          money.get(1000000005) == 0, str(money.get(1000000005)))
    check("большая трата не подменила баланс",
          money.get(1000000006) == 90, str(money.get(1000000006)))
    check("телефон за баланс не принят",
          all(amount < 100_000 for amount in money.values()), str(money))
    check("шапка выгрузки в ошибки не попала", skipped == [], str(skipped))
    check("разобраны все строки с людьми", len(rows) == 6, str(len(rows)))

    names = {row["id"]: row["username"] for row in rows}
    check("юзернейм подхвачен", names.get(1000000002) == "client_a",
          str(names.get(1000000002)))
    check("без юзернейма поле пустое", names.get(1000000001) == "",
          str(names.get(1000000001)))

    rich = importer.with_money(rows)
    check("нулевые отделены от денежных", len(rich) == 5, str(len(rich)))
    check("в предпросмотре сначала крупные",
          importer.preview(rows)[0]["id"] == 1000000001,
          str(importer.preview(rows)[0]))

    # ---- предпросмотр ничего не применяет
    state = FakeState()
    call = FakeCallback("pn:transfer")
    await panel.cb_transfer(call, state)
    check("бот просит список", "Перенос балансов" in call.last, call.last[:60])
    check("сказано, что сразу не применит",
          "ничего не применю" in call.last, call.last)

    message = FakeMessage("777000111 25\n777000222 10.50")
    await panel.on_transfer_list(message, state)
    check("показан предпросмотр", "Проверьте перед записью" in message.last,
          message.last[:60])
    check("видно количество", "Всего строк: <b>2</b>" in message.last,
          message.last)
    check("видна общая сумма", "35.50" in message.last, message.last)
    check("до нажатия деньги не начислены",
          await db.get_user(conn, 777000111) is None)

    # ---- а теперь применяем
    call = FakeCallback("pn:transfer_go:all")
    await panel.cb_transfer_go(call, state, conn)
    first = await db.get_user(conn, 777000111)
    second = await db.get_user(conn, 777000222)
    check("клиент заведён", first is not None)
    check("баланс записан", first and first.balance == 2500, str(first.balance))
    check("копейки не потерялись", second and second.balance == 1050,
          str(second.balance))
    check("в отчёте видно, сколько новых",
          "Новых клиентов: <b>2</b>" in call.last, call.last[:200])

    # ---- повторный перенос не удваивает деньги
    state = FakeState()
    message = FakeMessage("777000111 25")
    await panel.on_transfer_list(message, state)
    await panel.cb_transfer_go(FakeCallback("pn:transfer_go:all"), state, conn)
    again = await db.get_user(conn, 777000111)
    check("повторный перенос не удваивает баланс", again.balance == 2500,
          str(again.balance))

    # ---- то же самое, но файлом
    from app.services import sheets

    table = [
        ["TELEGRAM ID", "USERNAME", "НОМ", "ТЕЛЕФОН", "ҲАМЁН", "ХАРҶ", "ФРМ"],
        ["1000000001", "", "Клиент1", "", "70.0", "0.0", "0"],
        ["1000000002", "@client_a", "Клиент2", "992000000001", "60.5", "58.5", "8"],
        ["1000000004", "", "40205", "", "1.0", "36.0", "4"],
        ["", "", "Ҳамагӣ", "", "131.5", "94.5", "12"],
    ]
    rows, skipped = importer.parse_rows(table)
    money = {r["id"]: r["amount"] for r in rows}
    check("колонка остатка найдена по заголовку",
          money.get(1000000001) == 7000, str(money.get(1000000001)))
    check("колонка «потрачено» не взята",
          money.get(1000000002) == 6050, str(money.get(1000000002)))
    check("ник из цифр в колонке не мешает",
          money.get(1000000004) == 100, str(money.get(1000000004)))
    check("строка итогов без ID пропущена", len(rows) == 3, str(len(rows)))
    check("юзернейм без собачки", rows[1]["username"] == "client_a",
          rows[1]["username"])
    check("файл разобран без ошибок", skipped == [], str(skipped))

    # заголовок в другом порядке — всё равно находим
    other = [
        ["balance", "chat id", "note"],
        ["12.50", "777123456", "—"],
    ]
    rows, _ = importer.parse_rows(other)
    check("порядок колонок не важен",
          rows and rows[0] == {"id": 777123456, "amount": 1250, "username": ""},
          str(rows))

    # без заголовка — разбираем как обычный список
    plain = [["888123456", "25"], ["888123457", "10.50"]]
    rows, _ = importer.parse_rows(plain)
    check("без заголовка файл тоже читается", len(rows) == 2, str(rows))

    # ---- сам читатель файлов
    csv_bytes = "id;balance\n777999111;33.25\n777999222;0\n".encode()
    got = sheets.read(csv_bytes, "spisok.csv")
    check("csv прочитан", len(got) == 3, str(got))
    check("разделитель «;» понят", got[1] == ["777999111", "33.25"], str(got[1]))
    rows, _ = importer.parse_rows(got)
    check("и разобран по заголовку",
          {r["id"]: r["amount"] for r in rows} == {777999111: 3325, 777999222: 0},
          str(rows))

    check("cp1251 не роняет чтение",
          sheets.read("id;balance\n777999111;5".encode("cp1251"))[1][0]
          == "777999111")

    broken = False
    try:
        sheets.read(b"PK\x03\x04" + "мусор".encode(), "x.xlsx")
    except sheets.SheetError:
        broken = True
    check("битый Excel назван по-человечески", broken)

    huge = False
    try:
        sheets.read(b"x" * (sheets.MAX_BYTES + 1))
    except sheets.SheetError:
        huge = True
    check("слишком большой файл отклонён", huge)

    # ---- можно взять только тех, у кого деньги
    state = FakeState()
    message = FakeMessage("777000333 0\n777000444 15")
    await panel.on_transfer_list(message, state)
    check("нули посчитаны отдельно", "С нулём: <b>1</b>" in message.last,
          message.last[:300])
    await panel.cb_transfer_go(FakeCallback("pn:transfer_go:rich"), state, conn)
    check("клиент с деньгами заведён",
          (await db.get_user(conn, 777000444)) is not None)
    check("пустого заводить не стали",
          await db.get_user(conn, 777000333) is None)

    # ---- /start не затирает перенесённый баланс
    await db.upsert_user(conn, 777000111, "vasya", "Вася")
    after_start = await db.get_user(conn, 777000111)
    check("после «старта» баланс на месте", after_start.balance == 2500,
          str(after_start.balance))
    check("и имя записалось", after_start.username == "vasya",
          str(after_start.username))

    # ---- мусор вместо списка
    state = FakeState()
    message = FakeMessage("просто текст без цифр")
    await panel.on_transfer_list(message, state)
    check("мусор не принимается", "Ни одной записи не понял" in message.last,
          message.last[:60])

    # ---- нечего применять
    call = FakeCallback("pn:transfer_go:all")
    await panel.cb_transfer_go(call, FakeState(), conn)
    check("пустое применение не падает",
          any("потерялся" in a for a in call.alerts), str(call.alerts))


async def run(conn) -> None:
    await transfer(conn)
    bot = FakeBot()
    await db.upsert_user(conn, 900, "klient", "Клиент")
    await db.credit(conn, 900, 5000, as_deposit=True)

    # ------------------------------------------------------------- поиск
    check("клиент находится по ID",
          (await db.find_user(conn, "900")) is not None)
    check("клиент находится по юзернейму",
          (await db.find_user(conn, "@klient")).id == 900)
    check("юзернейм без собаки тоже ищется",
          (await db.find_user(conn, "klient")).id == 900)
    check("регистр не мешает", (await db.find_user(conn, "@KLIENT")).id == 900)
    check("несуществующий не находится",
          await db.find_user(conn, "@nobody") is None)

    state = FakeState()
    msg = FakeMessage("@nobody")
    await panel.on_user_search(msg, state, conn)
    check("на ненайденного объясняем причину", "напишет боту" in msg.last)

    msg = FakeMessage("900")
    await panel.on_user_search(msg, state, conn)
    check("карточка клиента показывается",
          "@klient" in msg.last and "50.00" in msg.last, msg.last[:90])

    # --------------------------------------------------------- начисление
    call = FakeCallback("pn:give:900")
    await panel.cb_adjust_start(call, state, conn)
    check("экран начисления открывается", "Начислить" in call.last)
    check("запомнили, кому начисляем", state.data["adjust_user"] == 900)

    msg = FakeMessage("25 бонус за отзыв")
    await panel.on_adjust_amount(msg, state, conn, bot)
    user = await db.get_user(conn, 900)
    check("баланс вырос", user.balance == 7500, str(user.balance))
    check("отчёт о начислении показан", "Начислено" in msg.last and "25.00" in msg.last)
    check("клиенту ушло уведомление",
          any("начислено" in t.lower() for _, t in bot.sent), str(bot.sent))
    check("причина дошла до клиента",
          any("бонус за отзыв" in t for _, t in bot.sent))

    ops = await db.list_adjustments(conn, user_id=900)
    check("правка записана", len(ops) == 1 and ops[0].amount == 2500, str(ops))
    check("записан админ, который её сделал", ops[0].admin_id == 111)
    check("записана причина", ops[0].reason == "бонус за отзыв")

    # ---------------------------------------------------------- списание
    call = FakeCallback("pn:take:900")
    await panel.cb_adjust_start(call, state, conn)
    msg = FakeMessage("10")
    await panel.on_adjust_amount(msg, state, conn, bot)
    user = await db.get_user(conn, 900)
    check("баланс уменьшился", user.balance == 6500, str(user.balance))
    check("списание записано со знаком минус",
          (await db.list_adjustments(conn, user_id=900))[0].amount == -1000)

    # ------------------------------- в минус баланс не уводим
    call = FakeCallback("pn:take:900")
    await panel.cb_adjust_start(call, state, conn)
    msg = FakeMessage("999")
    await panel.on_adjust_amount(msg, state, conn, bot)
    user = await db.get_user(conn, 900)
    check("списать больше, чем есть, нельзя",
          user.balance == 6500 and "Не хватает" in msg.last, str(user.balance))
    check("лишней записи не появилось",
          len(await db.list_adjustments(conn, user_id=900)) == 2)

    # ---------------------------------------------- неверный ввод
    msg = FakeMessage("много")
    await panel.on_adjust_amount(msg, state, conn, bot)
    check("нечисловая сумма отклоняется", "числом" in msg.last)

    msg = FakeMessage("0")
    await panel.on_adjust_amount(msg, state, conn, bot)
    check("ноль отклоняется", "числом" in msg.last)

    # ------------------------------------------------------- блокировка
    call = FakeCallback("pn:ban:900")
    await panel.cb_ban_toggle(call, conn)
    check("клиент блокируется", (await db.get_user(conn, 900)).is_banned == 1)
    check("в карточке видно блокировку", "аблокирован" in call.last)

    call = FakeCallback("pn:ban:900")
    await panel.cb_ban_toggle(call, conn)
    check("клиент разблокируется", (await db.get_user(conn, 900)).is_banned == 0)

    # ------------------------------- правки видны в отчёте
    today = reports.local_today()
    data = await db.report(conn, *reports.bounds(today, today))
    check("начисления попали в отчёт", data["adjust_added"] == 2500, str(data))
    check("списания попали в отчёт", data["adjust_taken"] == 1000, str(data))
    check("в тексте отчёта есть раздел правок",
          "Правки вручную" in panel.format_report("Тест", data, []))

    empty = await db.report(conn, "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")
    check("без правок раздел не показывается",
          "Правки вручную" not in panel.format_report("Пусто", empty, []))

    # --------------------------- история правок видна в карточке
    stats = await db.user_order_stats(conn, 900)
    card = panel.user_card(await db.get_user(conn, 900), stats,
                           await db.list_adjustments(conn, user_id=900))
    check("в карточке видна история правок", "Правки баланса" in card)
    check("в истории видны и плюс, и минус",
          "+25.00" in card and "−10.00" in card, card[-140:])

    # ------------------- запись на каждое нажатие: её быть не должно
    # Проверка пользователя срабатывает на каждое сообщение и каждую
    # кнопку. Если она пишет в базу вхолостую, при тысяче человек
    # очередь писателей встаёт колом и остальные получают
    # «database is locked» — на этом бот однажды уже упал.
    await db.upsert_user(conn, 950, "stalyi", "Стальной")
    before = conn.total_changes
    await db.upsert_user(conn, 950, "stalyi", "Стальной")
    check("повторный вход ничего не пишет",
          conn.total_changes == before,
          f"записей: {conn.total_changes - before}")

    await db.upsert_user(conn, 950, "novyi_nik", "Стальной")
    check("смена юзернейма сохраняется",
          (await db.get_user(conn, 950)).username == "novyi_nik")

    await db.upsert_user(conn, 950, "novyi_nik", "Новое имя")
    check("смена имени сохраняется",
          (await db.get_user(conn, 950)).first_name == "Новое имя")

    check("возврат «новый» не сбился",
          await db.upsert_user(conn, 951, "svezhiy", "Свежий") is True
          and await db.upsert_user(conn, 951, "svezhiy", "Свежий") is False)


asyncio.run(main())
