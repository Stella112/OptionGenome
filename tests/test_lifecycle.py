"""Position lifecycle tests (spec section 14).

The ordering assertions matter most: a safety exit must beat a profit exit, and
nothing may be carried into settlement.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.rolldesk.lifecycle import LifecycleDecision, PositionState, decide, roll_replacement_failed
from src.types import Action

from .conftest import EXPIRY, NOW, TODAY, condor_legs, make_ticket, pcs_legs


def position(
    *,
    expiry: date = EXPIRY,
    entry_credit: float = 100.0,
    cost_to_close: float = 60.0,
    spot: float = 645.0,
    lots: int = 1,
    roll_count: int = 0,
    legs=None,
    structure_type: str = "put_credit_spread",
) -> PositionState:
    legs = legs if legs is not None else pcs_legs(expiry=expiry)
    return PositionState(
        ticket=make_ticket(legs, expiry=expiry, structure_type=structure_type),
        lots=lots,
        entry_credit=entry_credit,
        cost_to_close=cost_to_close,
        underlying_price=spot,
        opened_at=NOW,
        roll_count=roll_count,
    )


def action(pos, config, **kwargs) -> Action:
    return decide(pos, config, TODAY, **kwargs).action


# --- arithmetic --------------------------------------------------------------


def test_profit_captured():
    assert position(entry_credit=100.0, cost_to_close=40.0).profit_captured == pytest.approx(0.60)


def test_profit_captured_of_a_worthless_short_is_one():
    assert position(entry_credit=100.0, cost_to_close=0.0).profit_captured == 1.0


def test_loss_multiple():
    assert position(entry_credit=100.0, cost_to_close=250.0).loss_multiple == pytest.approx(2.5)


def test_pnl_can_be_negative():
    assert position(entry_credit=100.0, cost_to_close=180.0).pnl == pytest.approx(-80.0)


# --- forced flatten outranks everything (spec section 14) -------------------


def test_expired_position_is_expired(config):
    assert action(position(expiry=TODAY - timedelta(days=1)), config) is Action.EXPIRE


@pytest.mark.parametrize("days", [0, 1])
def test_flatten_zone_is_mandatory(config, days):
    """Never carried into expiration or settlement."""
    assert action(position(expiry=TODAY + timedelta(days=days)), config) is Action.FLATTEN


def test_flatten_beats_a_profitable_position(config):
    """A winner inside the flatten zone is still flattened."""
    pos = position(expiry=TODAY + timedelta(days=1), entry_credit=100.0, cost_to_close=5.0)
    assert pos.profit_captured > config.tp_frac_of_credit
    assert action(pos, config) is Action.FLATTEN


def test_flatten_beats_a_losing_position(config):
    pos = position(expiry=TODAY + timedelta(days=1), entry_credit=100.0, cost_to_close=500.0)
    assert action(pos, config) is Action.FLATTEN


def test_flatten_reason_names_the_zone(config):
    decision = decide(position(expiry=TODAY + timedelta(days=1)), config, TODAY)
    assert any("forced_flatten_zone" in r for r in decision.reasons)


# --- flatten-only mode -------------------------------------------------------


def test_flatten_only_mode_closes_everything(config):
    assert action(position(), config, flatten_only=True) is Action.FLATTEN


def test_flatten_only_beats_take_profit(config):
    pos = position(entry_credit=100.0, cost_to_close=10.0)
    assert action(pos, config, flatten_only=True) is Action.FLATTEN


# --- stop loss ---------------------------------------------------------------


def test_stop_loss_at_the_defend_multiple(config):
    """defend_mult is 2.0: cost to close at twice the credit exits."""
    assert action(position(entry_credit=100.0, cost_to_close=200.0), config) is Action.FLATTEN


def test_just_under_the_stop_does_not_exit(config):
    assert action(position(entry_credit=100.0, cost_to_close=199.0), config) is not Action.FLATTEN


def test_stop_loss_beats_a_breached_short(config):
    """Once the stop is hit, defending is no longer the right answer."""
    pos = position(entry_credit=100.0, cost_to_close=300.0, spot=600.0)
    assert pos.breached_short() is not None
    assert action(pos, config) is Action.FLATTEN


# --- take profit -------------------------------------------------------------


def test_take_profit_at_half_the_credit(config):
    """tp_frac_of_credit is 0.50."""
    assert action(position(entry_credit=100.0, cost_to_close=50.0), config) is Action.TAKE_PROFIT


def test_take_profit_above_the_target(config):
    assert action(position(entry_credit=100.0, cost_to_close=10.0), config) is Action.TAKE_PROFIT


def test_just_under_the_target_holds(config):
    assert action(position(entry_credit=100.0, cost_to_close=51.0), config) is Action.HOLD


# --- breached shorts ---------------------------------------------------------


def test_put_short_breached_when_spot_falls_through_it(config):
    pos = position(spot=639.0)  # short put is 640
    assert pos.breached_short() == Decimal("640")
    assert action(pos, config) is Action.DEFEND


def test_put_short_not_breached_above_the_strike(config):
    assert position(spot=641.0).breached_short() is None


def test_call_short_breached_when_spot_rises_through_it(config):
    pos = position(legs=condor_legs(), structure_type="iron_condor", spot=651.0)
    assert pos.breached_short() == Decimal("650")
    assert action(pos, config) is Action.DEFEND


def test_condor_unbreached_between_the_shorts(config):
    pos = position(legs=condor_legs(), structure_type="iron_condor", spot=642.0)
    assert pos.breached_short() is None


def test_defend_reason_names_the_strike_and_spot(config):
    decision = decide(position(spot=639.0), config, TODAY)
    assert any("short_strike_breached" in r for r in decision.reasons)
    assert any("spot=" in r for r in decision.reasons)


# --- rolling -----------------------------------------------------------------


def test_rolls_one_day_before_the_flatten_zone(config):
    """dte 2 with force_flatten_dte 1: extend rather than be force-closed tomorrow."""
    assert action(position(expiry=TODAY + timedelta(days=2)), config) is Action.ROLL


def test_does_not_roll_twice(config):
    pos = position(expiry=TODAY + timedelta(days=2), roll_count=1)
    assert action(pos, config) is Action.HOLD


def test_roll_is_flagged_as_needing_a_fresh_ticket(config):
    decision = decide(position(expiry=TODAY + timedelta(days=2)), config, TODAY)
    assert decision.needs_new_ticket


def test_a_rejected_roll_closes_the_existing_position():
    """Spec: if the replacement fails validation, close the existing instead."""
    decision = roll_replacement_failed(position())
    assert decision.action is Action.FLATTEN
    assert "roll_replacement_denied" in decision.reasons


def test_take_profit_beats_the_roll_window(config):
    pos = position(expiry=TODAY + timedelta(days=2), entry_credit=100.0, cost_to_close=20.0)
    assert action(pos, config) is Action.TAKE_PROFIT


# --- holding -----------------------------------------------------------------


def test_healthy_mid_life_position_holds(config):
    assert action(position(expiry=TODAY + timedelta(days=5)), config) is Action.HOLD


def test_hold_reason_reports_the_state(config):
    decision = decide(position(expiry=TODAY + timedelta(days=5)), config, TODAY)
    assert any("captured=" in r for r in decision.reasons)
    assert any("loss_multiple=" in r for r in decision.reasons)


def test_closed_market_still_flattens_inside_the_zone(config):
    """A safety exit is not deferred because the session is shut."""
    pos = position(expiry=TODAY + timedelta(days=1))
    assert action(pos, config, market_open=False) is Action.FLATTEN


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    "act,closes",
    [
        (Action.TAKE_PROFIT, True),
        (Action.FLATTEN, True),
        (Action.EXPIRE, True),
        (Action.HOLD, False),
        (Action.DEFEND, False),
        (Action.ROLL, False),
    ],
)
def test_closing_actions_are_identified(act, closes):
    assert LifecycleDecision(act, ()).closes_position is closes


def test_every_decision_carries_a_reason(config):
    cases = [
        position(expiry=TODAY - timedelta(days=1)),
        position(expiry=TODAY + timedelta(days=1)),
        position(entry_credit=100.0, cost_to_close=300.0),
        position(entry_credit=100.0, cost_to_close=10.0),
        position(spot=600.0),
        position(expiry=TODAY + timedelta(days=2)),
        position(expiry=TODAY + timedelta(days=5)),
    ]
    for pos in cases:
        assert decide(pos, config, TODAY).reasons


def test_decisions_are_deterministic(config):
    pos = position()
    assert decide(pos, config, TODAY) == decide(pos, config, TODAY)


# --- unknown spot ------------------------------------------------------------


def test_unknown_spot_is_not_a_breach(config):
    """A missing price is not a price of zero.

    Treating it as zero puts spot below every put strike, so one failed quote
    lookup would report every open position as breached at once.
    """
    assert position(spot=0.0).breached_short() is None
    assert action(position(spot=0.0), config) is not Action.DEFEND


def test_unknown_spot_still_permits_every_other_rule(config):
    assert action(position(spot=0.0, expiry=TODAY + timedelta(days=1)), config) is Action.FLATTEN
    assert action(position(spot=0.0, entry_credit=100.0, cost_to_close=10.0), config) is Action.TAKE_PROFIT
    assert action(position(spot=0.0, entry_credit=100.0, cost_to_close=300.0), config) is Action.FLATTEN
