"""Чтение таблиц: .xlsx и .csv — без внешних библиотек.

Файл .xlsx — это обычный zip с несколькими xml внутри, и прочитать из
него ячейки можно стандартной библиотекой. Ради одной операции в год
тянуть в зависимости целую библиотеку незачем: она потребует обновлений,
сборки на сервере и однажды сломает установку.

Возвращаем всегда одно и то же — список строк, строка это список текстов.
Кто вызывает, разбирает уже сам.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from xml.etree import ElementTree

log = logging.getLogger(__name__)

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
#: Больше этого не читаем: файл приходит из чужих рук.
MAX_ROWS = 20_000
MAX_BYTES = 12 * 1024 * 1024


class SheetError(Exception):
    """Файл не прочитался — с понятной человеку причиной."""


def read(data: bytes, filename: str = "") -> list[list[str]]:
    """Прочитать таблицу. Формат определяем по содержимому, не по имени:
    имя файла клиент может поменять как угодно."""
    if len(data) > MAX_BYTES:
        raise SheetError("файл слишком большой")

    if data[:2] == b"PK":
        return _xlsx(data)
    return _text(data, filename)


def _cell_text(cell, shared: list[str]) -> str:
    """Текст ячейки. Строки лежат либо в общем списке, либо внутри."""
    kind = cell.get("t")
    if kind == "inlineStr":
        parts = cell.iter(f"{NS}t")
        return "".join(part.text or "" for part in parts).strip()

    value = cell.find(f"{NS}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text.strip()


def _xlsx(data: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SheetError("это не файл Excel") from exc

    names = archive.namelist()
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/")
                    and n.endswith(".xml"))
    if not sheets:
        raise SheetError("в файле нет ни одного листа")

    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root.iter(f"{NS}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{NS}t")))

    root = ElementTree.fromstring(archive.read(sheets[0]))
    rows: list[list[str]] = []
    for row in root.iter(f"{NS}row"):
        cells = [_cell_text(c, shared) for c in row.iter(f"{NS}c")]
        if any(cells):
            rows.append(cells)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def _text(data: bytes, filename: str) -> list[list[str]]:
    """CSV или просто текст. Разделитель угадываем по первой строке."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SheetError("не смог прочитать текст файла")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        reader = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        # Не CSV — значит обычный текст, каждая строка целиком.
        return [[line] for line in text.splitlines() if line.strip()][:MAX_ROWS]

    rows = [[str(cell).strip() for cell in row] for row in reader if any(row)]
    return rows[:MAX_ROWS]
