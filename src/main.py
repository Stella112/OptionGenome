"""Entry point: connect, gate, then trade.

    python -m src.main --check     run the readiness gate and exit
    python -m src.main --once      one pass, then exit
    python -m src.main             run the session loop

Nothing reaches the broker unless the gate reports READY. The gate itself needs
a live MCP session and a reconciled account, so this module builds those first
and hands them in.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .broker.alpaca_cli import AlpacaCLI, CLIUnavailable, load_cli_reference
from .broker.alpaca_mcp import AlpacaMCP, MCPUnavailable, discover_capabilities
from .broker.mcp_stdio import MCPStdioBridge
from .config import load_config
from .journal import Journal
from .loop import TradingLoop
from .rolldesk.ranker import FeatherlessRanker
from .safety import ExecutionMode, SafetyViolation
from .startup import run_gate
from .types import SystemState

POLL_SECONDS = int(os.getenv("OG_POLL_SECONDS", "60"))


def _load_dotenv(path: str = ".env") -> None:
    """Load .env without adding a hard dependency for a five-line job."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def account_snapshot(mcp: AlpacaMCP) -> dict[str, Any] | None:
    """Read the account through MCP, shaped for the gate. None if unreadable."""
    try:
        raw = mcp.get_account()
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        level = int(raw.get("options_trading_level") or raw.get("options_level") or 0)
    except (TypeError, ValueError):
        level = 0
    return {
        "account_id": str(raw.get("id") or raw.get("account_number") or ""),
        "options_level": level,
        "buying_power": float(raw.get("buying_power") or 0.0),
        "equity": float(raw.get("equity") or 0.0),
    }


def build_desk(journal: Journal, bridge: MCPStdioBridge) -> tuple[TradingLoop, AlpacaMCP]:
    config = load_config()
    mode = ExecutionMode.from_env(dict(os.environ))
    capabilities = discover_capabilities(list(bridge.tools))
    mcp = AlpacaMCP(bridge.call_tool, capabilities)
    mcp.verify()

    cli = AlpacaCLI(
        execution_mode=mode,
        trading_host=os.environ["ALPACA_TRADING_HOST"],
        reference=load_cli_reference(),
    )
    loop = TradingLoop(
        config=config,
        journal=journal,
        mcp=mcp,
        cli=cli,
        execution_mode=mode,
        ranker=FeatherlessRanker(),
    )
    return loop, mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optiongenome")
    parser.add_argument("--check", action="store_true", help="run the readiness gate and exit")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args(argv)

    _load_dotenv()
    journal = Journal()

    try:
        bridge = MCPStdioBridge().start()
    except MCPUnavailable as exc:
        print(f"MCP unavailable: {exc}", file=sys.stderr)
        journal.record("HALT", stage="mcp_connect", error=str(exc))
        bridge = None

    try:
        mcp = None
        snapshot = None
        if bridge is not None:
            capabilities = discover_capabilities(list(bridge.tools))
            mcp = AlpacaMCP(bridge.call_tool, capabilities)
            snapshot = account_snapshot(mcp)

        result = run_gate(mcp=mcp, account_snapshot=snapshot, journal=journal)
        print(result.report())
        journal.record(
            "STARTUP",
            state=result.state.value,
            failures=[o.name for o in result.failures],
        )

        if args.check:
            return 0 if result.passed else 1

        if result.state is not SystemState.READY:
            print("\nrefusing to trade: the gate did not reach READY", file=sys.stderr)
            return 1

        loop, mcp = build_desk(journal, bridge)
        print("\nSYSTEM_STATE = READY; entering the trading loop")

        while True:
            now = datetime.now(timezone.utc)
            # Chain and signals are supplied by the market-data layer; a pass
            # with neither still reconciles and manages open positions.
            passed = loop.run_once(chain=[], signals=None, open_positions=[], now=now)
            print(f"[{now:%H:%M:%S}] {passed.as_dict()}")
            if args.once:
                return 0
            time.sleep(POLL_SECONDS)

    except (SafetyViolation, CLIUnavailable, MCPUnavailable) as exc:
        print(f"HALTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        journal.record("HALT", error=str(exc))
        return 1
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())
