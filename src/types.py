"""Immutable contracts passed between layers. No behaviour that touches a broker.

The Ticket (spec section 20) is the unit of exchange. Once a ticket is handed to
the ranking layer it is frozen: the model may return only a candidate ID.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping

CONTRACT_MULTIPLIER = 100


class Regime(str, Enum):
    INCOME = "INCOME"
    COMPRESSION = "COMPRESSION"
    MOMENTUM = "MOMENTUM"
    EVENT = "EVENT"


class StructureType(str, Enum):
    PUT_CREDIT_SPREAD = "put_credit_spread"
    IRON_CONDOR = "iron_condor"


#: The global allow-list. Anything not in here is DENY, unconditionally (spec section 9.3).
ALLOWED_STRUCTURES: frozenset[str] = frozenset(
    {StructureType.PUT_CREDIT_SPREAD.value, StructureType.IRON_CONDOR.value}
)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionIntent(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"

    @property
    def is_open(self) -> bool:
        return self in (PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN)

    @property
    def side(self) -> Side:
        return Side.BUY if self.name.startswith("BUY") else Side.SELL


class SystemState(str, Enum):
    BOOTING = "BOOTING"
    READY = "READY"
    FLATTEN_ONLY = "FLATTEN_ONLY"
    HALTED = "HALTED"


@dataclass(frozen=True)
class Quote:
    """A validated two-sided market for one contract."""

    symbol: str
    bid: float
    ask: float
    ts: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def is_two_sided(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def age_ms(self, now: datetime) -> float:
        return (now - self.ts).total_seconds() * 1000.0


@dataclass(frozen=True)
class Leg:
    """One leg of a multi-leg ticket. Exactly the fields Alpaca's mleg order takes."""

    symbol: str
    side: str
    position_intent: str
    ratio_qty: int

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "position_intent": self.position_intent,
            "ratio_qty": self.ratio_qty,
        }


@dataclass(frozen=True)
class Ticket:
    """Spec section 20. Immutable once handed to the ranking layer.

    Every safety-critical field here is a CLAIM, not a fact. The Risk Officer
    recalculates width, credit, max_loss, dte, structure_type and the leg
    relationships from the legs and validated quotes (spec section 12).
    """

    ticket_id: str
    underlying: str
    structure_type: str
    expiry: str  # YYYY-MM-DD
    dte: int
    legs: tuple[Leg, ...]
    credit_mid: float
    width: float
    max_loss: float
    short_delta: float
    quote_age_ms: int
    regime: str
    proposed_lots: int
    model_note: str | None = None

    @property
    def expiry_date(self) -> date:
        return date.fromisoformat(self.expiry)

    @property
    def ratios(self) -> tuple[int, ...]:
        return tuple(leg.ratio_qty for leg in self.legs)

    def with_model_note(self, note: str | None) -> "Ticket":
        """The only mutation the ranking layer may cause: attaching a rationale."""
        return dataclasses.replace(self, model_note=note)

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "underlying": self.underlying,
            "structure_type": self.structure_type,
            "expiry": self.expiry,
            "dte": self.dte,
            "legs": [leg.as_dict() for leg in self.legs],
            "credit_mid": self.credit_mid,
            "width": self.width,
            "max_loss": self.max_loss,
            "short_delta": self.short_delta,
            "quote_age_ms": self.quote_age_ms,
            "regime": self.regime,
            "proposed_lots": self.proposed_lots,
            "model_note": self.model_note,
        }

    def for_ranking(self) -> dict:
        """Exactly the fields the model may see (spec section 26).

        Deliberately omits: account balance, buying power, max account risk,
        daily risk budget, allowed lots, and every Risk Officer rule.
        """
        return {
            "candidate_id": self.ticket_id,
            "underlying": self.underlying,
            "structure": self.structure_type,
            "expiry": self.expiry,
            "strikes": [leg.symbol for leg in self.legs],
            "credit": round(self.credit_mid, 4),
            "width": round(self.width, 4),
            "max_loss": round(self.max_loss, 2),
            "short_delta": round(self.short_delta, 4),
        }


@dataclass(frozen=True)
class Permission:
    """MarketDNA's entire output (spec section 18). Nothing else crosses the boundary."""

    regime: str
    allowed_strategies: tuple[str, ...]
    max_lots: int
    reasons: tuple[str, ...]

    def permits(self, structure_type: str) -> bool:
        return structure_type in self.allowed_strategies and self.max_lots > 0

    def as_dict(self) -> dict:
        return {
            "regime": self.regime,
            "allowed_strategies": list(self.allowed_strategies),
            "max_lots": self.max_lots,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class OpenStructure:
    """An open defined-risk structure as the desk's book records it."""

    structure_id: str
    underlying: str
    structure_type: str
    expiry: date
    short_strikes: tuple[Decimal, ...]
    lots: int
    entry_credit: float  # dollars received
    max_loss: float  # dollars at risk
    opened_at: datetime
    roll_count: int = 0


@dataclass(frozen=True)
class Account:
    """Reconciled account identity and capital. Sourced through MCP only."""

    account_id: str
    allowed_account_id: str  # configured DEV/JUDGE id; the officer asserts equality
    equity: float
    cash: float
    buying_power: float
    options_level: int
    start_of_day_equity: float
    high_water_mark: float

    @property
    def drawdown(self) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, 1 - self.equity / self.high_water_mark)


@dataclass(frozen=True)
class Book:
    """Desk state at decision time: open risk, validated quotes, session clock."""

    open_structures: tuple[OpenStructure, ...] = ()
    day_open_risk: float = 0.0
    quotes: Mapping[str, Quote] = field(default_factory=dict)
    now: datetime | None = None
    market_open: bool = True
    minutes_to_close: int = 390

    @property
    def open_count(self) -> int:
        return len(self.open_structures)


@dataclass(frozen=True)
class RiskDecision:
    """The only verdict shape. ALLOW or DENY, never a maybe (spec section 11)."""

    decision: str  # "ALLOW" | "DENY"
    reasons: tuple[str, ...] = ()
    allowed_lots: int = 0

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "allowed_lots": self.allowed_lots,
        }
