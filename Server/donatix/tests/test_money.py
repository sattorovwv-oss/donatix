from decimal import Decimal

from donatix.money import apply_markup, fmt, fmt_unit, order_total_micro, to_micro


def test_fmt_four_digits():
    assert fmt(153750) == "15.3750"
    assert fmt(-7688) == "-0.7688"
    assert fmt(0) == "0.0000"


def test_markup_and_total_round_up():
    unit = apply_markup(Decimal("0.015375"), Decimal("8"))
    assert unit == Decimal("0.016605")
    # 100 звёзд: 1.6605 ровно; 3 звезды: 0.049815 → округляем ВВЕРХ до 0.0499
    assert order_total_micro(unit, 100) == 16605
    assert order_total_micro(unit, 3) == 499


def test_total_never_below_cost():
    base = Decimal("0.0150000")
    for qty in (50, 51, 333, 9999):
        assert order_total_micro(apply_markup(base, Decimal("0")), qty) >= base * qty * 10000


def test_fmt_unit():
    assert fmt_unit(Decimal("0.016605")) == "0.016605"
    assert fmt_unit(Decimal("10.5")) == "10.5000"


def test_to_micro():
    assert to_micro("100") == 1_000_000
    assert to_micro("-5.50") == -55_000
