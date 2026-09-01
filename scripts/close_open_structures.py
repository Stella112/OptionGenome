#!/usr/bin/env python
"""Close every open defined-risk structure through the CLI.

Reconstructs each open structure from the broker's own leg positions rather than
from anything the desk remembers, prices the close from live quotes, and routes
the order through the same CLI adapter the trading loop uses — including the
account assertion and the --dry-run format check.

    python scripts/close_open_structures.py --dry-run
    python scripts/close_open_structures.py --confirm

Nothing is sent without --confirm.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker.alpaca_cli import AlpacaCLI, load_cli_reference  # noqa: E402
from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities  # noqa: E402
from src.broker.mcp_stdio import MCPStdioBridge  # noqa: E402
from src.broker.occ import OCCError, parse_option_symbol  # noqa: E402
from src.journal import Journal  # noqa: E402
from src.main import _load_dotenv  # noqa: E402
from src.reconcile import as_list  # noqa: E402
from src.safety import ExecutionMode  # noqa: E402
from src.types import CONTRACT_MULTIPLIER, Leg, Ticket  # noqa: E402


def rebuild_structures(positions) -> dict[tuple[str, str], list[dict]]:
    """Group option legs into structures by underlying and expiry."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        try:
            contract = parse_option_symbol(symbol)
        except OCCError:
            continue  # not an option leg
        grouped[(contract.underlying, contract.expiry.isoformat())].append(pos)
    return grouped


def ticket_from_legs(underlying: str, expiry: str, legs: list[dict]) -> tuple[Ticket, int]:
    """Rebuild the OPENING ticket. closing_legs() inverts it into the close."""
    built: list[Leg] = []
    lots = 1
    for pos in legs:
        qty = float(pos.get("qty", 0))
        lots = max(lots, int(abs(qty)))
        # A negative quantity is a short leg, which was opened with sell_to_open.
        if qty < 0:
            built.append(Leg(str(pos["symbol"]), "sell", "sell_to_open", 1))
        else:
            built.append(Leg(str(pos["symbol"]), "buy", "buy_to_open", 1))

    structure = "iron_condor" if len(built) == 4 else "put_credit_spread"
    ticket = Ticket(
        ticket_id=f"close-{underlying}-{expiry}",
        underlying=underlying,
        structure_type=structure,
        expiry=expiry,
        dte=0,
        legs=tuple(built),
        credit_mid=0.0,
        width=0.0,
        max_loss=0.0,
        short_delta=0.0,
        quote_age_ms=0,
        regime="CLOSE",
        proposed_lots=lots,
    )
    return ticket, lots


def cost_to_close(legs: list[dict]) -> float:
    """Per-share cost to buy the structure back, from the broker's own marks."""
    total = 0.0
    for pos in legs:
        qty = float(pos.get("qty", 0))
        price = float(pos.get("current_price") or pos.get("market_value") or 0) or 0.0
        # Buying back a short costs money; selling a long returns it.
        total += price if qty < 0 else -price
    return max(0.01, abs(total))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="actually send the closing orders")
    args = parser.parse_args()

    _load_dotenv()
    journal = Journal()
    bridge = MCPStdioBridge().start()
    try:
        mcp = AlpacaMCP(bridge.call_tool, discover_capabilities(list(bridge.tools)))
        account = mcp.get_account()
        account_id = str(account.get("account_number") or "")

        cli = AlpacaCLI(
            execution_mode=ExecutionMode.from_env(dict(os.environ)),
            trading_host=os.environ["ALPACA_TRADING_HOST"],
            reference=load_cli_reference(),
        )

        grouped = rebuild_structures(as_list(mcp.get_positions()))
        if not grouped:
            print("no open option structures")
            return 0

        for (underlying, expiry), legs in sorted(grouped.items()):
            ticket, lots = ticket_from_legs(underlying, expiry, legs)
            cost = cost_to_close(legs)
            print(f"\n{underlying} {expiry}  {len(legs)} legs  lots={lots}  cost~{cost:.2f}")
            for leg in ticket.legs:
                print(f"    {leg.symbol} {leg.side}")

            command = cli.build_close_command(
                ticket=ticket,
                lots=lots,
                cost_to_close_mid=cost,
                actual_account_id=account_id,
            )
            print(f"  limit {command.limit_price:.2f}")

            ok, detail = cli.verify_legs_format(command)
            if not ok:
                print(f"  dry-run failed, not sending: {detail[:200]}")
                journal.record("ERROR", stage="close_verify", detail=detail[:500])
                continue
            print("  dry-run accepted")

            if not args.confirm:
                print("  (not sent; pass --confirm)")
                continue

            code, stdout, stderr = cli.submit(command, account_id)
            journal.record(
                "FLATTEN",
                ticket_id=ticket.ticket_id,
                reasons=["manual_roll"],
                exit_code=code,
                stdout=stdout[:1500],
                stderr=stderr[:500],
            )
            print(f"  submitted, exit={code}")
        return 0
    finally:
        bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())
