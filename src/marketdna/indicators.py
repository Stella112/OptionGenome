"""Deterministic signal calculation (spec section 15).

EMA20, EMA50, ADX(14), 20-day realized volatility, its 100-day median, and IV
rank over a trailing one-year IV range. Pure functions over price series: no
model may influence any value here, and none of these functions touch a broker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Sequence

TRADING_DAYS = 252
RV_WINDOW = 20
RV_MEDIAN_WINDOW = 100
ADX_PERIOD = 14


class IndicatorError(ValueError):
    """Raised when a series is too short or malformed to produce a signal."""


@dataclass(frozen=True)
class Signals:
    """Everything MarketDNA needs to classify a regime. No prices, no orders."""

    ema20: float
    ema50: float
    adx: float
    realized_vol: float
    realized_vol_median: float
    implied_vol: float
    iv_rank: float
    hours_to_next_event: float
    next_event_name: str

    @property
    def trend_up(self) -> bool:
        return self.ema20 > self.ema50

    def as_dict(self) -> dict:
        return {
            "ema20": round(self.ema20, 4),
            "ema50": round(self.ema50, 4),
            "adx": round(self.adx, 4),
            "realized_vol": round(self.realized_vol, 6),
            "realized_vol_median": round(self.realized_vol_median, 6),
            "implied_vol": round(self.implied_vol, 6),
            "iv_rank": round(self.iv_rank, 4),
            "hours_to_next_event": round(self.hours_to_next_event, 3),
            "next_event_name": self.next_event_name,
        }


def ema(values: Sequence[float], period: int) -> float:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    if period < 1:
        raise IndicatorError(f"ema period must be >= 1, got {period}")
    if len(values) < period:
        raise IndicatorError(f"ema({period}) needs {period} values, got {len(values)}")

    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    result = seed
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    return [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothing: seed with the sum of the first `period`, then decay."""
    if len(values) < period:
        raise IndicatorError(f"wilder smoothing needs {period} values, got {len(values)}")
    smoothed = [sum(values[:period])]
    for value in values[period:]:
        smoothed.append(smoothed[-1] - smoothed[-1] / period + value)
    return smoothed


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = ADX_PERIOD,
) -> float:
    """Wilder's ADX on a 0-100 scale. Needs 2 * period + 1 bars.

    High ADX means a directional tape, which is exactly when short premium is
    least welcome, so this gates the MOMENTUM regime.
    """
    n = min(len(highs), len(lows), len(closes))
    required = 2 * period + 1
    if n < required:
        raise IndicatorError(f"adx({period}) needs {required} bars, got {n}")
    highs, lows, closes = list(highs[-n:]), list(lows[-n:]), list(closes[-n:])

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    tr = true_ranges(highs, lows, closes)

    sm_tr = _wilder_smooth(tr, period)
    sm_plus = _wilder_smooth(plus_dm, period)
    sm_minus = _wilder_smooth(minus_dm, period)

    dx: list[float] = []
    for t, p, m in zip(sm_tr, sm_plus, sm_minus):
        if t <= 0:
            dx.append(0.0)
            continue
        di_plus = 100 * p / t
        di_minus = 100 * m / t
        denom = di_plus + di_minus
        dx.append(0.0 if denom <= 0 else 100 * abs(di_plus - di_minus) / denom)

    if len(dx) < period:
        # Not enough DX points to average a full ADX period; fall back to the
        # mean of what exists rather than inventing a value.
        return sum(dx) / len(dx) if dx else 0.0
    return sum(dx[-period:]) / period


def realized_vol(closes: Sequence[float], window: int = RV_WINDOW) -> float:
    """Annualized close-to-close volatility over the trailing `window` returns."""
    if len(closes) < window + 1:
        raise IndicatorError(f"realized_vol({window}) needs {window + 1} closes, got {len(closes)}")
    tail = list(closes[-(window + 1) :])
    rets = []
    for a, b in zip(tail, tail[1:]):
        if a <= 0 or b <= 0:
            raise IndicatorError("realized_vol requires strictly positive closes")
        rets.append(math.log(b / a))
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)


def realized_vol_series(closes: Sequence[float], window: int = RV_WINDOW) -> list[float]:
    """Rolling realized vol, one value per bar once the window is filled."""
    out = []
    for end in range(window + 1, len(closes) + 1):
        out.append(realized_vol(closes[:end], window))
    return out


def realized_vol_median(closes: Sequence[float], lookback: int = RV_MEDIAN_WINDOW) -> float:
    """Median of the rolling realized-vol series over the trailing `lookback` values."""
    series = realized_vol_series(closes)
    if not series:
        raise IndicatorError("not enough closes for a realized-vol median")
    return median(series[-lookback:])


def iv_rank(current_iv: float, iv_history: Sequence[float]) -> float:
    """Position of current IV within its trailing one-year range, 0-100.

    Rank, not percentile: (iv - min) / (max - min). A flat history has no range
    to rank against, so it returns 50 rather than a misleading 0 or 100.
    """
    history = [v for v in iv_history if v is not None and v > 0]
    if not history:
        raise IndicatorError("iv_rank requires a non-empty IV history")
    lo, hi = min(history), max(history)
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, 100 * (current_iv - lo) / (hi - lo)))


def build_signals(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    current_iv: float,
    iv_history: Sequence[float],
    hours_to_next_event: float,
    next_event_name: str = "",
) -> Signals:
    """Assemble the full signal set. Raises IndicatorError on insufficient data."""
    return Signals(
        ema20=ema(closes, 20),
        ema50=ema(closes, 50),
        adx=adx(highs, lows, closes),
        realized_vol=realized_vol(closes),
        realized_vol_median=realized_vol_median(closes),
        implied_vol=current_iv,
        iv_rank=iv_rank(current_iv, iv_history),
        hours_to_next_event=hours_to_next_event,
        next_event_name=next_event_name,
    )
