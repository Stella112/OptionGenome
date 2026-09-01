"""Paper-only and account-isolation tests (spec sections 3 and 7).

These invariants are the ones that must never regress: no live trading, and no
mode in which an unknown account is accepted.
"""

from __future__ import annotations

import pytest

from src.safety import (
    LIVE_TRADE_FLAGS,
    ExecutionMode,
    Mode,
    SafetyViolation,
    assert_no_live_args,
    assert_no_live_flags,
    assert_options_level,
    assert_paper_host,
    assert_paper_only,
)

DEV_ENV = {"MODE": "development", "DEV_ACCOUNT_ID": "DEV-1", "JUDGE_ACCOUNT_ID": "JUDGE-9"}
JUDGE_ENV = {"MODE": "judging", "DEV_ACCOUNT_ID": "DEV-1", "JUDGE_ACCOUNT_ID": "JUDGE-9"}


# --- paper host --------------------------------------------------------------


def test_paper_host_is_accepted():
    assert_paper_host("https://paper-api.alpaca.markets")


@pytest.mark.parametrize(
    "host",
    [
        "https://api.alpaca.markets",  # the live endpoint
        "https://paper-api.alpaca.markets.evil.com",
        "http://paper-api.alpaca.markets",  # not https
        "https://broker-api.alpaca.markets",
        "",
    ],
)
def test_non_paper_hosts_are_rejected(host):
    with pytest.raises(SafetyViolation):
        assert_paper_host(host)


def test_live_endpoint_is_named_in_the_error():
    with pytest.raises(SafetyViolation, match="never trades live"):
        assert_paper_host("https://api.alpaca.markets")


# --- live flags --------------------------------------------------------------


@pytest.mark.parametrize("flag", LIVE_TRADE_FLAGS)
@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "on", "live"])
def test_live_flags_are_rejected(flag, value):
    with pytest.raises(SafetyViolation, match=flag):
        assert_no_live_flags({flag: value})


@pytest.mark.parametrize("value", ["false", "0", "no", "", "off"])
def test_falsy_live_flags_are_permitted(value):
    assert_no_live_flags({"ALPACA_LIVE_TRADE": value})


def test_clean_environment_passes():
    assert_no_live_flags({"MODE": "development"})


# --- forbidden arguments -----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["order", "submit", "--live"],
        ["order", "submit", "--LIVE"],
        ["--production"],
        ["order", "--prod=true"],
        ["--real"],
    ],
)
def test_live_arguments_are_rejected(argv):
    with pytest.raises(SafetyViolation):
        assert_no_live_args(argv)


def test_ordinary_arguments_are_permitted():
    assert_no_live_args(["order", "submit", "--order-class", "mleg", "--type", "limit"])


def test_substring_of_a_legitimate_arg_is_not_a_false_positive():
    """--live-preview is not --live; matching must be exact or key=value."""
    assert_no_live_args(["--liveness-probe", "--delivery"])


# --- combined gate -----------------------------------------------------------


def test_full_paper_gate_passes_on_a_clean_setup():
    assert_paper_only("https://paper-api.alpaca.markets", ["order", "submit"], {"MODE": "development"})


def test_full_paper_gate_fails_on_a_live_host():
    with pytest.raises(SafetyViolation):
        assert_paper_only("https://api.alpaca.markets", [], {})


def test_full_paper_gate_fails_on_a_live_flag():
    with pytest.raises(SafetyViolation):
        assert_paper_only("https://paper-api.alpaca.markets", [], {"ALPACA_LIVE_TRADE": "true"})


# --- execution mode and account isolation (spec section 3) ------------------


def test_development_mode_allows_the_dev_account():
    ExecutionMode.from_env(DEV_ENV).assert_account("DEV-1")


def test_judging_mode_allows_the_judge_account():
    ExecutionMode.from_env(JUDGE_ENV).assert_account("JUDGE-9")


def test_development_mode_rejects_the_judging_account():
    with pytest.raises(SafetyViolation, match="account assertion failed"):
        ExecutionMode.from_env(DEV_ENV).assert_account("JUDGE-9")


def test_judging_mode_rejects_the_development_account():
    with pytest.raises(SafetyViolation, match="account assertion failed"):
        ExecutionMode.from_env(JUDGE_ENV).assert_account("DEV-1")


def test_unknown_account_is_rejected_in_every_mode():
    for env in (DEV_ENV, JUDGE_ENV):
        with pytest.raises(SafetyViolation):
            ExecutionMode.from_env(env).assert_account("SOMEONE-ELSE")


def test_empty_account_is_rejected():
    with pytest.raises(SafetyViolation, match="no account id"):
        ExecutionMode.from_env(DEV_ENV).assert_account("")


@pytest.mark.parametrize("mode", ["", "live", "prod", "paper", "dev", "judge", "yolo"])
def test_unknown_mode_is_rejected(mode):
    with pytest.raises(SafetyViolation, match="MODE must be one of"):
        ExecutionMode.from_env({"MODE": mode, "DEV_ACCOUNT_ID": "DEV-1"})


def test_mode_requires_its_account_id():
    with pytest.raises(SafetyViolation, match="DEV_ACCOUNT_ID"):
        ExecutionMode.from_env({"MODE": "development"})
    with pytest.raises(SafetyViolation, match="JUDGE_ACCOUNT_ID"):
        ExecutionMode.from_env({"MODE": "judging", "DEV_ACCOUNT_ID": "DEV-1"})


def test_identical_dev_and_judge_accounts_are_rejected():
    """The judging account is never used for development."""
    with pytest.raises(SafetyViolation, match="same account"):
        ExecutionMode.from_env(
            {"MODE": "development", "DEV_ACCOUNT_ID": "SAME", "JUDGE_ACCOUNT_ID": "SAME"}
        )


def test_mode_parsing_is_case_insensitive():
    assert ExecutionMode.from_env({**DEV_ENV, "MODE": "DEVELOPMENT"}).mode is Mode.DEVELOPMENT


# --- options level -----------------------------------------------------------


def test_options_level_3_is_accepted():
    assert_options_level(3)


@pytest.mark.parametrize("level", [0, 1, 2])
def test_insufficient_options_level_is_rejected(level):
    with pytest.raises(SafetyViolation, match="options level"):
        assert_options_level(level)
