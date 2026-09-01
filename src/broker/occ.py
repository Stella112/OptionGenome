"""OCC option symbol construction and parsing (spec section 21).

Format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE * 1000, zero-padded to 8}
Example: SPY250127C00608000 -> SPY, 2025-01-27, CALL, 608.00

Symbols are built and parsed here and nowhere else. Scattered string
concatenation of option symbols is a build failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")

STRIKE_SCALE = 1000


class OCCError(ValueError):
    """Raised for any malformed OCC symbol or un-encodable contract."""


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: date
    right: str  # "C" or "P"
    strike: Decimal

    def __post_init__(self) -> None:
        if self.right not in ("C", "P"):
            raise OCCError(f"right must be C or P, got {self.right!r}")

    @property
    def symbol(self) -> str:
        return build_option_symbol(self.underlying, self.expiry, self.right, self.strike)


def build_option_symbol(
    underlying: str,
    expiry: date,
    right: str,
    strike: Decimal | float | str,
) -> str:
    """Encode a contract as an OCC symbol. Raises OCCError on anything unrepresentable."""
    root = (underlying or "").strip().upper()
    if not root or not root.isalpha() or len(root) > 6:
        raise OCCError(f"underlying must be 1-6 alphabetic characters, got {underlying!r}")

    right = (right or "").strip().upper()
    if right not in ("C", "P"):
        raise OCCError(f"right must be C or P, got {right!r}")

    if not isinstance(expiry, date):
        raise OCCError(f"expiry must be a date, got {type(expiry).__name__}")

    try:
        strike_dec = Decimal(str(strike))
    except (InvalidOperation, ValueError) as exc:
        raise OCCError(f"strike is not numeric: {strike!r}") from exc
    if strike_dec <= 0:
        raise OCCError(f"strike must be positive, got {strike_dec}")

    scaled = strike_dec * STRIKE_SCALE
    if scaled != scaled.to_integral_value():
        raise OCCError(f"strike {strike_dec} is finer than the 1/1000 OCC grid")
    scaled_int = int(scaled)
    if scaled_int > 99_999_999:
        raise OCCError(f"strike {strike_dec} exceeds the 8-digit OCC field")

    return f"{root}{expiry:%y%m%d}{right}{scaled_int:08d}"


def parse_option_symbol(symbol: str) -> OptionContract:
    """Decode an OCC symbol. Raises OCCError on anything malformed."""
    if not isinstance(symbol, str):
        raise OCCError(f"symbol must be a string, got {type(symbol).__name__}")
    match = _SYMBOL_RE.match(symbol.strip().upper())
    if not match:
        raise OCCError(f"malformed OCC symbol: {symbol!r}")

    ymd = match.group("ymd")
    try:
        expiry = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError as exc:
        raise OCCError(f"invalid expiry date in {symbol!r}: {exc}") from exc

    strike = Decimal(match.group("strike")) / STRIKE_SCALE
    if strike <= 0:
        raise OCCError(f"non-positive strike in {symbol!r}")

    return OptionContract(
        underlying=match.group("root"),
        expiry=expiry,
        right=match.group("right"),
        strike=strike,
    )


def is_option_symbol(symbol: str) -> bool:
    try:
        parse_option_symbol(symbol)
        return True
    except OCCError:
        return False
