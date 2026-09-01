"""Leg ratio reduction (spec section 22).

Every ticket's leg ratios must reduce so the GCD across all legs is exactly 1.
2:4 becomes 1:2. Zero or negative ratios are invalid, not reducible.
"""

from __future__ import annotations

from math import gcd
from typing import Sequence


class RatioError(ValueError):
    """Raised for zero, negative, non-integer, or empty ratio sets."""


def _validate(ratios: Sequence[int]) -> tuple[int, ...]:
    if not ratios:
        raise RatioError("ratio set is empty")
    out = []
    for r in ratios:
        if isinstance(r, bool) or not isinstance(r, int):
            raise RatioError(f"ratio must be an int, got {r!r}")
        if r <= 0:
            raise RatioError(f"ratio must be positive, got {r}")
        out.append(r)
    return tuple(out)


def ratio_gcd(ratios: Sequence[int]) -> int:
    """GCD across the ratio set. Raises RatioError on invalid input."""
    valid = _validate(ratios)
    g = 0
    for r in valid:
        g = gcd(g, r)
    return g


def reduce_ratios(ratios: Sequence[int]) -> tuple[int, ...]:
    """Divide the ratio set by its GCD. reduce_ratios([2,4]) -> (1, 2)."""
    valid = _validate(ratios)
    g = ratio_gcd(valid)
    return tuple(r // g for r in valid)


def is_reduced(ratios: Sequence[int]) -> bool:
    """True when the GCD across all ratios is exactly 1. Invalid sets are not reduced."""
    try:
        return ratio_gcd(ratios) == 1
    except RatioError:
        return False
