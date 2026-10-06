"""Перенос балансов со старого бота.

Список выгружают по-разному: «123456789 100», «123456789 - 100.50»,
«id: 123456789, balance: 100», таблицей с заголовками. Разбирать каждый
формат отдельно бессмысленно — берём из строки два числа: первое длинное
это ID, последнее это сумма.

Деньги чужие, поэтому разбор ничего не применяет сам: он только читает
и показывает, что понял. Применяет — владелец, отдельным нажатием.
"""
from __future__ import annotations

import re

#: ID пользователя Telegram: минимум пять цифр. Меньше — не бывает.
ID_RE = re.compile(r"\b(\d{5,15})\b")
#: Сумма: с копейками или без, разделитель точка или запятая.
MONEY_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")
#: Юзернейм, если он есть в строке.
NAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")
#: Юзернейм целиком — для колонки, где он уже отделён от прочего.
NAME_OK = re.compile(r"[A-Za-z0-9_]{4,32}")
#: Хвост таблицы выгрузки: баланс, потрачено, число заказов.
#: Без этого из строки «… 70.00  0.00  0» взялось бы последнее число —
#: количество заказов, и все балансы обнулились бы.
TABLE_TAIL = re.compile(
    r"(\d+(?:[.,]\d{1,2}))\s+(\d+(?:[.,]\d{1,2}))\s+(\d+)\s*$"
)


def to_diram(text: str) -> int | None:
    """Сумму в сомони — в дирамы. None — не разобрали."""
    cleaned = str(text or "").replace(",", ".").replace(" ", "").strip()
    cleaned = cleaned.replace("\u00a0", "")
    try:
        value = round(float(cleaned) * 100)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_balances(raw: str) -> tuple[list[dict], list[str]]:
    """Разобрать список. Возвращает (записи, непонятые строки).

    Запись — это {"id", "amount", "username"}. В «непонятые» попадают
    только строки, где ID есть, а сумму разобрать не вышло: шапка и
    разделители отбрасываются молча.

    Повтор одного ID не удваивает баланс: остаётся последняя строка —
    так же, как если бы выгрузку сделали заново.
    """
    found: dict[int, dict] = {}
    skipped: list[str] = []

    for line in (raw or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue

        ids = ID_RE.findall(line)
        if not ids:
            # Шапка, разделители, итоги — в выгрузке их всегда полно.
            # Показывать их владельцу как «не понял» значит пугать зря:
            # в них и нет ничего, что нужно переносить.
            continue

        user_id = int(ids[0])
        # Сумма — это то, что стоит после ID: иначе в строке вида
        # «5 123456789 100» первым числом оказался бы номер по порядку.
        tail = line[line.index(ids[0]) + len(ids[0]):]

        # Выгрузка таблицей: в конце строки баланс, потрачено и число
        # заказов. Берём первое из трёх — остальное не баланс.
        table = TABLE_TAIL.search(tail)
        if table:
            amount = to_diram(table.group(1))
        else:
            numbers = MONEY_RE.findall(tail)
            amount = to_diram(numbers[-1]) if numbers else None

        if amount is None:
            skipped.append(line.strip())
            continue

        name = NAME_RE.search(tail)
        found[user_id] = {
            "id": user_id,
            "amount": amount,
            "username": name.group(1) if name else "",
        }

    return list(found.values()), skipped


#: Как в выгрузках называют колонку с ID.
ID_HEADERS = ("telegram id", "telegram_id", "user id", "user_id", "id",
              "чат", "chat id", "chat_id", "корбар")
#: И колонку с остатком на счету. «Харҷ» (потрачено) сюда не входит
#: намеренно: спутать её с балансом — раздать людям чужие деньги.
MONEY_HEADERS = ("ҳамён", "хамён", "hamyon", "баланс", "balance", "wallet",
                 "остаток", "счёт", "счет", "сумма", "amount")
#: И колонку с юзернеймом.
NAME_HEADERS = ("username", "юзернейм", "ник", "nick", "@")


def _header_index(cells: list[str], names: tuple[str, ...]) -> int | None:
    for index, cell in enumerate(cells):
        low = str(cell).strip().lower()
        if low and any(mark in low for mark in names):
            return index
    return None


def find_header(rows: list[list[str]]) -> tuple[int, dict] | None:
    """Найти строку заголовка и номера нужных колонок.

    Без заголовка пришлось бы гадать по порядку колонок, а он у каждой
    выгрузки свой: где-то баланс третий, где-то пятый, а рядом стоит
    «потрачено» — и перепутать их значит раздать людям чужие деньги.
    """
    for number, cells in enumerate(rows[:20]):
        ids = _header_index(cells, ID_HEADERS)
        money = _header_index(cells, MONEY_HEADERS)
        if ids is not None and money is not None:
            return number, {
                "id": ids, "amount": money,
                "username": _header_index(cells, NAME_HEADERS),
            }
    return None


def parse_rows(rows: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Разобрать таблицу из файла.

    Если в ней есть понятный заголовок — читаем по колонкам. Если нет,
    склеиваем ячейки обратно в строку и разбираем как обычный список:
    так файл без заголовка тоже не пропадёт.
    """
    header = find_header(rows)
    if header is None:
        text = "\n".join(" ".join(str(cell) for cell in row) for row in rows)
        return parse_balances(text)

    start, columns = header
    found: dict[int, dict] = {}
    skipped: list[str] = []

    for cells in rows[start + 1:]:
        raw_id = _at(cells, columns["id"])
        digits = "".join(ch for ch in raw_id if ch.isdigit())
        if len(digits) < 5:
            continue          # пустая строка, итоги, разделитель

        amount = to_diram(_at(cells, columns["amount"]))
        if amount is None:
            skipped.append(" ".join(str(cell) for cell in cells)[:80])
            continue

        name = _at(cells, columns["username"]).lstrip("@").strip()
        found[int(digits)] = {
            "id": int(digits),
            "amount": amount,
            "username": name if NAME_OK.fullmatch(name) else "",
        }
    return list(found.values()), skipped


def _at(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return str(cells[index]).strip()


def with_money(rows: list[dict]) -> list[dict]:
    """Только те, у кого на счету есть деньги."""
    return [row for row in rows if row["amount"] > 0]


def preview(rows: list[dict], limit: int = 5) -> list[dict]:
    """Первые записи — чтобы владелец глазами сверил, что понято верно.

    Показываем сначала тех, у кого деньги: нули занимают весь экран,
    а проверять надо именно суммы.
    """
    rich = sorted(with_money(rows), key=lambda row: -row["amount"])
    return (rich or rows)[:limit]
