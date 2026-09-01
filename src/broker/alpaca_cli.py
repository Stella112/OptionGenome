"""Alpaca CLI write adapter (spec sections 4, 6, 31, 32, 33).

ALL broker writes go through here: submit, cancel, close, flatten, roll. No
direct SDK call and no direct REST request exists anywhere in the application.

Flag names in this module are transcribed from the captured output of the
installed binary (docs/cli-reference.txt, alpaca v0.0.14), not from memory:

    --order-class --legs --qty --type --limit-price --time-in-force
    --client-order-id --dry-run

One thing the captured help does NOT specify is the wire format of `--legs`
(it documents only "list of order legs (<= 4)"). That format is therefore
treated as UNVERIFIED: `verify_legs_format()` must prove it against the real
binary with --dry-run before any live submission is permitted. Until that
passes, submit_order refuses. See LegsFormatUnverified.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from ..safety import ExecutionMode, SafetyViolation, assert_no_live_args, assert_paper_host
from ..types import CONTRACT_MULTIPLIER, Leg, RiskDecision, Ticket

CLI_REFERENCE_DOC = Path("docs/cli-reference.txt")
CLI_BINARY = "alpaca"
PLACEHOLDER_MARKER = "PENDING"

REQUIRED_CAPTURES = (
    "alpaca doctor",
    "alpaca --help-all",
    "alpaca order submit --help",
    "alpaca order submit --schema",
)

#: Multi-leg spreads are ALWAYS limit orders, never market (spec section 31).
ORDER_TYPE = "limit"
TIME_IN_FORCE = "day"
ORDER_CLASS = "mleg"

#: Deterministic concession from mid when pricing a credit structure: give up
#: this fraction of the mid credit to improve fill odds (spec section 32). It
#: is a shave off a limit price, never a switch to a market order.
LIMIT_CONCESSION = 0.05
MIN_LIMIT_PRICE = 0.01


class CLIUnavailable(RuntimeError):
    """Raised when the CLI is absent, unauthenticated, or its schema uncaptured."""


class LegsFormatUnverified(CLIUnavailable):
    """Raised when a live submit is attempted before --legs has been proven.

    The captured help does not document the --legs wire format. Guessing it and
    sending it to a broker is exactly what spec section 4 forbids.
    """


@dataclass(frozen=True)
class CLIReference:
    path: Path
    text: str

    @property
    def is_placeholder(self) -> bool:
        return self.text.strip().startswith(PLACEHOLDER_MARKER) or not self.text.strip()

    @property
    def missing_captures(self) -> tuple[str, ...]:
        return tuple(c for c in REQUIRED_CAPTURES if c not in self.text)

    @property
    def is_usable(self) -> bool:
        return not self.is_placeholder and not self.missing_captures


def load_cli_reference(path: Path | str = CLI_REFERENCE_DOC) -> CLIReference:
    path = Path(path)
    if not path.exists():
        raise CLIUnavailable(
            f"{path} does not exist. Capture the installed binary's output first:\n  "
            + "\n  ".join(REQUIRED_CAPTURES)
        )
    return CLIReference(path=path, text=path.read_text(encoding="utf-8"))


def cli_binary_path(binary: str = CLI_BINARY) -> str | None:
    return shutil.which(binary)


def verify_cli(
    reference_path: Path | str = CLI_REFERENCE_DOC, binary: str = CLI_BINARY
) -> CLIReference:
    """Startup gate items 12-14. Raises CLIUnavailable rather than improvising."""
    if cli_binary_path(binary) is None:
        raise CLIUnavailable(
            f"{binary!r} is not on PATH. Install it and run 'alpaca profile login'."
        )
    reference = load_cli_reference(reference_path)
    if reference.is_placeholder:
        raise CLIUnavailable(
            f"{reference.path} is still the placeholder. Capture the real output of:\n  "
            + "\n  ".join(REQUIRED_CAPTURES)
        )
    if reference.missing_captures:
        raise CLIUnavailable(
            f"{reference.path} is missing captures: {list(reference.missing_captures)}"
        )
    return reference


# --- limit pricing (spec section 32) ----------------------------------------


def limit_price_for_credit(credit_mid: float, concession: float = LIMIT_CONCESSION) -> float:
    """Price a credit structure from mid, shaved by a fixed deterministic amount.

    Never returns zero or negative, and never signals a market order. Rounded to
    the penny because that is the tradeable increment.
    """
    if credit_mid <= 0:
        raise ValueError(f"credit_mid must be positive, got {credit_mid}")
    price = round(credit_mid * (1 - concession), 2)
    return max(MIN_LIMIT_PRICE, price)


def limit_price_to_close(cost_mid: float, concession: float = LIMIT_CONCESSION) -> float:
    """Price a closing (debit) order from mid, paying slightly up to get filled."""
    if cost_mid < 0:
        raise ValueError(f"cost_mid must not be negative, got {cost_mid}")
    return max(MIN_LIMIT_PRICE, round(cost_mid * (1 + concession), 2))


# --- leg encoding ------------------------------------------------------------


def encode_legs(legs: Sequence[Leg]) -> str:
    """Serialise legs for --legs.

    JSON is the format implied by the captured response schema, whose Order
    entity carries `legs: object[]` with exactly these field names. It remains
    UNVERIFIED against the binary until verify_legs_format() runs.
    """
    if not legs:
        raise ValueError("cannot encode an empty leg list")
    if len(legs) > 4:
        raise ValueError(f"Alpaca accepts at most 4 legs, got {len(legs)}")
    return json.dumps(
        [
            {
                "symbol": leg.symbol,
                "side": leg.side,
                "ratio_qty": str(leg.ratio_qty),
                "position_intent": leg.position_intent,
            }
            for leg in legs
        ],
        separators=(",", ":"),
    )


def closing_legs(legs: Sequence[Leg]) -> tuple[Leg, ...]:
    """Mirror an opening structure into the order that closes it."""
    flip_side = {"buy": "sell", "sell": "buy"}
    flip_intent = {
        "sell_to_open": "buy_to_close",
        "buy_to_open": "sell_to_close",
        "buy_to_close": "sell_to_open",
        "sell_to_close": "buy_to_open",
    }
    return tuple(
        Leg(
            symbol=leg.symbol,
            side=flip_side[leg.side],
            position_intent=flip_intent[leg.position_intent],
            ratio_qty=leg.ratio_qty,
        )
        for leg in legs
    )


@dataclass(frozen=True)
class OrderCommand:
    """A fully built, inspectable broker command. Building one is not sending it."""

    argv: tuple[str, ...]
    ticket_id: str
    client_order_id: str
    limit_price: float
    lots: int
    intent: str  # "open" | "close"

    @property
    def shell(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    def with_dry_run(self) -> "OrderCommand":
        if "--dry-run" in self.argv:
            return self
        return OrderCommand(
            argv=self.argv + ("--dry-run",),
            ticket_id=self.ticket_id,
            client_order_id=self.client_order_id,
            limit_price=self.limit_price,
            lots=self.lots,
            intent=self.intent,
        )


def check_authenticated(binary: str = CLI_BINARY, runner=None) -> tuple[bool, str]:
    """Ask the installed binary whether it is actually authenticated.

    Reading the captured reference file only proves the capture happened, not
    that a profile is logged in. Startup gate item 13 must not report PASS on a
    binary that will reject the first order of the session.
    """
    # The PATH check only applies to the real binary. An injected runner IS the
    # binary as far as this check is concerned, so tests need no executable.
    if runner is None and cli_binary_path(binary) is None:
        return False, f"{binary!r} is not on PATH"

    run = runner or AlpacaCLI._subprocess_runner
    try:
        code, stdout, stderr = run([binary, "doctor"])
    except Exception as exc:
        return False, f"could not run '{binary} doctor': {type(exc).__name__}: {exc}"

    output = "\n".join(part for part in (stdout, stderr) if part).strip()
    if "authentication required" in output.lower() or "profile login" in output.lower():
        return False, "not authenticated: run 'alpaca profile login'"
    if code != 0:
        return False, output[:200] or f"'{binary} doctor' exited {code}"
    return True, "authenticated"


class AlpacaCLI:
    """Constructs and runs broker write commands. Every write passes the guards."""

    def __init__(
        self,
        execution_mode: ExecutionMode,
        trading_host: str,
        reference: CLIReference | None = None,
        binary: str = CLI_BINARY,
        runner=None,
    ):
        self.execution_mode = execution_mode
        self.trading_host = trading_host
        self.reference = reference
        self.binary = binary
        #: Injected for tests: callable(argv) -> (returncode, stdout, stderr).
        self._runner = runner or self._subprocess_runner
        self._legs_format_verified = False

    # --- process plumbing ----------------------------------------------------

    @staticmethod
    def _subprocess_runner(argv: Sequence[str]) -> tuple[int, str, str]:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=60, check=False
        )
        return completed.returncode, completed.stdout, completed.stderr

    # --- guards --------------------------------------------------------------

    def _guard_write(self, actual_account_id: str, argv: Sequence[str]) -> None:
        """Runs immediately before every broker write (spec section 33)."""
        assert_paper_host(self.trading_host)
        assert_no_live_args(list(argv))
        self.execution_mode.assert_account(actual_account_id)

    def preflight(
        self,
        ticket: Ticket,
        decision: RiskDecision,
        actual_account_id: str,
        system_state: str,
    ) -> None:
        """The checks that gate order construction, in spec order."""
        if system_state not in ("READY", "FLATTEN_ONLY"):
            raise SafetyViolation(f"system state {system_state!r} may not write to the broker")
        if not decision.allowed:
            raise SafetyViolation(
                f"ticket {ticket.ticket_id} was not allowed: {list(decision.reasons)}"
            )
        if decision.allowed_lots < 1:
            raise SafetyViolation(f"ticket {ticket.ticket_id} allowed zero lots")
        if self.reference is None or not self.reference.is_usable:
            raise CLIUnavailable(
                "CLI schema has not been captured; refusing to construct a broker command"
            )
        self._guard_write(actual_account_id, [])

    # --- command construction ------------------------------------------------

    def build_submit_command(
        self,
        ticket: Ticket,
        decision: RiskDecision,
        actual_account_id: str,
        system_state: str = "READY",
        client_order_id: str | None = None,
    ) -> OrderCommand:
        """Build the multi-leg opening order (spec section 31).

        order_class mleg, one qty for the parent, per-leg ratio_qty and
        position_intent, always limit and day. Never a market order.
        """
        self.preflight(ticket, decision, actual_account_id, system_state)

        lots = decision.allowed_lots
        limit_price = limit_price_for_credit(ticket.credit_mid)
        coid = client_order_id or f"og-{ticket.ticket_id}-{lots}"

        argv = (
            self.binary,
            "order",
            "submit",
            "--order-class",
            ORDER_CLASS,
            "--qty",
            str(lots),
            "--type",
            ORDER_TYPE,
            "--time-in-force",
            TIME_IN_FORCE,
            "--limit-price",
            f"{limit_price:.2f}",
            "--legs",
            encode_legs(ticket.legs),
            "--client-order-id",
            coid[:128],
        )
        self._guard_write(actual_account_id, argv)
        return OrderCommand(
            argv=argv,
            ticket_id=ticket.ticket_id,
            client_order_id=coid[:128],
            limit_price=limit_price,
            lots=lots,
            intent="open",
        )

    def build_close_command(
        self,
        ticket: Ticket,
        lots: int,
        cost_to_close_mid: float,
        actual_account_id: str,
        system_state: str = "READY",
        client_order_id: str | None = None,
    ) -> OrderCommand:
        """Build the order that closes an open structure.

        Closing is always permitted in FLATTEN_ONLY: reducing risk is the point
        of that state. It does not require a Risk Officer ALLOW, because the
        officer gates NEW risk, not the removal of existing risk.
        """
        if system_state not in ("READY", "FLATTEN_ONLY"):
            raise SafetyViolation(f"system state {system_state!r} may not write to the broker")
        if lots < 1:
            raise SafetyViolation(f"cannot close {lots} lots")
        if self.reference is None or not self.reference.is_usable:
            raise CLIUnavailable("CLI schema has not been captured; refusing to close")

        limit_price = limit_price_to_close(cost_to_close_mid)
        coid = client_order_id or f"og-close-{ticket.ticket_id}-{lots}"

        argv = (
            self.binary,
            "order",
            "submit",
            "--order-class",
            ORDER_CLASS,
            "--qty",
            str(lots),
            "--type",
            ORDER_TYPE,
            "--time-in-force",
            TIME_IN_FORCE,
            "--limit-price",
            f"{limit_price:.2f}",
            "--legs",
            encode_legs(closing_legs(ticket.legs)),
            "--client-order-id",
            coid[:128],
        )
        self._guard_write(actual_account_id, argv)
        return OrderCommand(
            argv=argv,
            ticket_id=ticket.ticket_id,
            client_order_id=coid[:128],
            limit_price=limit_price,
            lots=lots,
            intent="close",
        )

    # --- verification and execution -----------------------------------------

    def verify_legs_format(self, command: OrderCommand) -> tuple[bool, str]:
        """Prove the --legs encoding against the real binary using --dry-run.

        --dry-run prints the request body without submitting, so this exercises
        the binary's own parsing with no order reaching the market. Must pass
        before submit() will send anything.
        """
        code, stdout, stderr = self._runner(command.with_dry_run().argv)
        output = f"{stdout}\n{stderr}".strip()
        if code != 0:
            return False, output or f"exit {code}"
        if "authentication required" in output.lower():
            return False, "authentication required: run 'alpaca profile login'"
        if "error" in output.lower() and '"error": ""' not in output:
            try:
                payload = json.loads(stdout)
                if payload.get("error"):
                    return False, str(payload["error"])
            except json.JSONDecodeError:
                pass
        self._legs_format_verified = True
        return True, output

    def submit(self, command: OrderCommand, actual_account_id: str) -> tuple[int, str, str]:
        """Send a built command to the broker.

        Refuses until the --legs wire format has been proven with --dry-run,
        because the captured help does not document it and spec section 4
        forbids implementing an argument from memory.
        """
        if not self._legs_format_verified:
            raise LegsFormatUnverified(
                "the --legs wire format has not been verified against the installed binary. "
                "Run verify_legs_format() (which uses --dry-run) first. The captured help "
                "documents only 'list of order legs (<= 4)'."
            )
        self._guard_write(actual_account_id, command.argv)
        return self._runner(command.argv)
