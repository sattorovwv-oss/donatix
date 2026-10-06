"""Разрезание длинных ответов и доступ по id."""

import pytest

from bot.handlers import TG_TEXT_LIMIT, clean_name, _tx_line, split_message


def test_short_message_is_not_split():
    assert split_message("короткий ответ") == ["короткий ответ"]


def test_long_history_is_split_under_the_limit():
    """История за год не должна упираться в лимит Telegram и теряться целиком."""
    text = "\n".join(f"{i:03d}. 31.08.2026 · Абубакр · 500 000 KZT за «сумки»" for i in range(300))
    assert len(text) > TG_TEXT_LIMIT
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= TG_TEXT_LIMIT for c in chunks)
    # Ни одна запись не потерялась.
    assert sum(c.count("Абубакр") for c in chunks) == 300


def test_split_does_not_break_lines_in_the_middle():
    text = "\n".join(["запись " + "x" * 100] * 200)
    for chunk in split_message(text):
        for line in chunk.split("\n"):
            assert line == "" or line.startswith("запись ")


def test_single_overlong_line_is_hard_split():
    chunks = split_message("я" * (TG_TEXT_LIMIT * 2 + 7))
    assert all(len(c) <= TG_TEXT_LIMIT for c in chunks)
    assert sum(len(c) for c in chunks) == TG_TEXT_LIMIT * 2 + 7


@pytest.mark.parametrize("raw,expected", [
    ("Алиджон", "Алиджон"),
    ("  Алиджон   Махмаджонов  ", "Алиджон Махмаджонов"),
    ("Ali", "Ali"),
])
def test_good_names_are_accepted(raw, expected):
    assert clean_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "А", "x" * 65,
    "жми https://spam.example",      # имя попадает в панель админа — ссылок там быть не должно
    "смотри t.me/spam",
    "<b>жирный</b>",
])
def test_junk_names_are_rejected(raw):
    assert clean_name(raw) is None


def test_transaction_line_is_readable():
    row = {"id": 1, "created_at": "2026-08-31T00:00:00", "happened_on": "2026-08-20",
           "counterparty": "Абубакр", "amount": 500000, "currency": "KZT", "item": "сумки"}
    assert _tx_line(row) == "20.08.2026 · Абубакр · 500 000 KZT · за «сумки»"
