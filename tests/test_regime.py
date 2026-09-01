"""MarketDNA regime and permission tests (spec sections 16-18).

The mandatory evaluation order is the point: EVENT outranks MOMENTUM outranks
COMPRESSION outranks INCOME, and the first match wins.
"""

from __future__ import annotations

import pytest

from src.marketdna.indicators import Signals
from src.marketdna.regime import classify, permissions_for
from src.types import Regime


def signals(
    *,
    adx: float = 10.0,
    iv_rank: float = 50.0,
    hours_to_event: float = 1e9,
    event_name: str = "",
) -> Signals:
    return Signals(
        ema20=400.0,
        ema50=398.0,
        adx=adx,
        realized_vol=0.12,
        realized_vol_median=0.13,
        implied_vol=0.15,
        iv_rank=iv_rank,
        hours_to_next_event=hours_to_event,
        next_event_name=event_name,
    )


# --- classification ----------------------------------------------------------


def test_income_is_the_default(config):
    regime, reasons = classify(signals(), config)
    assert regime is Regime.INCOME
    assert "no_event_in_window" in reasons
    assert "adx_below_threshold" in reasons


def test_momentum_at_the_adx_threshold(config):
    """ADX >= 25 is MOMENTUM; the boundary is inclusive."""
    assert classify(signals(adx=25.0), config)[0] is Regime.MOMENTUM
    assert classify(signals(adx=24.99), config)[0] is not Regime.MOMENTUM


def test_compression_below_the_iv_rank_threshold(config):
    """IV rank < 30 is COMPRESSION; the boundary is exclusive."""
    assert classify(signals(iv_rank=29.99), config)[0] is Regime.COMPRESSION
    assert classify(signals(iv_rank=30.0), config)[0] is Regime.INCOME


def test_event_at_the_window_boundary(config):
    assert classify(signals(hours_to_event=24.0), config)[0] is Regime.EVENT
    assert classify(signals(hours_to_event=24.01), config)[0] is Regime.INCOME


# --- mandatory evaluation order ----------------------------------------------


def test_event_outranks_momentum(config):
    regime, _ = classify(signals(adx=80.0, hours_to_event=2.0, event_name="NFP"), config)
    assert regime is Regime.EVENT


def test_event_outranks_compression(config):
    regime, _ = classify(signals(iv_rank=5.0, hours_to_event=2.0), config)
    assert regime is Regime.EVENT


def test_event_outranks_everything(config):
    regime, _ = classify(signals(adx=90.0, iv_rank=1.0, hours_to_event=0.5), config)
    assert regime is Regime.EVENT


def test_momentum_outranks_compression(config):
    regime, _ = classify(signals(adx=40.0, iv_rank=5.0), config)
    assert regime is Regime.MOMENTUM


def test_event_reason_names_the_event(config):
    _, reasons = classify(signals(hours_to_event=3.0, event_name="NFP"), config)
    assert any("NFP" in r for r in reasons)


# --- permission contract (spec section 18) -----------------------------------


def test_income_permits_both_defined_risk_structures(config):
    permission = permissions_for(signals(), config)
    assert permission.regime == "INCOME"
    assert set(permission.allowed_strategies) == {"put_credit_spread", "iron_condor"}
    assert permission.max_lots == 1


def test_compression_still_permits_entry(config):
    permission = permissions_for(signals(iv_rank=10.0), config)
    assert permission.regime == "COMPRESSION"
    assert permission.max_lots == 1
    assert permission.allowed_strategies


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"adx": 40.0}, "MOMENTUM"),
        ({"hours_to_event": 2.0}, "EVENT"),
    ],
)
def test_blocked_regimes_permit_nothing(config, kwargs, expected):
    permission = permissions_for(signals(**kwargs), config)
    assert permission.regime == expected
    assert permission.allowed_strategies == ()
    assert permission.max_lots == 0


def test_blocked_regime_permits_no_structure(config):
    permission = permissions_for(signals(adx=40.0), config)
    assert not permission.permits("put_credit_spread")
    assert not permission.permits("iron_condor")


def test_permission_dict_has_exactly_the_contract_keys(config):
    """Spec section 18: MarketDNA may output this and nothing else."""
    assert set(permissions_for(signals(), config).as_dict()) == {
        "regime",
        "allowed_strategies",
        "max_lots",
        "reasons",
    }


def test_permission_never_exceeds_global_max_lots(config):
    for kwargs in ({}, {"iv_rank": 10.0}, {"adx": 40.0}, {"hours_to_event": 1.0}):
        assert permissions_for(signals(**kwargs), config).max_lots <= config.max_lots


def test_permission_reasons_are_always_populated(config):
    for kwargs in ({}, {"iv_rank": 10.0}, {"adx": 40.0}, {"hours_to_event": 1.0}):
        assert permissions_for(signals(**kwargs), config).reasons


def test_marketdna_never_names_a_strike_or_expiry(config):
    """MarketDNA emits permissions, never an order. Nothing tradeable leaks out.

    The only free-form field is `reasons`; it must carry signal readings, never
    a contract. `allowed_strategies` is checked separately against the
    allow-list, so a strategy name containing the word "credit" is not a leak.
    """
    from src.types import ALLOWED_STRUCTURES

    permission = permissions_for(signals(), config)
    assert set(permission.allowed_strategies) <= ALLOWED_STRUCTURES

    reasons_text = " ".join(permission.reasons).lower()
    for forbidden in ("strike", "expiry", "symbol", "leg", "qty", "lot", "$"):
        assert forbidden not in reasons_text, f"{forbidden!r} leaked into reasons"

    # No OCC symbol can appear anywhere in the permission payload.
    import re

    assert not re.search(r"[A-Z]{1,6}\d{6}[CP]\d{8}", repr(permission.as_dict()))
