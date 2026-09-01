"""Paper-only invariant and dev/judge account isolation (spec sections 3 and 7).

These are assertions, not preferences. Every one of them fails loudly, and a
failure halts the process before any order object can be constructed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

PAPER_HOST = "paper-api.alpaca.markets"

#: Environment variables that, if truthy, indicate a live-trading configuration.
#: Presence alone is not fatal; a truthy value is.
LIVE_TRADE_FLAGS = (
    "ALPACA_LIVE_TRADE",
    "ALPACA_LIVE",
    "APCA_LIVE_TRADING",
    "ALPACA_ENABLE_LIVE",
    "OG_LIVE",
)

TRUTHY = {"1", "true", "yes", "y", "on", "live"}

#: Argument fragments that must never appear in a broker write command.
FORBIDDEN_CLI_ARGS = ("--live", "--production", "--prod", "--real")


class Mode(str, Enum):
    DEVELOPMENT = "development"
    JUDGING = "judging"


class SafetyViolation(RuntimeError):
    """Raised when a paper-only or account-isolation invariant is broken.

    Never caught to continue trading. The only valid response is HALT.
    """


@dataclass(frozen=True)
class ExecutionMode:
    """The mode and the single account id it is allowed to touch."""

    mode: Mode
    allowed_account_id: str

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ExecutionMode":
        env = env if env is not None else dict(os.environ)
        raw = (env.get("MODE") or "").strip().lower()
        if raw not in {m.value for m in Mode}:
            raise SafetyViolation(
                f"MODE must be one of {sorted(m.value for m in Mode)}, got {raw!r}. "
                "There is no mode in which an unknown account is accepted."
            )
        mode = Mode(raw)

        key = "DEV_ACCOUNT_ID" if mode is Mode.DEVELOPMENT else "JUDGE_ACCOUNT_ID"
        allowed = (env.get(key) or "").strip()
        if not allowed:
            raise SafetyViolation(f"MODE={mode.value} requires {key} to be set")

        other_key = "JUDGE_ACCOUNT_ID" if mode is Mode.DEVELOPMENT else "DEV_ACCOUNT_ID"
        other = (env.get(other_key) or "").strip()
        if other and other == allowed:
            raise SafetyViolation(
                f"{key} and {other_key} are the same account. Development and judging "
                "accounts must be distinct; the judging account is never used for development."
            )

        return cls(mode=mode, allowed_account_id=allowed)

    def assert_account(self, actual_account_id: str) -> None:
        """The assertion that runs immediately before every broker write."""
        if not actual_account_id:
            raise SafetyViolation("broker reported no account id; refusing to write")
        if actual_account_id != self.allowed_account_id:
            raise SafetyViolation(
                f"account assertion failed in {self.mode.value} mode: broker account "
                f"{actual_account_id!r} is not the configured {self.allowed_account_id!r}"
            )


def assert_paper_host(url: str) -> None:
    """The trading host must be Alpaca's paper endpoint, over HTTPS."""
    if not url:
        raise SafetyViolation("no trading host configured")
    parsed = urlparse(url if "//" in url else f"https://{url}")
    if parsed.scheme != "https":
        raise SafetyViolation(f"trading host must use https, got {url!r}")
    if parsed.hostname != PAPER_HOST:
        raise SafetyViolation(
            f"trading host {parsed.hostname!r} is not the paper endpoint {PAPER_HOST!r}. "
            "This system never trades live."
        )


def assert_no_live_flags(env: dict | None = None) -> None:
    """Reject ALPACA_LIVE_TRADE=true and every equivalent."""
    env = env if env is not None else dict(os.environ)
    for flag in LIVE_TRADE_FLAGS:
        value = (env.get(flag) or "").strip().lower()
        if value in TRUTHY:
            raise SafetyViolation(f"live-trading flag {flag}={value!r} is set. Refusing to start.")


def assert_no_live_args(argv: list[str]) -> None:
    """Reject a --live style argument anywhere on a command line."""
    for arg in argv:
        lowered = str(arg).strip().lower()
        for forbidden in FORBIDDEN_CLI_ARGS:
            if lowered == forbidden or lowered.startswith(f"{forbidden}="):
                raise SafetyViolation(f"forbidden argument {arg!r} in broker command")


def assert_paper_only(
    trading_host: str,
    argv: list[str] | None = None,
    env: dict | None = None,
) -> None:
    """The complete paper-only gate (spec section 7)."""
    assert_paper_host(trading_host)
    assert_no_live_flags(env)
    assert_no_live_args(argv or [])


def assert_options_level(level: int, required: int = 3) -> None:
    if level < required:
        raise SafetyViolation(
            f"account has options level {level}; multi-leg defined-risk spreads need {required}"
        )
