"""Отчёт: PDF строится, кириллица читается, итоги по валютам раздельные."""

import pytest
from pypdf import PdfReader

from bot import db, reports


@pytest.fixture
def filled(conn):
    db.add_transaction(conn, 1, counterparty="Абубакр", amount=500000, currency="KZT",
                       direction="out", kind="payment", item="сумки", quantity=4,
                       unit="мест", happened_on="2026-08-20", due_date="2036-09-10")
    db.add_transaction(conn, 1, counterparty="Абубакр", amount=3000, currency="TJS",
                       direction="out", kind="transfer", happened_on="2026-08-31")
    db.add_transaction(conn, 1, counterparty="Салим", amount=1200, currency="TJS",
                       direction="in", kind="income", item="мебель", happened_on="2026-08-25")
    return conn


def build(conn, config, **kw):
    config.ensure_dirs()
    kw.setdefault("date_from", "2026-08-01")
    kw.setdefault("date_to", "2026-08-31")
    kw.setdefault("font_path", config.font_path)
    kw.setdefault("font_bold_path", config.font_bold_path)
    return reports.build_report(conn, 1, out_dir=config.reports_dir, **kw)


def text_of(path):
    return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)


@pytest.mark.parametrize("amount,currency,expected", [
    (500000, "KZT", "500 000 KZT"),
    (1200.5, "TJS", "1 200.5 TJS"),
    (None, "TJS", "—"),
])
def test_fmt_money(amount, currency, expected):
    assert reports.fmt_money(amount, currency) == expected


def test_fmt_date():
    assert reports.fmt_date("2026-08-31") == "31.08.2026"
    assert reports.fmt_date(None) == "—"


def test_report_renders_cyrillic_not_boxes(filled, config):
    """Главная проверка: встроенные шрифты reportlab дают квадраты вместо русских букв."""
    path, count = build(filled, config)
    assert count == 3
    text = text_of(path)
    for word in ("Отчёт по операциям", "Абубакр", "сумки", "Итоги по валютам"):
        assert word in text, f"нет «{word}» в PDF"


def test_report_keeps_currencies_separate(filled, config):
    path, _ = build(filled, config)
    text = text_of(path)
    assert "500 000 KZT" in text
    assert "3 000 TJS" in text
    # Никакой конвертации: суммы разных валют не складываются в одно число.
    assert "503 000" not in text


def test_report_lists_open_due_dates(filled, config):
    path, _ = build(filled, config)
    assert "Сроки, которые ещё не прошли" in text_of(path)


def test_empty_period_still_builds_valid_pdf(conn, config):
    path, count = build(conn, config, date_from="2020-01-01", date_to="2020-01-31")
    assert count == 0
    assert "Операций за этот период нет." in text_of(path)


def test_report_can_filter_by_counterparty(filled, config):
    path, count = build(filled, config, counterparty="Салим")
    assert count == 1
    assert "Салим" in text_of(path)


def test_missing_font_raises_clear_error(conn, config):
    reports._fonts_ready = False
    try:
        with pytest.raises(RuntimeError, match="кириллиц"):
            build(conn, config, font_path="/nope/missing.ttf", font_bold_path=None)
    finally:
        reports._fonts_ready = False
