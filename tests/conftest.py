"""Shared fixtures. Every test builds tickets through these helpers so a change
to the ticket contract breaks one place, not thirty."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker.occ import build_option_symbol
from src.config import load_config
from src.types import Account, Book, Leg, Permission, Quote, Ticket

NOW = datetime(2026, 9, 1, 15, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
EXPIRY = TODAY + timedelta(days=4)  # 4 DTE: inside the effective 2-7 window


@pytest.fixture(scope="session")
def config():
    return load_config("config/config.yaml")


def sym(strike: str, right: str = "P", expiry: date = EXPIRY) -> str:
    return build_option_symbol("SPY", expiry, right, Decimal(strike))


def quote(symbol: str, bid: float, ask: float, age_ms: float = 100.0) -> Quote:
    return Quote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        ts=NOW - timedelta(milliseconds=age_ms),
    )


def pcs_legs(short_strike: str = "640", long_strike: str = "635", expiry: date = EXPIRY):
    return (
        Leg(sym(short_strike, "P", expiry), "sell", "sell_to_open", 1),
        Leg(sym(long_strike, "P", expiry), "buy", "buy_to_open", 1),
    )


def condor_legs(expiry: date = EXPIRY):
    return (
        Leg(sym("630", "P", expiry), "buy", "buy_to_open", 1),
        Leg(sym("635", "P", expiry), "sell", "sell_to_open", 1),
        Leg(sym("650", "C", expiry), "sell", "sell_to_open", 1),
        Leg(sym("655", "C", expiry), "buy", "buy_to_open", 1),
    )


def quotes_for(legs, credit_target: float = 1.00, spread: float = 0.02) -> dict[str, Quote]:
    """Build penny-wide quotes whose net credit lands on `credit_target`.

    Long legs are priced near zero and the short legs carry the credit, which
    keeps the arithmetic obvious when a test asserts on the recalculated value.
    """
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    long_mid = 0.20
    short_mid = (credit_target + long_mid * len(longs)) / len(shorts)

    out: dict[str, Quote] = {}
    for leg in shorts:
        out[leg.symbol] = quote(leg.symbol, short_mid - spread / 2, short_mid + spread / 2)
    for leg in longs:
        out[leg.symbol] = quote(leg.symbol, long_mid - spread / 2, long_mid + spread / 2)
    return out


def make_ticket(
    legs=None,
    *,
    structure_type: str = "put_credit_spread",
    credit: float = 1.00,
    width: float = 5.0,
    expiry: date = EXPIRY,
    dte: int | None = None,
    regime: str = "INCOME",
    proposed_lots: int = 1,
    max_loss: float | None = None,
    ticket_id: str = "t-1",
) -> Ticket:
    legs = legs if legs is not None else pcs_legs(expiry=expiry)
    dte = dte if dte is not None else (expiry - TODAY).days
    max_loss = max_loss if max_loss is not None else (width - credit) * 100 * proposed_lots
    return Ticket(
        ticket_id=ticket_id,
        underlying="SPY",
        structure_type=structure_type,
        expiry=expiry.isoformat(),
        dte=dte,
        legs=legs,
        credit_mid=credit,
        width=width,
        max_loss=max_loss,
        short_delta=-0.18,
        quote_age_ms=100,
        regime=regime,
        proposed_lots=proposed_lots,
    )


def make_book(legs=None, credit: float = 1.00, **overrides) -> Book:
    legs = legs if legs is not None else pcs_legs()
    defaults = dict(
        open_structures=(),
        day_open_risk=0.0,
        quotes=quotes_for(legs, credit_target=credit),
        now=NOW,
        market_open=True,
        minutes_to_close=180,
    )
    defaults.update(overrides)
    return Book(**defaults)


def make_account(**overrides) -> Account:
    defaults = dict(
        account_id="DEV-1",
        allowed_account_id="DEV-1",
        equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        options_level=3,
        start_of_day_equity=100_000.0,
        high_water_mark=100_000.0,
    )
    defaults.update(overrides)
    return Account(**defaults)


def make_permission(
    regime: str = "INCOME",
    strategies=("put_credit_spread", "iron_condor"),
    max_lots: int = 1,
) -> Permission:
    return Permission(
        regime=regime,
        allowed_strategies=tuple(strategies),
        max_lots=max_lots,
        reasons=("test",),
    )


@pytest.fixture
def allow_case(config):
    """A ticket/book/account/permission set that must produce ALLOW.

    Every DENY test mutates exactly one dimension of this, so a failure points
    at the check under test rather than at fixture drift.
    """
    legs = pcs_legs()
    return dict(
        ticket=make_ticket(legs),
        book=make_book(legs),
        account=make_account(),
        regime=make_permission(),
        config=config,
    )
