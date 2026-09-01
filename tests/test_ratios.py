"""Ratio reduction tests (spec section 22)."""

from __future__ import annotations

import pytest

from src.rolldesk.ratios import RatioError, is_reduced, ratio_gcd, reduce_ratios


@pytest.mark.parametrize(
    "given,expected",
    [
        ([1, 1], (1, 1)),
        ([2, 4], (1, 2)),
        ([3, 6], (1, 2)),
        ([4, 2], (2, 1)),
        ([2, 2, 2, 2], (1, 1, 1, 1)),
        ([2, 4, 6, 8], (1, 2, 3, 4)),
        ([6, 9, 15], (2, 3, 5)),
        ([5], (1,)),
        ([7, 11], (7, 11)),  # coprime, already reduced
    ],
)
def test_reduce_ratios(given, expected):
    assert reduce_ratios(given) == expected


@pytest.mark.parametrize("given", [[1, 1], [1, 2], [2, 3], [1, 2, 3, 4], [7, 11]])
def test_already_reduced_sets_are_recognised(given):
    assert is_reduced(given)
    assert ratio_gcd(given) == 1


@pytest.mark.parametrize("given", [[2, 4], [3, 6], [2, 2], [4, 8, 12]])
def test_unreduced_sets_are_recognised(given):
    assert not is_reduced(given)
    assert ratio_gcd(given) > 1


def test_reduction_is_idempotent():
    assert reduce_ratios(reduce_ratios([2, 4, 6])) == reduce_ratios([2, 4, 6])


@pytest.mark.parametrize("given", [[2, 4], [3, 6], [6, 9, 15], [10, 20, 30]])
def test_reduced_output_always_has_gcd_one(given):
    assert ratio_gcd(reduce_ratios(given)) == 1


@pytest.mark.parametrize("bad", [[], [0], [1, 0], [-1], [2, -4], [1.5], [None], [True]])
def test_invalid_ratio_sets_are_rejected(bad):
    with pytest.raises(RatioError):
        reduce_ratios(bad)
    assert not is_reduced(bad)
