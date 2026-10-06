"""Разбор уведомления банковского бота.

Пример настоящего уведомления:

    Zachislenie
    Summa 617.00 TJS
    Komis 0.00 TJS
    Zachislenie 617.00 TJS
    Data 19:40 15.09.26
    Otpravitel 9990000***1111
    Kod 10000000001
    Karta 9999000011112222
    Balans 3 686.07 TJS

Главное правило здесь одно: **при малейшем сомнении — отказ**. Ошибиться
в сторону «не понял» стоит владельцу минуты ручной проверки. Ошибиться в
другую сторону — значит зачислить чужие деньги чужому человеку, и узнает
он об этом от пострадавшего.

Поэтому:

  * сумма берётся ТОЛЬКО из поля Zachislenie — это то, что реально
    пришло на счёт. Summa — это сколько отправили, Komis — сколько
    забрал банк, Balans — остаток на карте. Ни одно из трёх суммой
    платежа не является;
  * если поля Zachislenie с числом нет, берём Summa — но лишь когда
    комиссия явно нулевая. При любой комиссии без Zachislenie
    зачисленное неизвестно, и это отказ;
  * значение поля должно разобраться целиком. Любой хвост, которого мы
    не ждали, — отказ, а не попытка угадать.

Разбор идёт по именам полей, а не по номерам строк: банк может
переставить строки, добавить свою, сменить регистр — и всё это не
должно ломать распознавание.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Поля, которые мы понимаем. Всё остальное в сообщении пропускается.
FIELDS = ("summa", "zachislenie", "komis", "data", "otpravitel", "kod",
          "karta", "balans", "comment")

#: Слова, которыми банк называет приход денег.
CREDIT = ("zachislenie", "зачисление", "postuplenie", "поступление")

#: Слова, которыми банк называет расход. Такое уведомление — не платёж
#: нам, и разбирать его дальше нельзя ни при каких условиях.
DEBIT = ("spisanie", "списание", "snyatie", "снятие", "perevod", "перевод",
         "oplata", "оплата", "vozvrat", "возврат", "otkaz", "отказ")

#: Валюты, которые банк дописывает к сумме. Из значения убираются.
CURRENCY = ("tjs", "смн", "сомони", "somoni", "руб", "rub", "usd", "eur")

#: Строка вида «Имя значение». Разделителем бывает пробел, двоеточие,
#: точка или тире.
#:
#: Дефис считается разделителем, только если за ним пробел. Иначе
#: «Zachislenie -617.00» прочиталось бы как приход 617 сомони, а это
#: списание — самая дорогая ошибка, какую здесь можно сделать.
LINE = re.compile(r"^([A-Za-zА-Яа-яЁё]+)\s*(?:[:.]|-(?=\s)|[–—])?\s*(.*)$")

#: Сумма: цифры, возможны пробелы-разделители тысяч и два знака после
#: точки или запятой. Совпасть должно всё значение целиком.
AMOUNT = re.compile(r"^(\d[\d  ]*)(?:[.,](\d{1,2}))?$")

#: Больше этого в одном переводе не бывает — скорее сломанный разбор.
#: Миллион сомони в дирамах.
MAX_AMOUNT = 100_000_000


@dataclass
class Notice:
    """Разобранное уведомление."""
    amount: int = 0                 # дирамы, фактически зачислено
    op_code: str = ""
    sender: str = ""
    card_tail: str = ""
    bank_time: str = ""
    comment: str = ""               # приписка банка: «Сбербанк» и прочее
    source_field: str = ""          # из какого поля взята сумма
    error: str = ""                 # пусто — разобралось

    @property
    def ok(self) -> bool:
        return not self.error and self.amount > 0


def money(value: str, allow_zero: bool = False) -> int | None:
    """'617.00 TJS' -> 61700 дирам. None — разобрать не вышло.

    Возвращаем целое число в дирамах, а не дробное: 0.1 + 0.2 в любом
    языке даёт не 0.3, и сравнение сумм перестало бы быть точным.

    allow_zero нужен комиссии: «Komis 0.00» — совершенно нормальная
    строка, а вот зачисление на ноль смысла не имеет и разбором не
    считается.
    """
    text = (value or "").strip().replace(" ", " ")
    # Отрицательная сумма — это расход. Приходом она не станет ни при
    # каких обстоятельствах, и угадывать тут нечего.
    if text.startswith(("-", "−", "–", "—", "+")):
        return None
    for word in CURRENCY:
        text = re.sub(rf"(?i)\b{word}\b\.?", " ", text)
    text = text.strip()

    hit = AMOUNT.match(text)
    if hit is None:
        return None

    whole = hit.group(1).replace(" ", "")
    if not whole.isdigit():
        return None
    frac = (hit.group(2) or "").ljust(2, "0")
    total = int(whole) * 100 + int(frac)
    if total > MAX_AMOUNT or (total == 0 and not allow_zero):
        return None
    return total


def fields(text: str) -> dict[str, list[str]]:
    """Значения полей по именам. Список — потому что Zachislenie в
    уведомлении встречается дважды: заголовком и суммой."""
    out: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        hit = LINE.match(line.strip())
        if hit is None:
            continue
        name = hit.group(1).lower()
        if name in FIELDS:
            out.setdefault(name, []).append(hit.group(2).strip())
    return out


def head(text: str) -> str:
    """Первая значащая строка — банк пишет там тип операции."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip().lower()
    return ""


def mask_card(number: str) -> str:
    """Последние четыре знака карты. Больше нам не нужно и хранить
    больше нельзя: полный номер в базе и логах — это утечка."""
    digits = re.sub(r"\D", "", number or "")
    return digits[-4:] if len(digits) >= 4 else ""


def mask_sender(value: str) -> str:
    """Счёт плательщика — настолько скрытый, насколько можно, но не больше.

    Банк присылает его уже с дырой посередине: 9921071***0706. Так и
    оставляем. Своей маскировки поверх банковской быть не должно: от неё
    оставались первые три цифры и последние четыре, а первые три у всех
    одинаковые — код страны. Опознавали бы плательщика по четырём
    знакам, и любой чужой счёт с тем же хвостом сходил бы за своего.

    Если банк прислал счёт целиком, прячем середину сами, но оставляем
    достаточно, чтобы плательщики не сливались в одного.
    """
    raw = (value or "").strip()[:40]
    if not raw:
        return ""
    if "*" in raw:
        return raw          # банк уже скрыл — своего поверх не добавляем

    digits = re.sub(r"\D", "", raw)
    if len(digits) <= 10:
        return raw
    return f"{digits[:6]}***{digits[-4:]}"


def parse(text: str) -> Notice:
    """Разобрать уведомление. При любом сомнении — Notice с error."""
    if not (text or "").strip():
        return Notice(error="пустое сообщение")

    first = head(text)
    if any(first.startswith(word) for word in DEBIT):
        return Notice(error=f"это не зачисление: {first[:40]}")

    found = fields(text)

    # Заголовок «Zachislenie» идёт без значения — его пропускаем.
    # Значение, которое есть, но не читается, — повод отказать, а не
    # искать сумму в другом поле: банк назвал зачисление, мы его не
    # поняли, и подставлять вместо него Summa значит гадать.
    credited, source_field = None, ""

    filled = _values(found.get("zachislenie"))
    if filled:
        amounts = [money(value) for value in filled]
        if any(amount is None for amount in amounts):
            return Notice(error="сумму зачисления не разобрал")
        if len(set(amounts)) > 1:
            return Notice(error="в сообщении две разные суммы зачисления")
        credited, source_field = amounts[0], "zachislenie"

    if credited is None:
        # Zachislenie без суммы бывает, когда банк её не дублирует. Тогда
        # зачислено ровно столько, сколько отправлено, — но только если
        # комиссии не было. С комиссией зачисленное неизвестно.
        sent_raw = _values(found.get("summa"))
        fee_raw = _values(found.get("komis"))
        if not sent_raw:
            return Notice(error="сумму зачисления не нашёл")

        sent = money(sent_raw[0])
        if sent is None:
            return Notice(error="сумму отправления не разобрал")

        if fee_raw:
            # Комиссия названа — значит она была, и зачисленное ей не
            # равно. Складывать или вычитать наугад нельзя.
            fees = [money(value, allow_zero=True) for value in fee_raw]
            if any(fee is None for fee in fees):
                return Notice(error="комиссию не разобрал")
            if any(fee for fee in fees):
                return Notice(error="есть комиссия, но нет суммы зачисления")
        # Строки о комиссии нет вовсе — так приходят переводы из России
        # (Сбербанк, Тинькофф): банк уже удержал своё на той стороне и
        # пишет одну сумму, ту самую, что легла на счёт. Раньше мы такие
        # уведомления отвергали целиком, и каждый платёж из России
        # приходилось подтверждать руками.
        credited, source_field = sent, "summa"

    is_credit = ("zachislenie" in found
                 or any(first.startswith(word) for word in CREDIT))
    if not is_credit:
        return Notice(error="в сообщении нет слова о зачислении")

    return Notice(
        amount=credited,
        op_code=_first(found.get("kod")),
        sender=mask_sender(_first(found.get("otpravitel"))),
        card_tail=mask_card(_first(found.get("karta"))),
        bank_time=_first(found.get("data"))[:32],
        comment=_first(found.get("comment"))[:64],
        source_field=source_field,
    )


def _values(raw: list[str] | None) -> list[str]:
    """Только непустые значения поля: заголовок без значения не в счёт."""
    return [value for value in (raw or []) if value.strip()]


def _first(values: list[str] | None) -> str:
    for value in values or []:
        if value.strip():
            return value.strip()
    return ""


def safe_body(text: str) -> str:
    """Текст уведомления для хранения — с вырезанным номером карты.

    Храним, чтобы владелец мог глазами сверить спорный платёж. Но номер
    карты в базе не нужен никому и никогда.
    """
    def hide(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(2))
        return f"{match.group(1)}{'*' * max(0, len(digits) - 4)}{digits[-4:]}"

    cleaned = re.sub(r"(?i)^(\s*karta\s*[:.\-]?\s*)([\d \-]{8,})$", hide,
                     text or "", flags=re.MULTILINE)
    # На всякий случай — любая длинная цепочка цифр, даже не под Karta.
    cleaned = re.sub(r"\b(\d{4})\d{6,}(\d{4})\b", r"\1****\2", cleaned)
    return cleaned[:2000]
