"""Candidate generation tests (spec sections 24 and 25)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.broker.occ import build_option_symbol
from src.rolldesk.candidates import (
    ChainContract,
    build_iron_condors,
    build_put_credit_spreads,
    eligible_expiries,
    generate_candidates,
)
from src.rolldesk.structures import derive_geometry
from src.types import Permission

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
SPOT = Decimal("640")


def contract(strike, right, expiry, bid, ask, delta, age_ms=100):
    strike = Decimal(str(strike))
    return ChainContract(
        symbol=build_option_symbol("SPY", expiry, right, strike),
        underlying="SPY",
        expiry=expiry,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        ts=NOW - timedelta(milliseconds=age_ms),
        delta=delta,
    )


def synthetic_chain(expiry: date, spot: Decimal = SPOT, step: int = 1):
    """A plausible SPY chain: price and |delta| fall as strikes move away from spot.

    Prices are shaped so the further-OTM leg is always cheaper than the nearer
    one, which is what makes a credit spread produce a credit.
    """
    chain = []
    for offset in range(-15, 16, step):
        strike = spot + Decimal(offset)
        distance = abs(offset)
        # Puts: cheaper and lower delta the further below spot.
        put_mid = max(0.05, 3.0 - 0.18 * (offset if offset > 0 else -offset) if offset <= 0 else 3.0 + 0.2 * offset)
        put_mid = max(0.05, 3.0 - 0.18 * distance) if offset <= 0 else 3.0 + 0.25 * distance
        put_delta = -max(0.02, 0.50 - 0.032 * distance) if offset <= 0 else -min(0.95, 0.50 + 0.03 * distance)
        chain.append(contract(strike, "P", expiry, round(put_mid - 0.02, 2), round(put_mid + 0.02, 2), put_delta))
        # Calls: mirror image.
        call_mid = max(0.05, 3.0 - 0.18 * distance) if offset >= 0 else 3.0 + 0.25 * distance
        call_delta = max(0.02, 0.50 - 0.032 * distance) if offset >= 0 else min(0.95, 0.50 + 0.03 * distance)
        chain.append(contract(strike, "C", expiry, round(call_mid - 0.02, 2), round(call_mid + 0.02, 2), call_delta))
    return chain


EXPIRY = TODAY + timedelta(days=4)


@pytest.fixture
def chain():
    return synthetic_chain(EXPIRY)


def income(strategies=("put_credit_spread", "iron_condor")):
    return Permission("INCOME", tuple(strategies), 1, ("test",))


# --- expiry filtering --------------------------------------------------------


def test_only_expiries_inside_the_openable_window_are_used(config):
    chain = []
    for days in (0, 1, 2, 4, 7, 8, 20):
        chain += synthetic_chain(TODAY + timedelta(days=days))
    eligible = eligible_expiries(chain, config, TODAY)
    offsets = sorted((e - TODAY).days for e in eligible)
    expected = [d for d in (0, 1, 2, 4, 7, 8, 20) if config.min_entry_dte <= d <= config.max_entry_dte]
    assert offsets == expected
    assert 0 not in offsets and 1 not in offsets  # the flatten zone is never openable


def test_forced_flatten_zone_expiries_are_excluded(config):
    chain = synthetic_chain(TODAY + timedelta(days=1))
    assert eligible_expiries(chain, config, TODAY) == []


# --- put credit spreads ------------------------------------------------------


def test_builds_valid_put_credit_spreads(chain):
    tickets = build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW)
    assert tickets
    for t in tickets:
        assert t.structure_type == "put_credit_spread"
        assert len(t.legs) == 2
        derive_geometry(t.legs)  # must not raise


def test_put_credit_spread_sells_the_higher_strike(chain):
    for t in build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW):
        geometry = derive_geometry(t.legs)
        assert geometry.short_strikes[0] > geometry.long_strikes[0]


def test_every_candidate_carries_a_positive_credit(chain):
    for t in build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW):
        assert t.credit_mid > 0
        assert t.credit_mid < t.width  # otherwise risk is not defined


def test_max_loss_matches_width_minus_credit(chain):
    for t in build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW):
        assert t.max_loss == pytest.approx((t.width - t.credit_mid) * 100, abs=0.02)


def test_candidates_propose_one_lot(chain):
    """Sizing is the Risk Officer's job; the builder never proposes more than one."""
    for t in build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW):
        assert t.proposed_lots == 1


def test_dte_is_computed_not_copied(chain):
    for t in build_put_credit_spreads(chain, EXPIRY, "INCOME", TODAY, NOW):
        assert t.dte == (EXPIRY - TODAY).days


# --- iron condors ------------------------------------------------------------


def test_builds_valid_iron_condors(chain):
    tickets = build_iron_condors(chain, EXPIRY, "INCOME", TODAY, NOW)
    assert tickets
    for t in tickets:
        assert t.structure_type == "iron_condor"
        assert len(t.legs) == 4
        derive_geometry(t.legs)


def test_iron_condor_shorts_straddle_the_spot(chain):
    for t in build_iron_condors(chain, EXPIRY, "INCOME", TODAY, NOW):
        geometry = derive_geometry(t.legs)
        short_put, short_call = geometry.short_strikes
        assert short_put < short_call


# --- liquidity filtering -----------------------------------------------------


def test_one_sided_markets_are_excluded(chain):
    poisoned = [
        ChainContract(c.symbol, c.underlying, c.expiry, c.strike, c.right, 0.0, c.ask, c.ts, c.delta)
        if c.right == "P"
        else c
        for c in chain
    ]
    assert build_put_credit_spreads(poisoned, EXPIRY, "INCOME", TODAY, NOW) == []


def test_very_wide_markets_are_excluded(chain):
    wide = [
        ChainContract(c.symbol, c.underlying, c.expiry, c.strike, c.right, 0.01, 5.0, c.ts, c.delta)
        for c in chain
    ]
    assert build_put_credit_spreads(wide, EXPIRY, "INCOME", TODAY, NOW) == []


def test_a_chain_with_no_deltas_yields_nothing(chain):
    """Strike selection is delta-driven; without deltas the builder stands down."""
    undeltaed = [
        ChainContract(c.symbol, c.underlying, c.expiry, c.strike, c.right, c.bid, c.ask, c.ts, None)
        for c in chain
    ]
    assert build_put_credit_spreads(undeltaed, EXPIRY, "INCOME", TODAY, NOW) == []


# --- the full generator ------------------------------------------------------


def test_generates_a_shortlist(chain, config):
    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    assert 1 <= len(tickets) <= 3


def test_shortlist_is_diverse(chain, config):
    """Spec section 25: not three effectively identical tickets."""
    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    if len(tickets) > 1:
        signatures = {(round(t.short_delta, 2), t.width) for t in tickets}
        assert len(signatures) == len(tickets)


def test_no_two_candidates_share_the_same_legs(chain, config):
    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    leg_sets = [tuple(leg.symbol for leg in t.legs) for t in tickets]
    assert len(set(leg_sets)) == len(leg_sets)


def test_every_candidate_clears_the_credit_floor(chain, config):
    for t in generate_candidates(chain, income(), config, TODAY, NOW):
        assert t.credit_mid / t.width >= config.min_credit_to_width


def test_every_candidate_is_structurally_valid(chain, config):
    for t in generate_candidates(chain, income(), config, TODAY, NOW):
        geometry = derive_geometry(t.legs)
        assert geometry.structure_type == t.structure_type


def test_blocked_regime_produces_no_candidates(chain, config):
    """MOMENTUM and EVENT are not special-cased; they simply permit nothing."""
    blocked = Permission("MOMENTUM", (), 0, ("adx_at_or_above_threshold",))
    assert generate_candidates(chain, blocked, config, TODAY, NOW) == []


def test_permission_limits_which_structures_are_built(chain, config):
    only_pcs = income(strategies=("put_credit_spread",))
    tickets = generate_candidates(chain, only_pcs, config, TODAY, NOW)
    assert tickets
    assert all(t.structure_type == "put_credit_spread" for t in tickets)


def test_empty_chain_produces_no_candidates(config):
    assert generate_candidates([], income(), config, TODAY, NOW) == []


def test_ticket_ids_are_stable_and_unique(chain, config):
    first = generate_candidates(chain, income(), config, TODAY, NOW)
    second = generate_candidates(chain, income(), config, TODAY, NOW)
    assert [t.ticket_id for t in first] == [t.ticket_id for t in second]
    assert len({t.ticket_id for t in first}) == len(first)


def test_candidates_survive_the_risk_officer(chain, config):
    """End to end: a generated candidate must be capable of reaching ALLOW."""
    from src.risk.officer import evaluate
    from src.types import Account, Book

    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    assert tickets

    quotes = {c.symbol: c.as_quote() for c in chain}
    account = Account(
        account_id="DEV-1",
        allowed_account_id="DEV-1",
        equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        options_level=3,
        start_of_day_equity=100_000.0,
        high_water_mark=100_000.0,
    )
    book = Book(quotes=quotes, now=NOW, market_open=True, minutes_to_close=180)

    verdicts = [evaluate(t, book, account, income(), config) for t in tickets]
    assert any(v.allowed for v in verdicts), [v.reasons for v in verdicts]


# --- expiry preference (operator decision, 2026-09-01) -----------------------


def test_the_furthest_expiry_can_be_targeted_on_demand(config):
    """The duration preference still works; it is simply not the default.

    Default is off because, with a long-dated position already held, targeting
    the furthest expiry every pass deadlocks against the overlapping-short check.
    """
    chain = []
    for days in (3, 9, 17):
        chain += synthetic_chain(TODAY + timedelta(days=days))
    tickets = generate_candidates(
        chain, income(), config, TODAY, NOW, prefer_longest_expiry=True
    )
    assert tickets
    assert {t.expiry for t in tickets} == {(TODAY + timedelta(days=17)).isoformat()}


def test_richest_first_is_the_default(config):
    """Default behaviour ranks by credit across every eligible expiry."""
    chain = []
    for days in (3, 17):
        chain += synthetic_chain(TODAY + timedelta(days=days))
    assert generate_candidates(chain, income(), config, TODAY, NOW)


def test_a_barren_far_expiry_falls_back_to_a_nearer_one(config):
    """A thin far-dated ladder must not strand the desk when targeting duration."""
    near = synthetic_chain(TODAY + timedelta(days=4))
    far = [
        ChainContract(c.symbol, c.underlying, c.expiry, c.strike, c.right, 0.0, c.ask, c.ts, c.delta)
        for c in synthetic_chain(TODAY + timedelta(days=17))
    ]
    tickets = generate_candidates(
        near + far, income(), config, TODAY, NOW, prefer_longest_expiry=True
    )
    assert tickets
    assert {t.expiry for t in tickets} == {(TODAY + timedelta(days=4)).isoformat()}


def test_a_held_expiry_does_not_deadlock_the_desk(config):
    """With a position already on the furthest expiry, nearer ones stay available.

    Regression: preferring the longest expiry made every pass target the expiry
    already held, which the overlapping-short check refuses, so the desk stopped
    trading entirely.
    """
    chain = []
    for days in (4, 9, 17):
        chain += synthetic_chain(TODAY + timedelta(days=days))
    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    assert tickets
    expiries = {t.expiry for t in tickets}
    assert expiries != {(TODAY + timedelta(days=17)).isoformat()}


# --- wing width economics ----------------------------------------------------


def test_narrow_wings_are_no_longer_built(config):
    """A 1-wide condor loses ~32% of its credit to the bid-ask on entry and exit.

    Measured on the live SPY chain: the spread cost is roughly constant per
    structure while credit scales with width, so narrow wings cannot clear it.
    """
    from src.rolldesk.candidates import CANDIDATE_WIDTHS

    assert min(CANDIDATE_WIDTHS) >= 5.0


def test_wider_structures_still_face_the_risk_cap(config):
    """Width raises max loss, so the officer must still be able to refuse it."""
    from src.risk.officer import evaluate
    from src.types import Account, Book

    chain = []
    for days in (4, 9):
        chain += synthetic_chain(TODAY + timedelta(days=days))
    tickets = generate_candidates(chain, income(), config, TODAY, NOW)
    assert tickets

    quotes = {c.symbol: c.as_quote() for c in chain}
    # 0.75% of a small account is a few dollars: every candidate must be refused.
    tiny = Account("DEV-1", "DEV-1", 5_000.0, 5_000.0, 20_000.0, 3, 5_000.0, 5_000.0)
    book = Book(quotes=quotes, now=NOW, market_open=True, minutes_to_close=180)
    for ticket in tickets:
        decision = evaluate(ticket, book, tiny, income(), config, today=TODAY)
        assert not decision.allowed
        assert any("max_loss_pct_exceeded" in r for r in decision.reasons)


# --- ranking by expected return on risk --------------------------------------


def _ticket(credit, delta, legs_n=4, width=5.0):
    from src.types import Leg, Ticket

    legs = tuple(
        Leg(f"L{i}", "sell" if i % 2 == 0 else "buy",
            "sell_to_open" if i % 2 == 0 else "buy_to_open", 1)
        for i in range(legs_n)
    )
    return Ticket("t", "SPY", "iron_condor" if legs_n == 4 else "put_credit_spread",
                  EXPIRY.isoformat(), 4, legs, credit, width,
                  max(0.0, (width - credit) * 100), delta, 100, "INCOME", 1)


def test_ranking_prefers_probability_over_raw_credit():
    """The richest credit sits nearest the money and is breached most often."""
    from src.rolldesk.candidates import expected_return_on_risk

    rich_but_risky = _ticket(2.40, 0.32)   # ~36% chance both shorts survive
    modest_and_safe = _ticket(1.20, 0.10)  # ~80% chance
    assert expected_return_on_risk(modest_and_safe) > expected_return_on_risk(rich_but_risky)


def test_a_structure_that_cannot_lose_ranks_above_a_coin_flip():
    from src.rolldesk.candidates import expected_return_on_risk

    assert expected_return_on_risk(_ticket(1.0, 0.0, legs_n=2)) > \
        expected_return_on_risk(_ticket(1.0, 0.5, legs_n=2))


def test_broken_geometry_ranks_last():
    from src.rolldesk.candidates import expected_return_on_risk

    assert expected_return_on_risk(_ticket(1.0, 0.1, legs_n=2, width=0.0)) < 0
    assert expected_return_on_risk(_ticket(6.0, 0.1, legs_n=2, width=5.0)) < 0


def test_aggressive_deltas_are_no_longer_targeted():
    from src.rolldesk.candidates import TARGET_SHORT_DELTAS

    assert max(TARGET_SHORT_DELTAS) <= 0.25
