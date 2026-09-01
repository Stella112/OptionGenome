"""Signal calculation tests (spec section 15)."""

from __future__ import annotations

import math

import pytest

from src.marketdna.indicators import (
    IndicatorError,
    adx,
    ema,
    iv_rank,
    realized_vol,
    realized_vol_median,
)


def trending_series(n: int = 120, start: float = 400.0, step: float = 2.0):
    """A clean uptrend: ADX should read high."""
    closes = [start + step * i for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


def choppy_series(n: int = 120, start: float = 400.0, amplitude: float = 2.0):
    """A mean-reverting sawtooth: ADX should read low."""
    closes = [start + amplitude * math.sin(i) for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    return highs, lows, closes


# --- EMA ---------------------------------------------------------------------


def test_ema_of_a_flat_series_is_that_value():
    assert ema([100.0] * 60, 20) == pytest.approx(100.0)


def test_ema_tracks_a_rising_series_below_the_last_price():
    closes = [float(i) for i in range(1, 101)]
    value = ema(closes, 20)
    assert value < closes[-1]
    assert value > closes[-40]


def test_shorter_ema_reacts_faster_than_longer():
    _, _, closes = trending_series()
    assert ema(closes, 20) > ema(closes, 50)


def test_ema_rejects_insufficient_data():
    with pytest.raises(IndicatorError):
        ema([1.0, 2.0], 20)


def test_ema_rejects_invalid_period():
    with pytest.raises(IndicatorError):
        ema([1.0] * 10, 0)


# --- ADX ---------------------------------------------------------------------


def test_adx_is_high_in_a_clean_trend():
    assert adx(*trending_series()) > 50


def test_adx_is_low_in_a_choppy_tape():
    assert adx(*choppy_series()) < 25


def test_adx_is_direction_agnostic():
    """A downtrend is just as trending as an uptrend."""
    up = adx(*trending_series())
    highs, lows, closes = trending_series(step=-2.0)
    assert adx(highs, lows, closes) == pytest.approx(up, rel=0.15)


def test_adx_stays_within_bounds():
    for series in (trending_series(), choppy_series()):
        assert 0 <= adx(*series) <= 100


def test_adx_rejects_insufficient_bars():
    with pytest.raises(IndicatorError):
        adx([1.0] * 10, [1.0] * 10, [1.0] * 10)


def test_adx_of_a_flat_series_is_zero():
    flat = [100.0] * 60
    assert adx(flat, flat, flat) == pytest.approx(0.0)


# --- realized volatility -----------------------------------------------------


def test_realized_vol_of_a_flat_series_is_zero():
    assert realized_vol([100.0] * 30) == pytest.approx(0.0)


def test_realized_vol_rises_with_dispersion():
    calm = [100.0 + 0.1 * (-1) ** i for i in range(40)]
    wild = [100.0 + 5.0 * (-1) ** i for i in range(40)]
    assert realized_vol(wild) > realized_vol(calm)


def test_realized_vol_is_annualized():
    """A 1% daily alternation annualizes to a large number, not a small one."""
    series = [100.0 * (1.01 ** (i % 2)) for i in range(40)]
    assert realized_vol(series) > 0.10


def test_realized_vol_rejects_short_series():
    with pytest.raises(IndicatorError):
        realized_vol([100.0] * 5)


def test_realized_vol_rejects_non_positive_prices():
    with pytest.raises(IndicatorError):
        realized_vol([100.0] * 20 + [0.0])


def test_realized_vol_median_is_stable_on_a_steady_series():
    _, _, closes = trending_series(n=200)
    median = realized_vol_median(closes)
    assert median >= 0
    assert median == pytest.approx(realized_vol(closes), rel=0.6)


# --- IV rank -----------------------------------------------------------------


@pytest.mark.parametrize(
    "current,history,expected",
    [
        (0.10, [0.10, 0.30], 0.0),  # at the low
        (0.30, [0.10, 0.30], 100.0),  # at the high
        (0.20, [0.10, 0.30], 50.0),  # midpoint
        (0.15, [0.10, 0.30], 25.0),
    ],
)
def test_iv_rank_positions_within_the_range(current, history, expected):
    assert iv_rank(current, history) == pytest.approx(expected)


def test_iv_rank_clamps_outside_the_historical_range():
    assert iv_rank(0.05, [0.10, 0.30]) == 0.0
    assert iv_rank(0.99, [0.10, 0.30]) == 100.0


def test_iv_rank_of_a_flat_history_is_neutral():
    """No range means no rank; 50 is honest, 0 or 100 would not be."""
    assert iv_rank(0.20, [0.20, 0.20, 0.20]) == 50.0


def test_iv_rank_rejects_empty_history():
    with pytest.raises(IndicatorError):
        iv_rank(0.20, [])
