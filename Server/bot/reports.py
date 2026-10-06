"""PDF-отчёты. Кириллица через DejaVu — встроенные шрифты reportlab её не умеют."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import db

FONT = "DejaVu"
FONT_BOLD = "DejaVu-Bold"

DIRECTION_RU = {"out": "отправлено", "in": "получено"}
KIND_RU = {
    "transfer": "перевод",
    "payment": "оплата",
    "debt": "долг",
    "income": "приход",
}

_fonts_ready = False


def register_fonts(regular: str | None, bold: str | None) -> None:
    """Зарегистрировать шрифты один раз за процесс."""
    global _fonts_ready
    if _fonts_ready:
        return
    if not regular or not Path(regular).is_file():
        raise RuntimeError(
            "Не найден шрифт с кириллицей (DejaVuSans.ttf). "
            "Установи пакет fonts-dejavu или задай FONT_PATH в .env."
        )
    pdfmetrics.registerFont(TTFont(FONT, regular))
    if bold and Path(bold).is_file():
        pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
    else:
        pdfmetrics.registerFont(TTFont(FONT_BOLD, regular))
    _fonts_ready = True


def fmt_money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    whole = f"{amount:,.2f}".rstrip("0").rstrip(".") if amount % 1 else f"{int(amount):,}"
    return f"{whole.replace(',', ' ')} {(currency or '').upper()}".strip()


def fmt_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value[:10]


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName=FONT_BOLD, fontSize=16, spaceAfter=4),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontName=FONT, fontSize=10,
                              textColor=colors.HexColor("#555555"), spaceAfter=10),
        "h2": ParagraphStyle("h", parent=base["Heading2"], fontName=FONT_BOLD, fontSize=12, spaceBefore=10, spaceAfter=6),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontName=FONT, fontSize=8.5, leading=11),
        "note": ParagraphStyle("n", parent=base["Normal"], fontName=FONT, fontSize=9,
                               textColor=colors.HexColor("#7a4a00"), spaceBefore=8),
    }


def build_report(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    date_from: str,
    date_to: str,
    out_dir: Path,
    font_path: str | None,
    font_bold_path: str | None,
    counterparty: str | None = None,
    direction: str | None = None,
    text: str | None = None,
) -> tuple[Path, int]:
    """Собрать PDF за период. Возвращает (путь, число операций)."""
    register_fonts(font_path, font_bold_path)
    st = _styles()

    rows = db.search_transactions(
        conn, owner_id,
        text=text, counterparty=counterparty,
        date_from=date_from, date_to=date_to, direction=direction,
        limit=2000,
    )
    rows.sort(key=lambda r: (r.get("happened_on") or r["created_at"][:10], r["id"]))

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    path = out_dir / f"report_{date_from}_{date_to}_{stamp}.pdf"

    doc = SimpleDocTemplate(
        str(path), pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"Отчёт {date_from} — {date_to}", author="Финансовый бот",
    )

    story: list[Any] = [Paragraph("Отчёт по операциям", st["title"])]
    filters = [f"Период: {fmt_date(date_from)} — {fmt_date(date_to)}"]
    if counterparty:
        filters.append(f"Контрагент: {counterparty}")
    if direction:
        filters.append(f"Направление: {DIRECTION_RU.get(direction, direction)}")
    if text:
        filters.append(f"Поиск: {text}")
    filters.append(f"Записей: {len(rows)}")
    story.append(Paragraph(" · ".join(filters), st["sub"]))

    # ── Итоги по валютам (без конвертации) ──
    totals = db.totals_by_currency(rows)
    story.append(Paragraph("Итоги по валютам", st["h2"]))
    if totals:
        head = ["Валюта", "Отправлено / оплачено", "Получено", "Разница", "Операций"]
        data = [head]
        for cur in sorted(totals):
            t = totals[cur]
            data.append([
                cur,
                fmt_money(t["out"], cur),
                fmt_money(t["in"], cur),
                fmt_money(t["in"] - t["out"], cur),
                str(int(t["count"])),
            ])
        table = Table(data, colWidths=[25 * mm, 55 * mm, 45 * mm, 45 * mm, 25 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("За этот период операций с суммами нет.", st["cell"]))

    # ── Список операций ──
    story.append(Paragraph("Операции", st["h2"]))
    if rows:
        head = ["Дата", "Кто / кому", "Тип", "Сумма", "За что", "Кол-во", "Срок", "Заметка"]
        data = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in head]]
        for r in rows:
            qty = ""
            if r.get("quantity") is not None:
                q = r["quantity"]
                qty = f"{int(q) if float(q).is_integer() else q} {r.get('unit') or ''}".strip()
            kind = KIND_RU.get(r.get("kind") or "", r.get("kind") or "")
            direction_ru = DIRECTION_RU.get(r.get("direction") or "", "")
            type_cell = " / ".join(x for x in (kind, direction_ru) if x)
            data.append([
                Paragraph(fmt_date(r.get("happened_on") or r["created_at"]), st["cell"]),
                Paragraph(r.get("counterparty") or "—", st["cell"]),
                Paragraph(type_cell or "—", st["cell"]),
                Paragraph(fmt_money(r.get("amount"), r.get("currency")), st["cell"]),
                Paragraph(r.get("item") or "—", st["cell"]),
                Paragraph(qty or "—", st["cell"]),
                Paragraph(fmt_date(r.get("due_date")) if r.get("due_date") else "—", st["cell"]),
                Paragraph(r.get("note") or "", st["cell"]),
            ])
        table = Table(
            data,
            colWidths=[22 * mm, 38 * mm, 30 * mm, 34 * mm, 55 * mm, 20 * mm, 22 * mm, 45 * mm],
            repeatRows=1, hAlign="LEFT",
        )
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("Операций за этот период нет.", st["cell"]))

    # ── Открытые сроки ──
    today = datetime.now().strftime("%Y-%m-%d")
    open_due = [r for r in rows if r.get("due_date") and r["due_date"] >= today]
    if open_due:
        story.append(Spacer(1, 4 * mm))
        lines = [
            f"{fmt_date(r['due_date'])} — {r.get('counterparty') or 'без имени'}: "
            f"{fmt_money(r.get('amount'), r.get('currency'))}"
            + (f" за «{r['item']}»" if r.get("item") else "")
            for r in sorted(open_due, key=lambda r: r["due_date"])
        ]
        story.append(Paragraph("Сроки, которые ещё не прошли:<br/>" + "<br/>".join(lines), st["note"]))

    doc.build(story)
    return path, len(rows)
