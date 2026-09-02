"""Candidate generation (spec sections 24 and 25).

Roll Desk builds fully specified, defined-risk tickets from an option chain.
This module never places an order and never asks a model anything: it produces
a shortlist that deterministic code has already proven structurally valid.

Diversity matters (spec section 25): three near-identical tickets give the
ranking layer nothing to choose between, so candidates are spread across short
delta and width, and near-duplicates are dropped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Sequence

from ..config import Config
from ..types import Leg, Permission, Quote, StructureType, Ticket
from .structures import StructureError, derive_geometry

#: Short-leg delta targets, from far out-of-the-money to near the money. These
#: drive candidate DIVERSITY, not risk: every resulting ticket still faces the
#: full Risk Officer. Deliberately module-level rather than in config.yaml,
#: which the spec freezes.
TARGET_SHORT_DELTAS: tuple[float, ...] = (0.16, 0.25, 0.32)

#: Spread widths to attempt, in strike points, narrowest first.
CANDIDATE_WIDTHS: tuple[float, ...] = (1.0, 2.0, 5.0)

#: A leg wider than this fraction of its own mid is too illiquid to build on.
#: The Risk Officer applies the stricter spec rule; this is a cheap pre-filter
#: so obviously untradeable strikes never become candidates.
MAX_LEG_RELATIVE_SPREAD = 0.35

#: Two candidates whose short strikes and width both match are duplicates.
MAX_CANDIDATES_DEFAULT = 3

#: Build from the furthest eligible expiry rather than the richest one.
#:
#: Set at the operator's instruction on 2026-09-01: positions are to be held
#: through the results announcement, not resolved inside the submission window.
#: Ranking purely by credit-to-width favours near-dated expiries (a live run
#: produced Sept 9 and Sept 10 candidates when Sept 18 was the intent), so the
#: builder walks expiries from longest to shortest and stops at the first that
#: yields viable structures.
#:
#: Turned back OFF on 2026-09-01 once the Sept 18 position was established. With
#: it on, every pass targeted Sept 18, and the overlapping-short check correctly
#: refused to stack a second structure on an expiry already held -- so the desk
#: deadlocked and would never have traded again. Off, it works the nearer
#: expiries while the long-dated position runs, which is the point of holding it.
PREFER_LONGEST_EXPIRY = False


@dataclass(frozen=True)
class ChainContract:
    """One option contract as read through MCP, normalised.

    `delta` is the broker's greek where available. It is used only to pick
    strikes; nothing downstream trusts it for risk.
    """

    symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    right: str  # "C" or "P"
    bid: float
    ask: float
    ts: datetime
    delta: float | None = None
    implied_volatility: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def is_two_sided(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def relative_spread(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / self.mid

    def as_quote(self) -> Quote:
        return Quote(symbol=self.symbol, bid=self.bid, ask=self.ask, ts=self.ts)


def _liquid(contract: ChainContract) -> bool:
    return contract.is_two_sided and contract.relative_spread <= MAX_LEG_RELATIVE_SPREAD


def eligible_expiries(chain: Iterable[ChainContract], config: Config, today: date) -> list[date]:
    """Expiries inside the openable window.

    Uses `min_entry_dte`, which already excludes the forced-flatten zone, so the
    builder never spends effort on an expiry the Risk Officer must reject.
    """
    out = {
        c.expiry
        for c in chain
        if config.min_entry_dte <= (c.expiry - today).days <= config.max_entry_dte
    }
    return sorted(out)


def _nearest_by_delta(
    contracts: Sequence[ChainContract], target_abs_delta: float
) -> ChainContract | None:
    """Pick the contract whose |delta| is closest to target. None if no deltas."""
    with_delta = [c for c in contracts if c.delta is not None]
    if not with_delta:
        return None
    return min(with_delta, key=lambda c: abs(abs(c.delta) - target_abs_delta))


def _nearest_by_strike(
    contracts: Sequence[ChainContract], target_strike: Decimal
) -> ChainContract | None:
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(c.strike - target_strike))


def _ticket_id(underlying: str, structure: str, expiry: date, leg_symbols: Sequence[str]) -> str:
    """Stable, content-derived id so the same structure always gets the same id.

    Hashes EVERY leg, not just the short strikes: two condors can share their
    short strikes and differ only in wing width, and the ranking layer picks by
    id, so a collision would make the model's choice ambiguous.
    """
    payload = f"{underlying}|{structure}|{expiry.isoformat()}|{'|'.join(sorted(leg_symbols))}"
    return f"{structure[:3]}-{hashlib.sha1(payload.encode()).hexdigest()[:10]}"


def _build_ticket(
    legs: tuple[Leg, ...],
    contracts: dict[str, ChainContract],
    structure: StructureType,
    regime: str,
    today: date,
    now: datetime,
    short_delta: float,
) -> Ticket | None:
    """Assemble a ticket, or None if the legs do not form a valid structure.

    Every field is computed here; none is copied from an upstream guess.
    """
    try:
        geometry = derive_geometry(legs)
    except StructureError:
        return None

    credit = 0.0
    for leg in legs:
        contract = contracts[leg.symbol]
        value = contract.mid * leg.ratio_qty
        credit += value if leg.side == "sell" else -value
    if credit <= 0:
        return None

    width = float(geometry.width)
    if width <= 0 or credit >= width:
        return None

    quote_age_ms = max((now - contracts[leg.symbol].ts).total_seconds() * 1000 for leg in legs)
    dte = (geometry.expiry - today).days

    return Ticket(
        ticket_id=_ticket_id(
            geometry.underlying,
            structure.value,
            geometry.expiry,
            [leg.symbol for leg in legs],
        ),
        underlying=geometry.underlying,
        structure_type=structure.value,
        expiry=geometry.expiry.isoformat(),
        dte=dte,
        legs=legs,
        credit_mid=round(credit, 4),
        width=width,
        max_loss=round((width - credit) * 100, 2),  # one lot; the officer sizes
        short_delta=round(short_delta, 4),
        quote_age_ms=int(quote_age_ms),
        regime=regime,
        proposed_lots=1,
        model_note=None,
    )


def build_put_credit_spreads(
    chain: Sequence[ChainContract],
    expiry: date,
    regime: str,
    today: date,
    now: datetime,
) -> list[Ticket]:
    """Sell a put, buy a further-out-of-the-money put against it."""
    puts = sorted(
        [c for c in chain if c.right == "P" and c.expiry == expiry and _liquid(c)],
        key=lambda c: c.strike,
    )
    if len(puts) < 2:
        return []
    by_symbol = {c.symbol: c for c in puts}

    tickets: list[Ticket] = []
    for target in TARGET_SHORT_DELTAS:
        short = _nearest_by_delta(puts, target)
        if short is None:
            continue
        for width in CANDIDATE_WIDTHS:
            long = _nearest_by_strike(
                [c for c in puts if c.strike < short.strike], short.strike - Decimal(str(width))
            )
            if long is None or long.strike >= short.strike:
                continue
            legs = (
                Leg(short.symbol, "sell", "sell_to_open", 1),
                Leg(long.symbol, "buy", "buy_to_open", 1),
            )
            ticket = _build_ticket(
                legs,
                by_symbol,
                StructureType.PUT_CREDIT_SPREAD,
                regime,
                today,
                now,
                abs(short.delta or 0.0),
            )
            if ticket is not None:
                tickets.append(ticket)
    return tickets


def build_iron_condors(
    chain: Sequence[ChainContract],
    expiry: date,
    regime: str,
    today: date,
    now: datetime,
) -> list[Ticket]:
    """Sell a put spread and a call spread on the same expiry."""
    live = [c for c in chain if c.expiry == expiry and _liquid(c)]
    puts = sorted([c for c in live if c.right == "P"], key=lambda c: c.strike)
    calls = sorted([c for c in live if c.right == "C"], key=lambda c: c.strike)
    if len(puts) < 2 or len(calls) < 2:
        return []
    by_symbol = {c.symbol: c for c in live}

    tickets: list[Ticket] = []
    for target in TARGET_SHORT_DELTAS:
        short_put = _nearest_by_delta(puts, target)
        short_call = _nearest_by_delta(calls, target)
        if short_put is None or short_call is None:
            continue
        if short_put.strike >= short_call.strike:
            continue
        for width in CANDIDATE_WIDTHS:
            w = Decimal(str(width))
            long_put = _nearest_by_strike(
                [c for c in puts if c.strike < short_put.strike], short_put.strike - w
            )
            long_call = _nearest_by_strike(
                [c for c in calls if c.strike > short_call.strike], short_call.strike + w
            )
            if long_put is None or long_call is None:
                continue
            if long_put.strike >= short_put.strike or long_call.strike <= short_call.strike:
                continue
            legs = (
                Leg(long_put.symbol, "buy", "buy_to_open", 1),
                Leg(short_put.symbol, "sell", "sell_to_open", 1),
                Leg(short_call.symbol, "sell", "sell_to_open", 1),
                Leg(long_call.symbol, "buy", "buy_to_open", 1),
            )
            ticket = _build_ticket(
                legs,
                by_symbol,
                StructureType.IRON_CONDOR,
                regime,
                today,
                now,
                max(abs(short_put.delta or 0.0), abs(short_call.delta or 0.0)),
            )
            if ticket is not None:
                tickets.append(ticket)
    return tickets


def _diversify(tickets: Sequence[Ticket], limit: int) -> list[Ticket]:
    """Drop duplicates, then spread the shortlist across delta and width.

    Ranking by credit-to-width alone would return three variants of the same
    trade; taking the best of each (delta, width) bucket keeps the shortlist
    genuinely different.
    """
    seen: set[tuple] = set()
    unique: list[Ticket] = []
    for t in sorted(tickets, key=lambda t: -(t.credit_mid / t.width if t.width else 0)):
        key = (t.structure_type, t.expiry, tuple(leg.symbol for leg in t.legs))
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)

    buckets: dict[tuple, Ticket] = {}
    for t in unique:
        bucket = (round(t.short_delta, 2), t.width)
        buckets.setdefault(bucket, t)

    spread = sorted(
        buckets.values(), key=lambda t: -(t.credit_mid / t.width if t.width else 0)
    )
    if len(spread) >= limit:
        return spread[:limit]
    # Not enough distinct buckets; top up from the remaining unique tickets.
    for t in unique:
        if t not in spread:
            spread.append(t)
        if len(spread) >= limit:
            break
    return spread[:limit]


def generate_candidates(
    chain: Sequence[ChainContract],
    permission: Permission,
    config: Config,
    today: date,
    now: datetime,
    limit: int = MAX_CANDIDATES_DEFAULT,
    prefer_longest_expiry: bool = PREFER_LONGEST_EXPIRY,
) -> list[Ticket]:
    """Produce a diverse, structurally valid shortlist for the current permission.

    Returns an empty list when the regime permits nothing. That is the normal
    NO_CANDIDATE path (spec section 18) -- MOMENTUM and EVENT are not special-cased,
    they simply arrive with no allowed strategies.
    """
    if not permission.allowed_strategies or permission.max_lots < 1:
        return []

    expiries = eligible_expiries(chain, config, today)
    if prefer_longest_expiry:
        # Longest first, and stop at the first expiry that actually yields
        # tradeable structures, so a thin far-dated ladder cannot strand the desk.
        expiries = sorted(expiries, reverse=True)

    def viable_for(chosen: Sequence[date]) -> list[Ticket]:
        tickets: list[Ticket] = []
        for expiry in chosen:
            if StructureType.PUT_CREDIT_SPREAD.value in permission.allowed_strategies:
                tickets += build_put_credit_spreads(chain, expiry, permission.regime, today, now)
            if StructureType.IRON_CONDOR.value in permission.allowed_strategies:
                tickets += build_iron_condors(chain, expiry, permission.regime, today, now)
        # Only keep what clears the regime's own credit floor, so the shortlist
        # is not padded with tickets the Risk Officer will certainly deny.
        return [
            t
            for t in tickets
            if t.width > 0 and (t.credit_mid / t.width) >= config.min_credit_to_width
        ]

    if prefer_longest_expiry:
        for expiry in expiries:
            found = viable_for([expiry])
            if found:
                return _diversify(found, limit)
        return []

    return _diversify(viable_for(expiries), limit)
