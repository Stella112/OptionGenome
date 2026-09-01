"""Startup readiness gate (spec section 8).

Twenty checks run before the trading loop begins. Only when every one passes
does the system reach READY. Any failure yields HALTED, is journaled, and no
order object may be constructed.

The gate is written so a machine with no CLI and no MCP connection halts with a
precise, actionable reason rather than starting in a degraded state.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .broker.alpaca_cli import CLIUnavailable, verify_cli
from .broker.alpaca_mcp import AlpacaMCP, MCPUnavailable, load_tool_manifest
from .config import Config, ConfigError, load_config
from .journal import Journal
from .marketdna.calendar import CalendarError, load_events
from .risk import officer
from .safety import (
    ExecutionMode,
    SafetyViolation,
    assert_no_live_flags,
    assert_options_level,
    assert_paper_host,
)
from .types import SystemState

MIN_PYTHON = (3, 12)


@dataclass(frozen=True)
class CheckOutcome:
    index: int
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{self.index:2d}] {mark} {self.name}: {self.detail}"


@dataclass
class GateResult:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def state(self) -> SystemState:
        return SystemState.READY if self.passed else SystemState.HALTED

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed]

    def report(self) -> str:
        lines = [str(o) for o in self.outcomes]
        lines.append("")
        lines.append(f"SYSTEM_STATE = {self.state.value}")
        if self.failures:
            lines.append(f"{len(self.failures)} check(s) failed; the trading loop will not start.")
        return "\n".join(lines)


def _run(result: GateResult, index: int, name: str, fn: Callable[[], str]) -> Any:
    """Run one check. An exception is a failed check, never a crash."""
    try:
        detail = fn()
        result.outcomes.append(CheckOutcome(index, name, True, detail or "ok"))
        return True
    except Exception as exc:
        result.outcomes.append(CheckOutcome(index, name, False, f"{type(exc).__name__}: {exc}"))
        return False


def run_gate(
    env: dict | None = None,
    config_path: str | Path | None = None,
    mcp: AlpacaMCP | None = None,
    account_snapshot: dict | None = None,
    journal: Journal | None = None,
) -> GateResult:
    """Execute all twenty startup checks in order.

    `account_snapshot` is the reconciled account read through MCP. When absent,
    the identity, options-level and buying-power checks fail closed -- the desk
    does not assume an account it has not actually read.
    """
    env = env if env is not None else dict(os.environ)
    result = GateResult()
    state: dict[str, Any] = {}

    # 1. Python and configuration validation
    def check_python() -> str:
        if sys.version_info < MIN_PYTHON:
            raise RuntimeError(
                f"Python {'.'.join(map(str, MIN_PYTHON))}+ required, running {sys.version.split()[0]}"
            )
        return f"Python {sys.version.split()[0]}"

    _run(result, 1, "python_version", check_python)

    def check_config() -> str:
        state["config"] = load_config(config_path)
        return f"loaded {len(state['config'].underlyings)} underlying(s)"

    _run(result, 2, "config_valid", check_config)

    # 2-3. Paper endpoint and live-flag assertions
    host = env.get("ALPACA_TRADING_HOST", "")
    _run(result, 3, "paper_endpoint", lambda: (assert_paper_host(host), host)[1])
    _run(result, 4, "no_live_flags", lambda: (assert_no_live_flags(env), "no live flags set")[1])

    # 4. Required environment variables
    def check_env() -> str:
        required = ("MODE", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_TRADING_HOST")
        missing = [k for k in required if not (env.get(k) or "").strip()]
        if missing:
            raise RuntimeError(f"missing environment variables: {missing}")
        return "all required variables present"

    _run(result, 5, "environment", check_env)

    # 5. Mode and account configuration
    def check_mode() -> str:
        state["mode"] = ExecutionMode.from_env(env)
        return f"{state['mode'].mode.value} -> {state['mode'].allowed_account_id}"

    _run(result, 6, "execution_mode", check_mode)

    # 6. Account identity verification
    def check_identity() -> str:
        if account_snapshot is None:
            raise RuntimeError("no account read through MCP; identity cannot be verified")
        if "mode" not in state:
            # check 6 failed, so there is no configured account to compare against.
            # Report that plainly rather than cascading into a KeyError.
            raise RuntimeError("execution mode is invalid; account identity cannot be checked")
        mode: ExecutionMode = state["mode"]
        mode.assert_account(str(account_snapshot.get("account_id", "")))
        return f"account {account_snapshot['account_id']} matches {mode.mode.value} mode"

    _run(result, 7, "account_identity", check_identity)

    # 7. Options Level 3
    def check_options_level() -> str:
        if account_snapshot is None:
            raise RuntimeError("no account read through MCP; options level unknown")
        level = int(account_snapshot.get("options_level", 0))
        assert_options_level(level)
        return f"options level {level}"

    _run(result, 8, "options_level", check_options_level)

    # 8. Buying power
    def check_buying_power() -> str:
        if account_snapshot is None:
            raise RuntimeError("no account read through MCP; buying power unknown")
        bp = float(account_snapshot.get("buying_power", 0.0))
        if bp <= 0:
            raise RuntimeError(f"non-positive buying power: {bp}")
        return f"${bp:,.2f}"

    _run(result, 9, "buying_power", check_buying_power)

    # 9-11. MCP connection, discovery, and capability verification
    def check_mcp_connection() -> str:
        if mcp is None:
            raise MCPUnavailable(
                "no MCP server connected. Connect alpacahq/alpaca-mcp-server before starting."
            )
        return "connected"

    _run(result, 10, "mcp_connection", check_mcp_connection)

    def check_mcp_discovery() -> str:
        manifest = load_tool_manifest()
        if manifest is None:
            raise MCPUnavailable(
                "docs/mcp-tools.json holds no discovered tools. Run discovery at runtime; "
                "do not hardcode historical v1 tool names."
            )
        return f"{len(manifest.discovered)} tools discovered"

    _run(result, 11, "mcp_discovery", check_mcp_discovery)

    def check_mcp_capabilities() -> str:
        if mcp is None:
            raise MCPUnavailable("no MCP server connected")
        mcp.verify()
        return "all required capabilities present"

    _run(result, 12, "mcp_capabilities", check_mcp_capabilities)

    # 12-14. CLI availability, authentication, schema
    def check_cli() -> str:
        reference = verify_cli()
        state["cli_reference"] = reference
        return f"{reference.path} captured"

    _run(result, 13, "cli_available", check_cli)

    def check_cli_auth() -> str:
        """Ask the live binary, not the captured file.

        A captured reference proves only that the capture ran. Authentication is
        session state, so it is verified against the binary every startup.
        """
        from .broker.alpaca_cli import check_authenticated

        ok, detail = check_authenticated()
        if not ok:
            raise CLIUnavailable(detail)
        return detail

    _run(result, 14, "cli_authenticated", check_cli_auth)

    def check_cli_schema() -> str:
        reference = state.get("cli_reference")
        if reference is None or not reference.is_usable:
            raise CLIUnavailable("order submit schema not captured")
        return "order submit schema captured"

    _run(result, 15, "cli_schema", check_cli_schema)

    # 15. Event calendar
    def check_events() -> str:
        events = load_events()
        if not events:
            raise CalendarError("event calendar is empty")
        return f"{len(events)} events loaded"

    _run(result, 16, "events_calendar", check_events)

    # 16. Database connectivity
    def check_database() -> str:
        url = env.get("OG_DB", "sqlite:///optiongenome.db")
        if not url.startswith("sqlite"):
            raise RuntimeError(f"unexpected database url: {url}")
        import sqlite3

        target = url.split("///", 1)[-1] if "///" in url else ":memory:"
        conn = sqlite3.connect(target)
        conn.execute("select 1")
        conn.close()
        return f"sqlite reachable at {target}"

    _run(result, 17, "database", check_database)

    # 17. Journal writability
    def check_journal() -> str:
        j = journal or Journal()
        if not j.is_writable():
            raise RuntimeError(f"journal path is not writable: {j.path}")
        return f"{j.path} writable"

    _run(result, 18, "journal_writable", check_journal)

    # 18. Risk Officer self-tests
    def check_officer() -> str:
        if not officer.self_test():
            raise RuntimeError("Risk Officer self-test failed: an unsafe ticket was not denied")
        return "unsafe ticket correctly denied"

    _run(result, 19, "risk_officer_selftest", check_officer)

    # 19-20. OCC parser self-tests and configuration consistency
    def check_occ() -> str:
        from datetime import date
        from decimal import Decimal

        from .broker.occ import build_option_symbol, parse_option_symbol

        symbol = build_option_symbol("SPY", date(2025, 1, 27), "C", Decimal("608"))
        if symbol != "SPY250127C00608000":
            raise RuntimeError(f"OCC build produced {symbol}")
        contract = parse_option_symbol(symbol)
        if contract.strike != Decimal("608") or contract.right != "C":
            raise RuntimeError(f"OCC parse produced {contract}")
        return "round-trip verified"

    _run(result, 20, "occ_selftest", check_occ)

    return result


def main() -> int:
    """Run the gate and print the report. Exit 0 only when READY."""
    from dotenv import load_dotenv

    load_dotenv()
    result = run_gate()
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
