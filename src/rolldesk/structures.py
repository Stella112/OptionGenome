"""Structure validation (spec section 23) and independent geometry recalculation.

This module is the single source of truth for what a put credit spread and an
iron condor actually ARE. The Risk Officer calls in here to re-derive structure
type, width and maximum loss from the legs themselves rather than trusting the
values a ticket claims (spec section 12).

Pure: no network, no filesystem, no database, no broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..broker.occ import OCCError, OptionContract, parse_option_symbol
from ..types import CONTRACT_MULTIPLIER, Leg, PositionIntent, Side, StructureType
from .ratios import is_reduced


@dataclass(frozen=True)
class ParsedLeg:
    """A ticket leg joined to its decoded OCC contract."""

    leg: Leg
    contract: OptionContract

    @property
    def strike(self) -> Decimal:
        return self.contract.strike

    @property
    def right(self) -> str:
        return self.contract.right

    @property
    def is_short(self) -> bool:
        return self.leg.side == Side.SELL.value


@dataclass(frozen=True)
class Geometry:
    """What the legs actually describe, derived independently of the ticket's claims."""

    structure_type: str
    underlying: str
    expiry: date
    width: Decimal  # widest defined-risk wing, in strike points
    short_strikes: tuple[Decimal, ...]
    long_strikes: tuple[Decimal, ...]

    def max_loss_per_lot(self, credit_per_share: float) -> float:
        """Maximum theoretical loss for one lot, in dollars, given a net credit."""
        return (float(self.width) - credit_per_share) * CONTRACT_MULTIPLIER


class StructureError(ValueError):
    """Raised when legs do not form a permitted defined-risk structure."""


def parse_legs(legs: tuple[Leg, ...]) -> tuple[ParsedLeg, ...]:
    """Decode every leg symbol and validate the leg fields themselves."""
    if not legs:
        raise StructureError("ticket has no legs")

    parsed: list[ParsedLeg] = []
    for leg in legs:
        try:
            intent = PositionIntent(leg.position_intent)
        except ValueError as exc:
            raise StructureError(f"unknown position_intent {leg.position_intent!r}") from exc
        try:
            side = Side(leg.side)
        except ValueError as exc:
            raise StructureError(f"unknown side {leg.side!r}") from exc
        if intent.side is not side:
            raise StructureError(
                f"{leg.symbol}: side {leg.side!r} contradicts position_intent {leg.position_intent!r}"
            )
        if not intent.is_open:
            raise StructureError(
                f"{leg.symbol}: {leg.position_intent!r} is a closing intent on an opening ticket"
            )
        if not isinstance(leg.ratio_qty, int) or isinstance(leg.ratio_qty, bool) or leg.ratio_qty <= 0:
            raise StructureError(
                f"{leg.symbol}: ratio_qty must be a positive int, got {leg.ratio_qty!r}"
            )
        try:
            contract = parse_option_symbol(leg.symbol)
        except OCCError as exc:
            raise StructureError(str(exc)) from exc
        parsed.append(ParsedLeg(leg=leg, contract=contract))

    if len({p.contract.underlying for p in parsed}) != 1:
        raise StructureError("legs span multiple underlyings")
    if len({p.contract.expiry for p in parsed}) != 1:
        raise StructureError("legs span multiple expiries")
    if len({p.leg.symbol for p in parsed}) != len(parsed):
        raise StructureError("duplicate leg symbols")
    return tuple(parsed)


def _require_equal_ratios(parsed: tuple[ParsedLeg, ...]) -> None:
    if len({p.leg.ratio_qty for p in parsed}) != 1:
        raise StructureError("defined-risk verticals require equal ratios across all legs")
    if not is_reduced([p.leg.ratio_qty for p in parsed]):
        raise StructureError("leg ratios do not reduce to a GCD of 1")


def _validate_put_credit_spread(parsed: tuple[ParsedLeg, ...]) -> Geometry:
    """Exactly two puts: sell_to_open the higher strike, buy_to_open the lower."""
    if len(parsed) != 2:
        raise StructureError(f"put_credit_spread requires exactly 2 legs, got {len(parsed)}")
    if any(p.right != "P" for p in parsed):
        raise StructureError("put_credit_spread legs must both be puts")

    shorts = [p for p in parsed if p.is_short]
    longs = [p for p in parsed if not p.is_short]
    if len(shorts) != 1 or len(longs) != 1:
        raise StructureError("put_credit_spread requires one short leg and one long leg")
    _require_equal_ratios(parsed)

    short, long = shorts[0], longs[0]
    if short.strike <= long.strike:
        raise StructureError(
            f"put_credit_spread short strike {short.strike} must sit above long strike {long.strike}"
        )

    return Geometry(
        structure_type=StructureType.PUT_CREDIT_SPREAD.value,
        underlying=short.contract.underlying,
        expiry=short.contract.expiry,
        width=short.strike - long.strike,
        short_strikes=(short.strike,),
        long_strikes=(long.strike,),
    )


def _validate_iron_condor(parsed: tuple[ParsedLeg, ...]) -> Geometry:
    """Four legs: long protective put, short put, short call, long protective call."""
    if len(parsed) != 4:
        raise StructureError(f"iron_condor requires exactly 4 legs, got {len(parsed)}")
    _require_equal_ratios(parsed)

    puts = sorted([p for p in parsed if p.right == "P"], key=lambda p: p.strike)
    calls = sorted([p for p in parsed if p.right == "C"], key=lambda p: p.strike)
    if len(puts) != 2 or len(calls) != 2:
        raise StructureError("iron_condor requires exactly two puts and two calls")

    long_put, short_put = puts  # lower strike first
    short_call, long_call = calls  # lower strike first

    if long_put.is_short or not short_put.is_short:
        raise StructureError("iron_condor put wing must be long the lower strike, short the higher")
    if not short_call.is_short or long_call.is_short:
        raise StructureError("iron_condor call wing must be short the lower strike, long the higher")
    if short_put.strike >= short_call.strike:
        raise StructureError(
            f"iron_condor short put {short_put.strike} must sit below short call {short_call.strike}"
        )

    put_width = short_put.strike - long_put.strike
    call_width = long_call.strike - short_call.strike
    if put_width <= 0 or call_width <= 0:
        raise StructureError("iron_condor wings must both have positive width")

    # Only one side can lose at expiration, so risk is the wider wing.
    return Geometry(
        structure_type=StructureType.IRON_CONDOR.value,
        underlying=short_put.contract.underlying,
        expiry=short_put.contract.expiry,
        width=max(put_width, call_width),
        short_strikes=(short_put.strike, short_call.strike),
        long_strikes=(long_put.strike, long_call.strike),
    )


def derive_geometry(legs: tuple[Leg, ...]) -> Geometry:
    """Determine what these legs are, independent of any claimed structure_type.

    Raises StructureError for anything that is not a permitted defined-risk
    structure. Leg count drives dispatch, so an unknown shape can never be
    silently coerced into an allowed one.
    """
    parsed = parse_legs(legs)
    match len(parsed):
        case 2:
            return _validate_put_credit_spread(parsed)
        case 4:
            return _validate_iron_condor(parsed)
        case n:
            raise StructureError(f"no permitted defined-risk structure has {n} legs")


def validate_structure(legs: tuple[Leg, ...], claimed_type: str) -> Geometry:
    """Derive geometry and confirm it matches what the ticket claims."""
    geometry = derive_geometry(legs)
    if geometry.structure_type != claimed_type:
        raise StructureError(
            f"ticket claims {claimed_type!r} but legs describe {geometry.structure_type!r}"
        )
    return geometry
