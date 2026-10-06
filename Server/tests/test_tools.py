"""Инструменты: приведение грязных данных и работа над временной базой."""

import pytest

from bot import db, tools


@pytest.fixture
def ctx(conn, config, tmp_path):
    config.ensure_dirs()
    return tools.ToolContext(
        conn=conn, owner_id=1, result=tools.TurnResult(),
        reports_dir=config.reports_dir,
        font_path=config.font_path, font_bold_path=config.font_bold_path,
        default_currency="TJS", today="2026-08-31",
    )


# ── чистка входа ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("сомони", "TJS"), ("Тенге", "KZT"), ("рублей", "RUB"),
    ("kzt", "KZT"), ("$", "USD"), (None, "TJS"), ("", "TJS"),
])
def test_clean_currency(value, expected):
    assert tools.clean_currency(value, "TJS") == expected


@pytest.mark.parametrize("value,expected", [
    (500000, 500000.0), ("500000", 500000.0), ("500 000", 500000.0),
    ("1,5", 1.5), ("не число", None), (None, None), ("", None),
])
def test_clean_amount(value, expected):
    assert tools.clean_amount(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("2026-08-31", "2026-08-31"), ("31.08.2026", "2026-08-31"),
    ("2026-08-31T10:00:00", "2026-08-31"), ("завтра", None), (None, None),
])
def test_clean_date(value, expected):
    assert tools.clean_date(value) == expected


# ── сохранение ─────────────────────────────────────────────────────────────

def test_save_full_record(ctx):
    out = tools.t_save_transaction(ctx, {
        "direction": "out", "kind": "payment", "counterparty": "Абубакр",
        "amount": 500000, "currency": "тенге", "item": "сумки",
        "quantity": 4, "unit": "мест", "happened_on": "2026-08-20",
        "due_date": "2026-08-30", "raw_text": "оплатил за товар сумки четыре места",
    })
    assert out["ok"] is True
    row = db.get_transaction(ctx.conn, 1, ctx.result.saved_transaction_ids[0])
    assert row["amount"] == 500000
    assert row["currency"] == "KZT"
    assert row["quantity"] == 4
    assert row["due_date"] == "2026-08-30"


def test_save_without_date_uses_today(ctx):
    tools.t_save_transaction(ctx, {"counterparty": "Салим", "amount": 100})
    row = db.get_transaction(ctx.conn, 1, ctx.result.saved_transaction_ids[0])
    assert row["happened_on"] == "2026-08-31"


def test_save_without_currency_falls_back_to_default(ctx):
    tools.t_save_transaction(ctx, {"amount": 100})
    row = db.get_transaction(ctx.conn, 1, ctx.result.saved_transaction_ids[0])
    assert row["currency"] == "TJS"


def test_save_without_amount_leaves_currency_empty(ctx):
    """Нет суммы — нет и валюты, иначе в отчёте появится фантомная строка."""
    tools.t_save_transaction(ctx, {"counterparty": "Салим", "item": "мебель"})
    row = db.get_transaction(ctx.conn, 1, ctx.result.saved_transaction_ids[0])
    assert row["amount"] is None
    assert row["currency"] is None


def test_save_rejects_bogus_enum_values(ctx):
    tools.t_save_transaction(ctx, {"direction": "куда-то", "kind": "непонятно", "amount": 5})
    row = db.get_transaction(ctx.conn, 1, ctx.result.saved_transaction_ids[0])
    assert row["direction"] == "out"
    assert row["kind"] == "transfer"


def test_save_empty_input_still_stores_row(ctx):
    """Пустой вызов не должен ронять бота — запись создаётся, поля пустые."""
    out = tools.t_save_transaction(ctx, {})
    assert out["ok"] is True
    assert len(ctx.result.saved_transaction_ids) == 1


# ── поиск ──────────────────────────────────────────────────────────────────

def test_search_returns_list_and_totals(ctx):
    tools.t_save_transaction(ctx, {"counterparty": "Абубакр", "amount": 3000,
                                   "currency": "сомони", "happened_on": "2026-08-01"})
    tools.t_save_transaction(ctx, {"counterparty": "Абубакр", "amount": 2000,
                                   "currency": "сомони", "happened_on": "2026-08-15"})
    out = tools.t_search_transactions(ctx, {"counterparty": "Абубакр"})
    assert out["найдено"] == 2
    assert len(out["операции"]) == 2  # вся история, не только последняя
    assert out["итоги_по_валютам"]["TJS"]["out"] == 5000


def test_search_empty_result_is_explicit(ctx):
    out = tools.t_search_transactions(ctx, {"counterparty": "Никто"})
    assert out["найдено"] == 0
    assert out["операции"] == []


# ── правка и удаление ──────────────────────────────────────────────────────

def test_update_changes_amount(ctx):
    tools.t_save_transaction(ctx, {"amount": 500000, "currency": "KZT"})
    tx_id = ctx.result.saved_transaction_ids[0]
    out = tools.t_update_transaction(ctx, {"transaction_id": tx_id, "amount": 400000})
    assert out["ok"] is True
    assert db.get_transaction(ctx.conn, 1, tx_id)["amount"] == 400000


def test_update_missing_row_reports_error(ctx):
    out = tools.t_update_transaction(ctx, {"transaction_id": 999, "amount": 1})
    assert out["ok"] is False


def test_delete_missing_row_reports_error(ctx):
    assert tools.t_delete_transaction(ctx, {"transaction_id": 999})["ok"] is False


# ── документы ──────────────────────────────────────────────────────────────

def test_describe_then_find_then_send(ctx):
    """Полный сценарий: подписал накладную → нашёл → поставил в очередь на отправку."""
    doc_id = db.add_document(ctx.conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")

    assert tools.t_describe_document(ctx, {
        "document_id": doc_id, "description": "накладная на женскую обувь",
        "doc_kind": "накладная",
    })["ok"] is True

    found = tools.t_find_documents(ctx, {"text": "накладная от женской обуви"})
    assert found["найдено"] == 1
    assert found["фото"][0]["id"] == doc_id

    sent = tools.t_send_documents(ctx, {"document_ids": [doc_id]})
    assert sent["ok"] is True
    assert ctx.result.documents_to_send == [doc_id]


def test_send_unknown_document_is_error(ctx):
    out = tools.t_send_documents(ctx, {"document_ids": [999]})
    assert out["ok"] is False
    assert ctx.result.documents_to_send == []


def test_send_accepts_single_id_not_in_list(ctx):
    doc_id = db.add_document(ctx.conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    assert tools.t_send_documents(ctx, {"document_ids": doc_id})["ok"] is True


def test_describe_missing_document_is_error(ctx):
    assert tools.t_describe_document(ctx, {"document_id": 999, "description": "x"})["ok"] is False


# ── отчёт ──────────────────────────────────────────────────────────────────

def test_build_report_creates_pdf(ctx):
    tools.t_save_transaction(ctx, {"amount": 500000, "currency": "KZT",
                                   "item": "сумки", "happened_on": "2026-08-10"})
    out = tools.t_build_report(ctx, {"date_from": "2026-08-01", "date_to": "2026-08-31"})
    assert out["ok"] is True
    assert out["операций"] == 1
    assert len(ctx.result.reports_to_send) == 1
    assert ctx.result.reports_to_send[0].is_file()


def test_build_report_needs_both_dates(ctx):
    assert tools.t_build_report(ctx, {"date_from": "2026-08-01"})["ok"] is False


def test_build_report_swaps_reversed_dates(ctx):
    out = tools.t_build_report(ctx, {"date_from": "2026-08-31", "date_to": "2026-08-01"})
    assert out["ok"] is True
    assert out["период"] == "2026-08-01 — 2026-08-31"
