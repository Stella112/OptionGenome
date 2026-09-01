"""Alpaca CLI write adapter (spec sections 4, 6, 31, 33).

ALL broker writes go through here: submit, cancel, close, flatten, roll,
do-not-exercise. No direct SDK call and no direct REST request exists anywhere
in the application.

The installed binary is the source of truth for its own arguments. This module
refuses to construct a command until `alpaca order submit --schema` has actually
been captured into docs/cli-reference.txt, because a CLI argument written from
memory is a build failure (spec section 4).

Consequently, on a machine without the CLI installed, every write path halts.
That is the gate working, not a missing feature.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..safety import ExecutionMode, SafetyViolation, assert_no_live_args
from ..types import RiskDecision, Ticket

CLI_REFERENCE_DOC = Path("docs/cli-reference.txt")
CLI_BINARY = "alpaca"

#: Marker left in the placeholder file. While present, no command may be built.
PLACEHOLDER_MARKER = "PENDING"

#: Captures that must be present in docs/cli-reference.txt before the write
#: path unlocks (spec section 4).
REQUIRED_CAPTURES = (
    "alpaca doctor",
    "alpaca --help-all",
    "alpaca order submit --help",
    "alpaca order submit --schema",
)


class CLIUnavailable(RuntimeError):
    """Raised when the CLI is absent, unauthenticated, or its schema uncaptured."""


@dataclass(frozen=True)
class CLIReference:
    """The captured output of the installed binary."""

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
    reference_path: Path | str = CLI_REFERENCE_DOC,
    binary: str = CLI_BINARY,
) -> CLIReference:
    """Startup gate items 12-14. Raises CLIUnavailable rather than improvising.

    Verifies the binary exists on PATH and that its own schema has been captured.
    """
    if cli_binary_path(binary) is None:
        raise CLIUnavailable(
            f"{binary!r} is not on PATH. Install it (brew install alpacahq/tap/cli, or "
            f"go install github.com/alpacahq/cli/cmd/alpaca@latest) and run 'alpaca profile login'."
        )

    reference = load_cli_reference(reference_path)
    if reference.is_placeholder:
        raise CLIUnavailable(
            f"{reference.path} is still the placeholder. Capture the real output of:\n  "
            + "\n  ".join(REQUIRED_CAPTURES)
            + "\nNo CLI argument may be implemented from memory."
        )
    if reference.missing_captures:
        raise CLIUnavailable(
            f"{reference.path} is missing captures: {list(reference.missing_captures)}"
        )
    return reference


class AlpacaCLI:
    """Constructs and runs broker write commands. Every write passes the guards.

    Command construction is deliberately unimplemented until the schema capture
    exists: `build_submit_command` raises so that a missing gate surfaces as a
    halt, never as a guessed argument list reaching a real broker.
    """

    def __init__(
        self,
        execution_mode: ExecutionMode,
        trading_host: str,
        reference: CLIReference | None = None,
        binary: str = CLI_BINARY,
    ):
        self.execution_mode = execution_mode
        self.trading_host = trading_host
        self.reference = reference
        self.binary = binary

    def _guard_write(self, actual_account_id: str, argv: Sequence[str]) -> None:
        """Runs immediately before every broker write (spec section 33)."""
        from ..safety import assert_paper_host

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
        """The checks that gate order construction, in spec order.

        Raises before any command is built. A ticket that never received ALLOW
        cannot reach a command, and neither can a lot count of zero.
        """
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

    def build_submit_command(
        self,
        ticket: Ticket,
        decision: RiskDecision,
        limit_price: float,
        actual_account_id: str,
        system_state: str = "READY",
    ) -> list[str]:
        """Build the multi-leg submit command.

        Deliberately not implemented. Spec section 31 fixes the ORDER SHAPE
        (order_class 'mleg', per-leg symbol/side/ratio_qty/position_intent,
        parent qty/type/time_in_force, always limit and never market), but the
        FLAG NAMES that carry that shape belong to the installed binary. They
        are transcribed from `alpaca order submit --schema` once it is captured
        into docs/cli-reference.txt -- not from memory.
        """
        self.preflight(ticket, decision, actual_account_id, system_state)
        raise CLIUnavailable(
            "build_submit_command is unimplemented pending the captured CLI schema. "
            "Run 'alpaca order submit --schema' into docs/cli-reference.txt, then transcribe "
            "the flag names here. See spec section 4."
        )
