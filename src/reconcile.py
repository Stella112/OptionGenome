"""Reconciliation and the high-water mark (spec section 30).

The desk's view of the world is rebuilt from the broker on every pass, never
carried forward from what it believes it did. Account equity, open positions and
day risk all come back through MCP; the journal supplies the persisted
high-water mark so a restart cannot silently reset a drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .broker.alpaca_mcp import AlpacaMCP
from .broker.occ import OCCError, parse_option_symbol
from .config import Config
from .journal import Journal
from .marketdata import market_clock
from .types import CONTRACT_MULTIPLIER, Account, Book, OpenStructure, Quote, SystemState


class ReconcileError(RuntimeError):
    """Raised when broker state cannot be read or parsed into a usable book."""


def _as_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ReconcileError(f"{field} is not numeric: {value!r}") from exc


def as_list(payload: Any) -> list:
    """Normalise a list-returning MCP payload.

    Alpaca wraps collections as {"result": [...]}. Iterating that dict yields
    its KEYS, which are strings, so downstream code that expects records fails
    with an unhelpful AttributeError. Normalise once, here.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("result", "positions", "orders", "data"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
        return []
    # A bare string is an error message from the server, never a collection.
    raise ReconcileError(f"expected a collection, got {type(payload).__name__}: {str(payload)[:200]}")



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

    # `account_number` (PA3Y88DE6VC4) is what the dashboard shows, what the
    # hackathon asks judges to match against, and what MODE is configured with.
    # `id` is an internal UUID and is deliberately not the identity used here.
    account_id = str(
        raw.get("account_number") or raw.get("account_id") or raw.get("id") or ""
    ).strip()
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


def underlying_price(mcp: AlpacaMCP, symbol: str) -> float:
    """Best-effort spot for the underlying. 0.0 when unavailable.

    Breach detection needs a live spot, but the desk's required capability set
    deliberately does not include a stock quote -- adding one would make the
    startup gate fail on a server that omits it. So this probes optional tools
    and degrades: a 0.0 spot disables breach detection only, leaving the DTE,
    profit and stop rules fully in force.
    """
    for tool, extract in (
        ("get_stock_latest_trade", lambda d: d.get("p") or d.get("price")),
        ("get_stock_latest_quote", lambda d: ((d.get("bp") or 0) + (d.get("ap") or 0)) / 2 or None),
    ):
        payload = mcp.try_call(tool, symbols=symbol)
        if not isinstance(payload, Mapping):
            continue
        # Responses nest per symbol under "trade"/"quote" or by ticker.
        for candidate in (payload.get("trade"), payload.get("quote"),
                          (payload.get("trades") or {}).get(symbol) if isinstance(payload.get("trades"), Mapping) else None,
                          (payload.get("quotes") or {}).get(symbol) if isinstance(payload.get("quotes"), Mapping) else None,
                          payload.get(symbol), payload):
            if isinstance(candidate, Mapping):
                value = _f_or_none(extract(candidate))
                if value and value > 0:
                    return value
    return 0.0


def _f_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rebuild_positions(positions: Sequence[Mapping[str, Any]], spot: float = 0.0) -> list:
    """Rebuild managed PositionStates from the broker's own option legs.

    Without this the lifecycle layer receives nothing and take-profit, the stop,
    defence, rolls and the mandatory force-flatten never run. The desk would open
    positions and abandon them.

    Legs are grouped by underlying and expiry. Prices come from the broker, so
    entry credit and cost-to-close are its numbers, not the desk's memory.
    """
    from .rolldesk.lifecycle import PositionState
    from .types import Leg, Ticket

    grouped: dict[tuple[str, date], list[Mapping[str, Any]]] = {}
    for pos in positions:
        try:
            contract = parse_option_symbol(str(pos.get("symbol", "")))
        except OCCError:
            continue
        grouped.setdefault((contract.underlying, contract.expiry), []).append(pos)

    out = []
    for (underlying, expiry), legs in sorted(grouped.items()):
        built: list[Leg] = []
        entry_credit = 0.0
        cost_to_close = 0.0
        lots = 1
        usable = True

        for pos in legs:
            qty = _f_or_none(pos.get("qty"))
            entry = _f_or_none(pos.get("avg_entry_price"))
            current = _f_or_none(pos.get("current_price"))
            if qty is None or entry is None or qty == 0:
                usable = False
                break
            size = abs(qty)
            lots = max(lots, int(size))
            mark = current if current is not None else entry
            if qty < 0:  # short: credit received at entry, costs money to buy back
                built.append(Leg(str(pos["symbol"]), "sell", "sell_to_open", 1))
                entry_credit += entry * CONTRACT_MULTIPLIER * size
                cost_to_close += mark * CONTRACT_MULTIPLIER * size
            else:  # long: paid at entry, returns money when sold
                built.append(Leg(str(pos["symbol"]), "buy", "buy_to_open", 1))
                entry_credit -= entry * CONTRACT_MULTIPLIER * size
                cost_to_close -= mark * CONTRACT_MULTIPLIER * size

        if not usable or len(built) not in (2, 4):
            continue

        structure = "put_credit_spread" if len(built) == 2 else "iron_condor"
        out.append(
            PositionState(
                ticket=Ticket(
                    ticket_id=f"{underlying}-{expiry.isoformat()}",
                    underlying=underlying,
                    structure_type=structure,
                    expiry=expiry.isoformat(),
                    dte=0,
                    legs=tuple(built),
                    credit_mid=0.0,
                    width=0.0,
                    max_loss=0.0,
                    short_delta=0.0,
                    quote_age_ms=0,
                    regime="MANAGED",
                    proposed_lots=lots,
                ),
                lots=lots,
                entry_credit=entry_credit,
                cost_to_close=max(0.0, cost_to_close),
                underlying_price=spot,
                opened_at=datetime.now(timezone.utc),
            )
        )
    return out


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

    # The clock carries next_close, not a minutes remaining field. Reading a
    # missing key as 0 would put every pass inside the final-session window and
    # the Risk Officer would refuse every entry, silently.
    market_open, minutes_to_close = market_clock(mcp)

    structures = group_positions_into_structures(as_list(mcp.get_positions()))
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
