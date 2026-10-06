"""Оформление: настраиваемые значки, разметка Telegram, история с возвратами."""
from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401

from app import db, emoji, keyboards, runtime, texts
from app.handlers import profile as prof_h

PASS, FAIL = [], []

# Что Telegram понимает в режиме HTML.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code",
    "pre", "blockquote", "span", "tg-spoiler", "tg-emoji",
}
TAG_RE = re.compile(r"</?([a-zA-Z-]+)")


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class FakeUser:
    def __init__(self, uid=321, username="buyer", first_name="Покупатель"):
        self.id, self.username, self.first_name = uid, username, first_name


class FakeMessage:
    def __init__(self, user=None):
        self.from_user = user or FakeUser()
        self.replies: list[str] = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.replies.append(text)
        return self

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""


class FakeCallback:
    def __init__(self, data, user=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.message = FakeMessage(self.from_user)

    async def answer(self, text="", **kw):
        return None

    @property
    def last(self):
        return self.message.last


def all_templates() -> dict[str, str]:
    return {name: getattr(texts, name) for name in texts._RAW}


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
    templates = all_templates()

    # ------------------------------------------- разметка валидна для Telegram
    bad_tags = {}
    for name, text in templates.items():
        tags = {t.lower() for t in TAG_RE.findall(text)} - ALLOWED_TAGS
        if tags:
            bad_tags[name] = tags
    check("во всех текстах только теги, понятные Telegram", not bad_tags, str(bad_tags))

    unbalanced = []
    for name, text in templates.items():
        for tag in ("b", "i", "code", "blockquote"):
            if text.count(f"<{tag}>") + text.count(f"<{tag} ") != text.count(f"</{tag}>"):
                unbalanced.append(f"{name}:{tag}")
    check("теги парные, ничего не забыто", not unbalanced, str(unbalanced))

    leftovers = {n: t for n, t in templates.items() if "[[" in t or "]]" in t}
    check("незакрытых токенов значков не осталось", not leftovers, str(list(leftovers)))

    quoted = [n for n, t in templates.items() if "<blockquote" in t]
    check("цитаты используются в оформлении", len(quoted) >= 15, f"{len(quoted)} экранов")

    # ------------------------------------------------ значки настраиваются
    check("значков в реестре достаточно", len(emoji.DEFAULTS) >= 25,
          str(len(emoji.DEFAULTS)))
    check("по умолчанию Premium — корона", emoji.em("premium") == "👑")

    await runtime.set_value(conn, "emoji_premium", "💎")
    check("значок меняется", emoji.em("premium") == "💎")
    check("текст сразу подхватывает новый значок",
          "💎" in texts.PREMIUM_ENTRY, texts.PREMIUM_ENTRY[:40])
    labels = [b.text for row in keyboards.main_menu().inline_keyboard for b in row]
    check("кнопка сразу подхватывает новый значок",
          any("💎" in t for t in labels), str(labels))

    runtime._cache.clear()
    await runtime.load(conn)
    check("новый значок пережил перезапуск", emoji.em("premium") == "💎")

    await runtime.reset(conn, "emoji_premium")
    check("сброс возвращает значок по умолчанию", emoji.em("premium") == "👑")

    check("слова значком не считаются", not emoji.is_emoji_like("привет"))
    check("длинная строка значком не считается", not emoji.is_emoji_like("⭐️⭐️⭐️⭐️⭐️"))
    check("символ подходит как значок", emoji.is_emoji_like("•"))

    # ---------------------------------------- история показывает возвраты
    await db.upsert_user(conn, 321, "buyer", "Покупатель")
    await db.credit(conn, 321, 100000)

    done = await db.create_order(conn, user_id=321, product_type="stars",
                                 quantity=100, recipient="friend", price=2000)
    await db.update_order(conn, done.id, status=db.ORDER_DELIVERED)
    lost = await db.create_order(conn, user_id=321, product_type="premium",
                                 quantity=3, recipient="other", price=13000)
    await db.update_order(conn, lost.id, status=db.ORDER_REFUNDED)

    call = FakeCallback("p:history")
    await prof_h.cb_history(call, conn)
    text = call.last

    check("история показывает сводку по заказам",
          "Иҷро шуд: <b>1</b>" in text and "Баргардонида шуд: <b>1</b>" in text,
          text.replace("\n", " ")[:150])
    check("видно, сколько всего потрачено", "20.00" in text)
    check("у возврата написано, что деньги вернулись",
          "ба баланс баргашт" in text, text.replace("\n", " ")[-160:])
    check("у выполненного написано, что списано", "Гирифта шуд:" in text)
    check("получатель показан у каждого заказа",
          "@friend" in text and "@other" in text)
    check("статусы понятны человеку",
          "Иҷро шуд" in text and "Пул баргардонида шуд" in text)

    # Пустая история тоже объясняет, что тут будет
    await db.upsert_user(conn, 322, "new", "Новичок")
    call = FakeCallback("p:history", FakeUser(322, "new", "Новичок"))
    await prof_h.cb_history(call, conn)
    check("пустая история объясняет, что появится",
          "Ҳоло холӣ" in call.last and "<blockquote>" in call.last)

    # ------------------------------------------- премиум-эмодзи
    from app.middlewares.emoji_guard import CustomEmojiGuard, strip_custom

    await runtime.reset(conn, "custom_emoji_on")
    await runtime.set_value(conn, "emoji_id_premium", "5368324170671202286")

    check("пока не проверено — премиум-эмодзи не подставляется",
          emoji.em_html("premium") == emoji.em("premium"), emoji.em_html("premium"))

    await runtime.set_value(conn, "custom_emoji_on", "1")
    html = emoji.em_html("premium")
    check("после включения подставляется тег",
          html.startswith("<tg-emoji emoji-id=\"5368324170671202286\">"), html)
    check("внутри тега остаётся запасной значок",
          emoji.em("premium") in html, html)
    check("в кнопках премиум-эмодзи не появляется",
          "<tg-emoji" not in " ".join(
              b.text for row in keyboards.main_menu().inline_keyboard for b in row))
    check("в текстах премиум-эмодзи появляется",
          "<tg-emoji" in texts.PREMIUM_ENTRY)

    # Присланный премиум-эмодзи распознаётся без ручного ввода ID
    class Ent:
        type = "custom_emoji"
        custom_emoji_id = "999888777"
        offset, length = 0, 2

    class Msg:
        text = "👑 вот такой"
        entities = [Ent()]

    got = emoji.extract_custom(Msg())
    check("ID премиум-эмодзи достаётся из сообщения",
          got == ("999888777", "👑"), str(got))

    class Plain:
        text = "👑"
        entities = []

    check("обычный эмодзи не путается с премиум",
          emoji.extract_custom(Plain()) is None)

    # Страховка: отказ Telegram убирает теги и выключает премиум-эмодзи
    check("страховка вырезает теги, оставляя значки",
          strip_custom('a <tg-emoji emoji-id="1">👑</tg-emoji> b') == "a 👑 b")

    class Method:
        text = 'Привет <tg-emoji emoji-id="1">👑</tg-emoji>'

    method = Method()
    calls = []

    async def make_request(bot, m):
        calls.append(m.text)
        if "<tg-emoji" in m.text:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="Bad Request: custom_emoji is not allowed")
        return "ok"

    result = await CustomEmojiGuard()(make_request, None, method)
    check("после отказа сообщение уходит без премиум-эмодзи",
          result == "ok" and calls[-1] == "Привет 👑", str(calls))
    check("после отказа премиум-эмодзи выключаются",
          not runtime.get_bool("custom_emoji_on"))

    # Чужие ошибки страховка не глотает
    async def other_error(bot, m):
        from aiogram.exceptions import TelegramBadRequest
        raise TelegramBadRequest(method=None, message="Bad Request: chat not found")

    try:
        await CustomEmojiGuard()(other_error, None, Method())
        passed = False
    except Exception as exc:
        passed = "chat not found" in str(exc)
    check("посторонние ошибки не проглатываются", passed)

    await runtime.reset(conn, "emoji_id_premium")

    # ---------------------------------------------- цвета кнопок
    from app import keyboards as kb_mod

    def styles_of(markup):
        return [(b.style, b.text) for row in markup.inline_keyboard for b in row]

    menu = styles_of(kb_mod.main_menu())
    check("главные действия синие",
          any(st == "primary" and "Ситора харидан" in t for st, t in menu), str(menu))
    check("пополнение зелёное",
          any(st == "success" and "Пур кардан" in t for st, t in menu), str(menu))
    check("навигация без цвета",
          any(st is None and "Профил" in t for st, t in menu), str(menu))

    pay = styles_of(kb_mod.confirm())
    check("оплата зелёная", ("success", f"{emoji.em('confirm')} Пардохт") in pay, str(pay))
    check("отмена красная",
          any(st == "danger" for st, _ in pay), str(pay))

    admin = styles_of(kb_mod.admin_deposit(1))
    check("зачислить зелёное, отклонить красное",
          {st for st, _ in admin} == {"success", "danger"}, str(admin))

    allowed = {None, "primary", "success", "danger"}
    every = []
    for markup in (kb_mod.main_menu(), kb_mod.confirm(), kb_mod.confirm_recipient(),
                   kb_mod.premium_menu(), kb_mod.profile(), kb_mod.deposit_methods(),
                   kb_mod.support_menu(True), kb_mod.support_menu(False),
                   kb_mod.ask_recipient(True), kb_mod.cancel(), kb_mod.back(),
                   kb_mod.admin_deposit(1), kb_mod.admin_retry(1),
                   kb_mod.stars_entry(), kb_mod.cancel_order(1)):
        every += styles_of(markup)
    bad = {st for st, _ in every} - allowed
    check("используются только допустимые цвета", not bad, str(bad))

    coloured = sum(1 for st, _ in every if st)
    check("цветом выделено не всё подряд",
          0 < coloured < len(every), f"{coloured} из {len(every)}")

    # ------------------------------------- премиум-значок на кнопке
    await runtime.set_value(conn, "emoji_id_stars", "111222333")
    await runtime.set_value(conn, "custom_emoji_on", "1")
    stars_btn = kb_mod.main_menu().inline_keyboard[0][0]
    check("премиум-значок ставится на кнопку",
          stars_btn.icon_custom_emoji_id == "111222333")
    check("обычный значок убран из текста, чтобы не дублировался",
          not stars_btn.text.startswith(emoji.em("stars")), stars_btn.text)

    await runtime.set_value(conn, "custom_emoji_on", "0")
    stars_btn = kb_mod.main_menu().inline_keyboard[0][0]
    check("при выключенных премиум-значках кнопка обычная",
          stars_btn.icon_custom_emoji_id is None
          and stars_btn.text.startswith(emoji.em("stars")), stars_btn.text)

    # Страховка снимает значок и с кнопки
    class Btn:
        icon_custom_emoji_id = "111222333"

    class Markup:
        inline_keyboard = [[Btn()]]

    class M2:
        text = "привет"
        reply_markup = Markup()

    m2 = M2()
    tries = []

    async def send(bot, m):
        tries.append(m.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id)
        if tries[-1]:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="Bad Request: custom emoji not allowed")
        return "ok"

    check("страховка снимает премиум-значок с кнопок",
          await CustomEmojiGuard()(send, None, m2) == "ok" and tries == ["111222333", None],
          str(tries))

    await runtime.reset(conn, "emoji_id_stars")

    # ------------------------------------------- заголовки и разделители
    with_rule = [n for n, t in templates.items() if texts.LINE in t]
    check("на экранах есть разделители", len(with_rule) >= 15, f"{len(with_rule)}")

    check("в меню есть жирный заголовок", "<b>" in texts.MENU)
    check("в информации свёрнутый блок с вопросами",
          "<blockquote expandable>" in texts.INFO)

    await premium_ids(conn)


async def premium_ids(conn) -> None:
    """Премиум-эмодзи владельца прописаны заранее и снимаются насовсем."""
    from app.handlers import panel

    # Раньше здесь стояло «длина словаря равна 12»: проверка падала от
    # любого нового значка и не ловила ни одной настоящей ошибки.
    # Настоящая здесь такая — опечатка в имени значка на кнопке. Она
    # проходит молча: кнопка просто остаётся без значка, и заметить это
    # можно только глазами, на живом боте.
    import re

    source = io.open("app/keyboards.py", encoding="utf-8").read()
    named = set(re.findall(r'icon="(\w+)"', source))
    named |= set(re.findall(r'labeled\("(\w+)"', source))
    unknown = sorted(k for k in named if k not in emoji.DEFAULTS)
    check("на кнопках нет значков с опечаткой в имени",
          not unknown, f"неизвестные: {unknown}")
    check("все ключи существуют",
          all(key in emoji.DEFAULTS for key in emoji.PREMIUM_IDS),
          str([k for k in emoji.PREMIUM_IDS if k not in emoji.DEFAULTS]))
    check("все ID — числа нужной длины",
          all(v.isdigit() and 15 <= len(v) <= 25 for v in emoji.PREMIUM_IDS.values()))
    check("ID не повторяются",
          len(set(emoji.PREMIUM_IDS.values())) == len(emoji.PREMIUM_IDS))
    check("ID подхватились из умолчаний",
          emoji.custom_id("stars") == "5258165702707125574", emoji.custom_id("stars"))

    # пока проверка не пройдена, премиум-эмодзи в текст не лезут
    await runtime.set_value(conn, "custom_emoji_on", "0")
    check("выключенный премиум не подставляется",
          "tg-emoji" not in texts.MENU, texts.MENU[:80])

    await runtime.set_value(conn, "custom_emoji_on", "1")
    check("включённый премиум подставляется в текст",
          'tg-emoji emoji-id="5258204546391351475"' in texts.MENU, texts.MENU[:120])
    check("указатель тоже премиум",
          'emoji-id="5231102735817918643"' in texts.MENU, texts.MENU[-120:])
    check("запасной значок остался внутри тега",
          "💰</tg-emoji>" in texts.MENU, texts.MENU[:120])

    # снятие должно держаться, а не откатываться к прописанному ID
    call = FakeCallback("pn:emcustdel:stars")
    await panel.cb_custom_delete(call, conn)
    check("премиум-эмодзи снимается", emoji.custom_id("stars") == "",
          repr(emoji.custom_id("stars")))
    await runtime.load(conn)
    check("снятие переживает перезапуск", emoji.custom_id("stars") == "",
          repr(emoji.custom_id("stars")))
    check("остальные значки не тронуты",
          emoji.custom_id("premium") == "5805553606635559688")

    await runtime.set_value(conn, "emoji_id_stars", emoji.PREMIUM_IDS["stars"])
    await runtime.set_value(conn, "custom_emoji_on", "0")


asyncio.run(main())
