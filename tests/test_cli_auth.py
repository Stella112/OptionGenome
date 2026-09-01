"""CLI authentication check tests.

A captured reference file proves the capture ran, not that a profile is logged
in. Authentication is session state and must be verified against the binary.
"""

from __future__ import annotations

import pytest

from src.broker.alpaca_cli import check_authenticated


def runner(code=0, stdout="", stderr=""):
    return lambda argv: (code, stdout, stderr)


def test_authenticated_binary_passes():
    ok, detail = check_authenticated(runner=runner(0, '{"code":0,"error":"","status":200}'))
    assert ok
    assert detail == "authenticated"


def test_unauthenticated_binary_fails():
    ok, detail = check_authenticated(
        runner=runner(0, '{"error":"authentication required\\nHint: run `alpaca profile login`"}')
    )
    assert not ok
    assert "profile login" in detail


def test_login_hint_alone_is_treated_as_unauthenticated():
    ok, detail = check_authenticated(runner=runner(0, "run alpaca profile login to continue"))
    assert not ok


def test_nonzero_exit_fails():
    ok, detail = check_authenticated(runner=runner(1, "", "doctor failed"))
    assert not ok
    assert "doctor failed" in detail


def test_a_crashing_runner_fails_closed():
    def boom(argv):
        raise OSError("binary vanished")

    ok, detail = check_authenticated(runner=boom)
    assert not ok
    assert "OSError" in detail


def test_missing_binary_fails():
    ok, detail = check_authenticated(binary="definitely-not-real-xyz")
    assert not ok
    assert "not on PATH" in detail


def test_stderr_is_inspected_too():
    ok, _ = check_authenticated(runner=runner(0, "", "authentication required"))
    assert not ok
