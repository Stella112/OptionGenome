"""OCC symbol round-trip and rejection tests (spec section 21)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.broker.occ import (
    OCCError,
    OptionContract,
    build_option_symbol,
    is_option_symbol,
    parse_option_symbol,
)


def test_spec_example_parses_exactly():
    contract = parse_option_symbol("SPY250127C00608000")
    assert contract.underlying == "SPY"
    assert contract.expiry == date(2025, 1, 27)
    assert contract.right == "C"
    assert contract.strike == Decimal("608.00")


def test_spec_example_builds_exactly():
    assert build_option_symbol("SPY", date(2025, 1, 27), "C", Decimal("608")) == "SPY250127C00608000"


@pytest.mark.parametrize("right", ["C", "P"])
@pytest.mark.parametrize(
    "strike", ["0.5", "1", "608", "608.50", "1234.125", "99999.999"]
)
@pytest.mark.parametrize("expiry", [date(2025, 1, 27), date(2026, 9, 4), date(2030, 12, 31)])
def test_parse_of_build_round_trips(right, strike, expiry):
    """parse(build(x)) == x, for calls and puts, across the strike grid."""
    original = OptionContract(
        underlying="SPY", expiry=expiry, right=right, strike=Decimal(strike)
    )
    assert parse_option_symbol(build_option_symbol("SPY", expiry, right, Decimal(strike))) == original


@pytest.mark.parametrize("underlying", ["A", "SPY", "QQQ", "GOOGL", "BRKB"])
def test_round_trip_across_root_lengths(underlying):
    symbol = build_option_symbol(underlying, date(2026, 9, 4), "P", Decimal("100"))
    assert parse_option_symbol(symbol).underlying == underlying


def test_build_of_parse_round_trips():
    symbol = "SPY260904P00640000"
    contract = parse_option_symbol(symbol)
    assert contract.symbol == symbol


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "SPY",
        "SPY250127X00608000",  # invalid right
        "SPY251327C00608000",  # month 13
        "SPY250132C00608000",  # day 32
        "SPY250127C0060800",  # 7-digit strike
        "SPY250127C000608000",  # 9-digit strike
        "SPY25012C00608000",  # short date
        "SPY250127C00000000",  # zero strike
        "1PY250127C00608000",  # numeric root
        "TOOLONGX250127C00608000",  # root over 6 chars
    ],
)
def test_malformed_symbols_are_rejected(bad):
    with pytest.raises(OCCError):
        parse_option_symbol(bad)
    assert not is_option_symbol(bad)


@pytest.mark.parametrize("strike", ["0", "-5", "608.0001", "100000"])
def test_unrepresentable_strikes_are_rejected(strike):
    with pytest.raises(OCCError):
        build_option_symbol("SPY", date(2026, 9, 4), "P", Decimal(strike))


def test_invalid_right_rejected_on_build():
    with pytest.raises(OCCError):
        build_option_symbol("SPY", date(2026, 9, 4), "X", Decimal("100"))


def test_non_date_expiry_rejected():
    with pytest.raises(OCCError):
        build_option_symbol("SPY", "2026-09-04", "P", Decimal("100"))


def test_symbols_are_normalised_to_upper_case():
    assert parse_option_symbol("spy250127c00608000").underlying == "SPY"
