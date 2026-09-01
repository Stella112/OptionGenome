"""Startup readiness gate tests (spec section 8).

The properties that matter: all twenty checks run, one failure means HALTED, and
a HALTED system never reports READY.
"""

from __future__ import annotations

import pytest

from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities
from src.journal import Journal
from src.startup import run_gate
from src.types import SystemState

from .test_broker_gates import FULL_TOOLSET

GOOD_ENV = {
    "MODE": "development",
    "DEV_ACCOUNT_ID": "DEV-1",
    "JUDGE_ACCOUNT_ID": "JUDGE-9",
    "ALPACA_API_KEY_ID": "key",
    "ALPACA_API_SECRET_KEY": "secret",
    "ALPACA_TRADING_HOST": "https://paper-api.alpaca.markets",
}

GOOD_ACCOUNT = {
    "account_id": "DEV-1",
    "options_level": 3,
    "buying_power": 100_000.0,
    "equity": 100_000.0,
}


#: Distinguishes "caller did not specify an account" from "caller passed None
#: on purpose to simulate an account that was never read".
UNSET = object()


def gate(env=None, account=UNSET, mcp=None, tmp_path=None):
    return run_gate(
        env={**GOOD_ENV, **(env or {})},
        account_snapshot=dict(GOOD_ACCOUNT) if account is UNSET else account,
        mcp=mcp,
        journal=Journal(tmp_path / "audit.jsonl") if tmp_path else None,
    )


def outcome(result, name):
    return next(o for o in result.outcomes if o.name == name)


# --- structure ---------------------------------------------------------------


def test_gate_runs_all_twenty_checks():
    assert len(gate().outcomes) == 20


def test_check_indices_are_sequential():
    assert [o.index for o in gate().outcomes] == list(range(1, 21))


def test_every_check_reports_a_detail():
    assert all(o.detail for o in gate().outcomes)


# --- the current machine -----------------------------------------------------


def test_gate_halts_without_cli_and_mcp():
    """No Alpaca CLI, no MCP server: the desk must not start."""
    result = gate()
    assert result.state is SystemState.HALTED
    assert not result.passed
    failed = {o.name for o in result.failures}
    assert {"cli_available", "mcp_connection", "mcp_capabilities"} <= failed


def test_checks_independent_of_day_zero_already_pass():
    """The pure layers are provably healthy even while the gate is closed."""
    result = gate()
    for name in (
        "python_version",
        "config_valid",
        "no_live_flags",
        "events_calendar",
        "journal_writable",
        "risk_officer_selftest",
        "occ_selftest",
    ):
        assert outcome(result, name).passed, outcome(result, name)


def test_failures_are_actionable():
    """A failure must say what to do, not merely that something is wrong."""
    result = gate()
    assert "alpaca-mcp-server" in outcome(result, "mcp_connection").detail
    assert "not on PATH" in outcome(result, "cli_available").detail


# --- individual failure modes ------------------------------------------------


def test_live_host_halts_the_gate():
    result = gate({"ALPACA_TRADING_HOST": "https://api.alpaca.markets"})
    assert not outcome(result, "paper_endpoint").passed
    assert result.state is SystemState.HALTED


def test_live_flag_halts_the_gate():
    result = gate({"ALPACA_LIVE_TRADE": "true"})
    assert not outcome(result, "no_live_flags").passed


def test_missing_credentials_halt_the_gate():
    result = gate({"ALPACA_API_KEY_ID": ""})
    assert not outcome(result, "environment").passed


def test_unknown_mode_halts_the_gate():
    result = gate({"MODE": "yolo"})
    assert not outcome(result, "execution_mode").passed


def test_wrong_account_halts_the_gate():
    result = gate(account={**GOOD_ACCOUNT, "account_id": "JUDGE-9"})
    assert not outcome(result, "account_identity").passed


def test_insufficient_options_level_halts_the_gate():
    result = gate(account={**GOOD_ACCOUNT, "options_level": 2})
    assert not outcome(result, "options_level").passed


def test_zero_buying_power_halts_the_gate():
    result = gate(account={**GOOD_ACCOUNT, "buying_power": 0.0})
    assert not outcome(result, "buying_power").passed


def test_unread_account_halts_identity_checks():
    """The desk never assumes an account it has not actually read."""
    result = gate(account=None)
    for name in ("account_identity", "options_level", "buying_power"):
        assert not outcome(result, name).passed


def test_connected_mcp_passes_the_connection_check():
    mcp = AlpacaMCP(lambda name, params: None, discover_capabilities(FULL_TOOLSET))
    result = gate(mcp=mcp)
    assert outcome(result, "mcp_connection").passed
    assert outcome(result, "mcp_capabilities").passed


def test_incomplete_mcp_fails_capability_verification():
    mcp = AlpacaMCP(lambda name, params: None, discover_capabilities(["get_account_info"]))
    result = gate(mcp=mcp)
    assert outcome(result, "mcp_connection").passed
    assert not outcome(result, "mcp_capabilities").passed


# --- reporting ---------------------------------------------------------------


def test_report_names_the_final_state():
    assert "SYSTEM_STATE = HALTED" in gate().report()


def test_report_lists_every_check():
    report = gate().report()
    assert report.count("PASS") + report.count("FAIL") >= 20


def test_a_single_failure_prevents_ready():
    """READY requires all twenty; nineteen is not enough."""
    result = gate()
    assert result.failures
    assert result.state is not SystemState.READY
