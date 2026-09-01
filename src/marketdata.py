"""Market data: MCP payloads in, typed desk objects out.

Everything the desk reads about the market passes through here, so the shape of
Alpaca's responses is decoded in exactly one place. Nothing in this module
places an order or makes a decision.

Snapshot shape, verified against the live server:

    "SPY260903P00755000": {
        "greeks":      {"delta": -0.2747, "gamma": ..., ...},
        "latestQuote": {"bp": 1.20, "ap": 1.25, "t": "...", "bs": 12, "as": 40},
        "dailyBar":    {"o":..., "h":..., "l":..., "c":..., "v":...}
    }
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .broker.alpaca_mcp import AlpacaMCP
from .broker.occ import OCCError, parse_option_symbol
from .config import Config
from .marketdata_types import Bar
from .rolldesk.candidates import ChainContract

#: Alpaca returns at most this many snapshots per page.
PAGE_LIMIT = 1000
MAX_PAGES = 12


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _timestamp(raw: Any) -> datetime:
    """Parse an RFC3339 timestamp, falling back to now when absent."""
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def snapshot_to_contract(symbol: str, snapshot: Mapping[str, Any]) -> ChainContract | None:
    """Convert one chain snapshot. None when it cannot be traded on.

    A contract with no two-sided quote is dropped here rather than being carried
    forward as a zero-priced candidate.
    """
    try:
        contract = parse_option_symbol(symbol)
    except OCCError:
        return None

    quote = snapshot.get("latestQuote") or {}
    bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None

    greeks = snapshot.get("greeks") or {}
    delta = _f(greeks.get("delta"))

    return ChainContract(
        symbol=symbol,
        underlying=contract.underlying,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right,
        bid=bid,
        ask=ask,
        ts=_timestamp(quote.get("t")),
        delta=delta,
    )


def parse_chain(payload: Any) -> list[ChainContract]:
    """Decode a chain payload into tradeable contracts."""
    if not isinstance(payload, Mapping):
        return []
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, Mapping):
        # Some responses are the snapshot map itself.
        snapshots = {k: v for k, v in payload.items() if isinstance(v, Mapping) and "latestQuote" in v}

    out: list[ChainContract] = []
    for symbol, snapshot in snapshots.items():
        if not isinstance(snapshot, Mapping):
            continue
        contract = snapshot_to_contract(str(symbol), snapshot)
        if contract is not None:
            out.append(contract)
    return out


def fetch_chain(
    mcp: AlpacaMCP,
    underlying: str,
    config: Config,
    today: date | None = None,
) -> list[ChainContract]:
    """Fetch the option chain across the openable DTE window.

    The expiry filter is applied server-side so the desk pulls the window it can
    actually trade rather than the whole chain. Pages are followed to exhaustion,
    with a hard cap so a pathological response cannot loop forever.
    """
    today = today or datetime.now(timezone.utc).date()
    low = (today + timedelta(days=config.min_entry_dte)).isoformat()
    high = (today + timedelta(days=config.max_entry_dte)).isoformat()

    contracts: list[ChainContract] = []
    seen: set[str] = set()
    page_token: str | None = None

    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {
            "expiration_date_gte": low,
            "expiration_date_lte": high,
            "limit": PAGE_LIMIT,
        }
        if page_token:
            params["page_token"] = page_token

        payload = mcp.get_option_chain(underlying, **params)
        for contract in parse_chain(payload):
            if contract.symbol not in seen:
                seen.add(contract.symbol)
                contracts.append(contract)

        page_token = payload.get("next_page_token") if isinstance(payload, Mapping) else None
        if not page_token:
            break

    return contracts


def parse_bars(payload: Any, symbol: str) -> list[Bar]:
    """Decode daily bars for one symbol, oldest first."""
    if not isinstance(payload, Mapping):
        return []
    bars = payload.get("bars")
    if isinstance(bars, Mapping):
        rows = bars.get(symbol) or next(iter(bars.values()), [])
    elif isinstance(bars, list):
        rows = bars
    else:
        rows = []

    out: list[Bar] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        high, low, close = _f(row.get("h")), _f(row.get("l")), _f(row.get("c"))
        if None in (high, low, close):
            continue
        out.append(Bar(ts=_timestamp(row.get("t")), high=high, low=low, close=close))
    out.sort(key=lambda b: b.ts)
    return out


def fetch_daily_bars(mcp: AlpacaMCP, symbol: str, days: int = 260) -> list[Bar]:
    """Daily bars for the regime signals. Enough history for EMA50 and IV rank."""
    payload = mcp.get_bars(symbol, timeframe="1Day", days=days, limit=days, sort="asc")
    return parse_bars(payload, symbol)


def atm_implied_vol(contracts: Sequence[ChainContract], spot: float) -> float | None:
    """Implied vol of the nearest-the-money contract, if the chain carries it."""
    if not contracts or spot <= 0:
        return None
    nearest = min(contracts, key=lambda c: abs(float(c.strike) - spot))
    return getattr(nearest, "implied_volatility", None)


def market_clock(mcp: AlpacaMCP) -> tuple[bool, int]:
    """(is_open, minutes_to_close). Closed with zero minutes when unreadable."""
    clock = mcp.get_clock()
    if not isinstance(clock, Mapping):
        return False, 0
    is_open = bool(clock.get("is_open", False))

    minutes = 0
    close_raw = clock.get("next_close") or clock.get("close")
    now_raw = clock.get("timestamp")
    if is_open and isinstance(close_raw, str):
        close_at = _timestamp(close_raw)
        now = _timestamp(now_raw) if isinstance(now_raw, str) else datetime.now(timezone.utc)
        minutes = max(0, int((close_at - now).total_seconds() // 60))
    return is_open, minutes
