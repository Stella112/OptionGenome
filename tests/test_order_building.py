"""Order construction tests (spec sections 31, 32, 33).

Two things must hold: the order shape matches the spec (mleg, limit, day, never
market), and nothing reaches the broker that has not passed every gate.
"""

from __future__ import annotations

import json

import pytest

from src.broker.alpaca_cli import (
    ORDER_CLASS,
    REQUIRED_CAPTURES,
    AlpacaCLI,
    CLIUnavailable,
    LegsFormatUnverified,
    closing_legs,
    encode_legs,
    limit_price_for_credit,
    limit_price_to_close,
    load_cli_reference,
)
from src.safety import ExecutionMode, SafetyViolation
from src.types import Leg, RiskDecision

from .conftest import condor_legs, make_ticket, pcs_legs

DEV_ENV = {"MODE": "development", "DEV_ACCOUNT_ID": "DEV-1", "JUDGE_ACCOUNT_ID": "JUDGE-9"}
PAPER = "https://paper-api.alpaca.markets"


@pytest.fixture
def reference():
    return load_cli_reference("docs/cli-reference.txt")


def cli(reference, runner=None):
    return AlpacaCLI(
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        trading_host=PAPER,
        reference=reference,
        runner=runner,
    )


def allow(lots: int = 1) -> RiskDecision:
    return RiskDecision(decision="ALLOW", reasons=(), allowed_lots=lots)


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


# --- limit pricing (spec section 32) ----------------------------------------


def test_limit_price_shaves_mid_for_a_credit():
    assert limit_price_for_credit(1.00) == 0.95


def test_limit_price_is_always_positive():
    assert limit_price_for_credit(0.01) >= 0.01


def test_limit_price_rejects_a_non_credit():
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError):
            limit_price_for_credit(bad)


def test_closing_price_pays_up_from_mid():
    assert limit_price_to_close(1.00) == 1.05


def test_closing_price_of_a_worthless_structure_is_the_minimum_tick():
    assert limit_price_to_close(0.0) == 0.01


# --- leg encoding ------------------------------------------------------------


def test_encoded_legs_carry_the_four_schema_fields():
    payload = json.loads(encode_legs(pcs_legs()))
    assert len(payload) == 2
    for leg in payload:
        assert set(leg) == {"symbol", "side", "ratio_qty", "position_intent"}


def test_encoded_legs_preserve_order_and_intent():
    payload = json.loads(encode_legs(pcs_legs("640", "635")))
    assert payload[0]["position_intent"] == "sell_to_open"
    assert payload[1]["position_intent"] == "buy_to_open"


def test_alpaca_four_leg_maximum_is_enforced():
    legs = condor_legs() + (Leg("SPY260904C00660000", "buy", "buy_to_open", 1),)
    with pytest.raises(ValueError, match="at most 4 legs"):
        encode_legs(legs)


def test_empty_legs_rejected():
    with pytest.raises(ValueError):
        encode_legs([])


def test_closing_legs_mirror_the_open():
    closed = closing_legs(pcs_legs("640", "635"))
    assert closed[0].side == "buy" and closed[0].position_intent == "buy_to_close"
    assert closed[1].side == "sell" and closed[1].position_intent == "sell_to_close"


def test_closing_legs_keep_symbols_and_ratios():
    original = condor_legs()
    for before, after in zip(original, closing_legs(original)):
        assert before.symbol == after.symbol
        assert before.ratio_qty == after.ratio_qty


def test_closing_a_close_round_trips():
    original = pcs_legs()
    assert closing_legs(closing_legs(original)) == original


# --- order shape (spec section 31) ------------------------------------------


def test_submit_command_uses_mleg_limit_day(reference):
    command = cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1")
    assert argv_value(command.argv, "--order-class") == ORDER_CLASS
    assert argv_value(command.argv, "--type") == "limit"
    assert argv_value(command.argv, "--time-in-force") == "day"


def test_submit_command_is_never_a_market_order(reference):
    command = cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1")
    assert "market" not in command.argv
    assert "--limit-price" in command.argv


def test_parent_qty_is_the_allowed_lot_count_not_the_proposed(reference):
    """The officer sizes; the ticket's own proposal is not what ships."""
    ticket = make_ticket(proposed_lots=9)
    command = cli(reference).build_submit_command(ticket, allow(lots=1), "DEV-1")
    assert argv_value(command.argv, "--qty") == "1"
    assert command.lots == 1


def test_submit_command_carries_every_leg(reference):
    ticket = make_ticket(condor_legs(), structure_type="iron_condor")
    command = cli(reference).build_submit_command(ticket, allow(), "DEV-1")
    assert len(json.loads(argv_value(command.argv, "--legs"))) == 4


def test_client_order_id_is_deterministic_for_idempotency(reference):
    first = cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1")
    second = cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1")
    assert first.client_order_id == second.client_order_id
    assert len(first.client_order_id) <= 128


def test_close_command_flips_the_legs(reference):
    ticket = make_ticket()
    command = cli(reference).build_close_command(ticket, 1, 0.40, "DEV-1")
    payload = json.loads(argv_value(command.argv, "--legs"))
    assert payload[0]["position_intent"] == "buy_to_close"
    assert command.intent == "close"


def test_close_command_is_allowed_in_flatten_only(reference):
    """Reducing risk is the point of FLATTEN_ONLY."""
    command = cli(reference).build_close_command(
        make_ticket(), 1, 0.40, "DEV-1", system_state="FLATTEN_ONLY"
    )
    assert command.intent == "close"


def test_opening_is_not_allowed_in_flatten_only_without_an_allow(reference):
    denied = RiskDecision(decision="DENY", reasons=("drawdown_flatten_only",), allowed_lots=0)
    with pytest.raises(SafetyViolation):
        cli(reference).build_submit_command(
            make_ticket(), denied, "DEV-1", system_state="FLATTEN_ONLY"
        )


# --- gates (spec section 33) -------------------------------------------------


def test_denied_ticket_never_builds_a_command(reference):
    denied = RiskDecision(decision="DENY", reasons=("market_closed",), allowed_lots=0)
    with pytest.raises(SafetyViolation, match="was not allowed"):
        cli(reference).build_submit_command(make_ticket(), denied, "DEV-1")


def test_zero_lots_never_builds_a_command(reference):
    with pytest.raises(SafetyViolation, match="zero lots"):
        cli(reference).build_submit_command(make_ticket(), allow(lots=0), "DEV-1")


def test_wrong_account_never_builds_a_command(reference):
    with pytest.raises(SafetyViolation, match="account assertion failed"):
        cli(reference).build_submit_command(make_ticket(), allow(), "JUDGE-9")


def test_halted_system_never_builds_a_command(reference):
    with pytest.raises(SafetyViolation, match="may not write"):
        cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1", system_state="HALTED")


def test_live_host_never_builds_a_command(reference):
    live = AlpacaCLI(
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        trading_host="https://api.alpaca.markets",
        reference=reference,
    )
    with pytest.raises(SafetyViolation):
        live.build_submit_command(make_ticket(), allow(), "DEV-1")


def test_uncaptured_schema_never_builds_a_command():
    bare = AlpacaCLI(ExecutionMode.from_env(DEV_ENV), PAPER, reference=None)
    with pytest.raises(CLIUnavailable):
        bare.build_submit_command(make_ticket(), allow(), "DEV-1")


# --- the unverified --legs format -------------------------------------------


def test_submit_refuses_before_the_legs_format_is_verified(reference):
    """Spec section 4: no argument may be implemented from memory."""
    desk = cli(reference, runner=lambda argv: (0, "{}", ""))
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    with pytest.raises(LegsFormatUnverified, match="not been verified"):
        desk.submit(command, "DEV-1")


def test_dry_run_verification_unlocks_submission(reference):
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, '{"code":0,"error":"","status":200}', ""

    desk = cli(reference, runner=runner)
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")

    ok, _ = desk.verify_legs_format(command)
    assert ok
    assert "--dry-run" in calls[0]  # verification never sends a real order

    desk.submit(command, "DEV-1")
    assert "--dry-run" not in calls[1]


def test_verification_fails_when_unauthenticated(reference):
    desk = cli(
        reference,
        runner=lambda argv: (0, '{"error":"authentication required"}', ""),
    )
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    ok, detail = desk.verify_legs_format(command)
    assert not ok
    assert "authentication" in detail.lower()


def test_verification_fails_on_a_nonzero_exit(reference):
    desk = cli(reference, runner=lambda argv: (1, "", "unknown flag: --legs"))
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    ok, detail = desk.verify_legs_format(command)
    assert not ok
    assert "--legs" in detail


def test_failed_verification_leaves_submission_locked(reference):
    desk = cli(reference, runner=lambda argv: (1, "", "bad legs format"))
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    desk.verify_legs_format(command)
    with pytest.raises(LegsFormatUnverified):
        desk.submit(command, "DEV-1")


def test_submit_still_asserts_the_account_after_verification(reference):
    desk = cli(reference, runner=lambda argv: (0, '{"error":""}', ""))
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    desk.verify_legs_format(command)
    with pytest.raises(SafetyViolation, match="account assertion failed"):
        desk.submit(command, "JUDGE-9")


# --- limit price sign on the wire (the fill-leak bug) -----------------------


def test_an_opening_credit_goes_on_the_wire_negative(reference):
    """Alpaca: 'A negative value signifies a credit.' Sent positive, a credit
    limit became a debit cap that any credit satisfies, so every entry filled
    at whatever the market offered - 0.02 to 0.16 below the limit we set."""
    command = cli(reference).build_submit_command(make_ticket(credit=1.00), allow(), "DEV-1")
    wire = float(argv_value(command.argv, "--limit-price"))
    assert wire < 0
    assert wire == pytest.approx(-0.95)  # mid 1.00 less the 5% concession, negated
    assert command.limit_price == pytest.approx(0.95)  # magnitude kept for display
    assert command.wire_limit_price == pytest.approx(-0.95)


def test_a_closing_debit_goes_on_the_wire_positive(reference):
    command = cli(reference).build_close_command(make_ticket(), 1, 0.40, "DEV-1")
    wire = float(argv_value(command.argv, "--limit-price"))
    assert wire > 0
    assert wire == pytest.approx(0.42)
    assert command.wire_limit_price == pytest.approx(0.42)


def test_dry_run_preserves_the_signed_limit(reference):
    command = cli(reference).build_submit_command(make_ticket(), allow(), "DEV-1")
    assert command.with_dry_run().wire_limit_price == command.wire_limit_price
    assert argv_value(command.with_dry_run().argv, "--limit-price").startswith("-")


def test_a_credit_limit_is_never_zero_or_positive_on_the_wire(reference):
    """The minimum-tick floor must not flip the sign."""
    command = cli(reference).build_submit_command(make_ticket(credit=0.01), allow(), "DEV-1")
    assert float(argv_value(command.argv, "--limit-price")) <= -0.01
