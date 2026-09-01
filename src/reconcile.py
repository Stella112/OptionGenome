"""Reconciliation and the high-water mark (spec section 30).

The desk's view of the world is rebuilt from the broker on every pass, never
carried forward from what it believes it did. Account equity, open positions and
day risk all come back through MCP; the journal supplies the persisted
high-water mark so a restart cannot silently reset a drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .broker.alpaca_mcp import AlpacaMCP
from .broker.occ import OCCError, parse_option_symbol
from .config import Config
from .journal import Journal
from .types import Account, Book, OpenStructure, Quote, SystemState


class ReconcileError(RuntimeError):
    """Raised when broker state cannot be read or parsed into a usable book."""


def _as_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ReconcileError(f"{field} is not numeric: {value!r}") from exc


def high_water_mark(
    journal: Journal,
    current_equity: float,
    persisted: float | None = None,
    start_of_day_equity: float | None = None,
) -> float:
    """max(persisted, journalled peak, start-of-day equity, current equity).

    start_of_day_equity matters on a cold start: with an empty journal, taking
    the peak as the current equity would make the drawdown zero by definition,
    so an account that opened at 100k and sits at 94k would look healthy and
    keep opening positions. Yesterday's close is a known prior high and must
    count.
    """
    floor = persisted if persisted is not None else 0.0
    candidates = [floor, journal.high_water_mark(floor=floor), current_equity]
    if start_of_day_equity is not None:
        candidates.append(start_of_day_equity)
    return max(candidates)


def drawdown_state(account: Account, config: Config) -> SystemState:
    """FLATTEN_ONLY once the drawdown limit is breached (spec section 30)."""
    if account.drawdown >= config.dd_flatten_pct:
        return SystemState.FLATTEN_ONLY
    return SystemState.READY


def build_account(
    raw: Mapping[str, Any],
    allowed_account_id: str,
    journal: Journal,
    start_of_day_equity: float | None = None,
    persisted_high_water: float | None = None,
) -> Account:
    """Turn an MCP account payload into the desk's Account."""
    if not raw:
        raise ReconcileError("broker returned no account")

    account_id = str(raw.get("id") or raw.get("account_id") or "").strip()
    if not account_id:
        raise ReconcileError("broker account payload has no id")

    equity = _as_float(raw.get("equity"), "equity")
    cash = _as_float(raw.get("cash"), "cash")
    buying_power = _as_float(raw.get("buying_power"), "buying_power")

    try:
        options_level = int(raw.get("options_trading_level") or raw.get("options_level") or 0)
    except (TypeError, ValueError):
        options_level = 0

    sod = start_of_day_equity
    if sod is None:
        sod = _as_float(raw.get("last_equity") or equity, "last_equity")

    return Account(
        account_id=account_id,
        allowed_account_id=allowed_account_id,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
        options_level=options_level,
        start_of_day_equity=sod,
        high_water_mark=high_water_mark(
            journal, equity, persisted_high_water, start_of_day_equity=sod
        ),
    )


def group_positions_into_structures(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[OpenStructure, ...]:
    """Collapse individual option legs into the structures the desk manages.

    Alpaca reports legs, not structures. Legs sharing an underlying and expiry
    are treated as one structure, which is also the granularity the Risk
    Officer's overlapping-exposure check works at.
    """
    grouped: dict[tuple[str, date], list[Mapping[str, Any]]] = {}
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        try:
            contract = parse_option_symbol(symbol)
        except OCCError:
            continue  # not an option leg; equity positions are not managed here
        grouped.setdefault((contract.underlying, contract.expiry), []).append(pos)

    structures: list[OpenStructure] = []
    for (underlying, expiry), legs in sorted(grouped.items()):
        short_strikes: list[Decimal] = []
        entry_credit = 0.0
        lots = 0
        for leg in legs:
            contract = parse_option_symbol(str(leg["symbol"]))
            qty = _as_float(leg.get("qty", 0), "qty")
            cost_basis = _as_float(leg.get("cost_basis", 0), "cost_basis")
            if qty < 0:  # a short leg carries a negative quantity
                short_strikes.append(contract.strike)
                entry_credit += -cost_basis
            else:
                entry_credit -= cost_basis
            lots = max(lots, int(abs(qty)))

        structures.append(
            OpenStructure(
                structure_id=f"{underlying}-{expiry.isoformat()}",
                underlying=underlying,
                structure_type="put_credit_spread" if len(legs) == 2 else "iron_condor",
                expiry=expiry,
                short_strikes=tuple(sorted(short_strikes)),
                lots=max(1, lots),
                entry_credit=entry_credit,
                max_loss=0.0,  # recomputed from geometry when a ticket is rebuilt
                opened_at=datetime.now().astimezone(),
                roll_count=0,
            )
        )
    return tuple(structures)


@dataclass(frozen=True)
class ReconciledState:
    account: Account
    book: Book
    system_state: SystemState

    @property
    def flatten_only(self) -> bool:
        return self.system_state is SystemState.FLATTEN_ONLY


def reconcile(
    mcp: AlpacaMCP,
    config: Config,
    journal: Journal,
    allowed_account_id: str,
    now: datetime,
    quotes: Mapping[str, Quote] | None = None,
    start_of_day_equity: float | None = None,
    persisted_high_water: float | None = None,
) -> ReconciledState:
    """Rebuild account and book from the broker. Raises ReconcileError on bad state."""
    raw_account = mcp.get_account()
    account = build_account(
        raw_account,
        allowed_account_id=allowed_account_id,
        journal=journal,
        start_of_day_equity=start_of_day_equity,
        persisted_high_water=persisted_high_water,
    )

    clock = mcp.get_clock() or {}
    market_open = bool(clock.get("is_open", False))
    minutes_to_close = int(clock.get("minutes_to_close", 0) or 0)

    structures = group_positions_into_structures(mcp.get_positions() or [])
    day_risk = journal.risk_opened_on(now.date())

    book = Book(
        open_structures=structures,
        day_open_risk=day_risk,
        quotes=dict(quotes or {}),
        now=now,
        market_open=market_open,
        minutes_to_close=minutes_to_close,
    )

    journal.record(
        "RECONCILE",
        account_id=account.account_id,
        equity=account.equity,
        cash=account.cash,
        high_water_mark=account.high_water_mark,
        drawdown=round(account.drawdown, 5),
        open_structures=len(structures),
        day_open_risk=day_risk,
        market_open=market_open,
    )

    return ReconciledState(
        account=account,
        book=book,
        system_state=drawdown_state(account, config),
    )
