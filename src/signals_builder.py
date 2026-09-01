"""Assemble MarketDNA's signal set from live market data.

Separated from marketdata.py (which only decodes) and from regime.py (which only
classifies), so each layer stays independently testable.

One honest caveat, stated in code rather than hidden: a true IV rank needs a
trailing year of implied vol, which the broker does not serve directly. Until
the journal has accumulated that history, IV rank is computed from the realized
volatility series as a documented proxy. Every pass records the day's ATM IV, so
the real measure takes over once enough history exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .journal import Journal
from .marketdata import Bar
from .marketdna.calendar import hours_to_next_event, load_events
from .marketdna.indicators import (
    Signals,
    adx,
    ema,
    iv_rank,
    realized_vol,
    realized_vol_median,
    realized_vol_series,
)

MIN_BARS = 60


class InsufficientData(RuntimeError):
    """Raised when there is not enough history to classify a regime honestly."""


def recorded_iv_history(journal: Journal, limit: int = 252) -> list[float]:
    """Implied vols this desk has previously observed, oldest first."""
    history: list[float] = []
    for entry in journal.read():
        if entry.get("event") != "REGIME":
            continue
        iv = (entry.get("signals") or {}).get("implied_vol")
        if isinstance(iv, (int, float)) and iv > 0:
            history.append(float(iv))
    return history[-limit:]


def build_signals(
    bars: Sequence[Bar],
    implied_vol: float | None,
    now: datetime,
    journal: Journal | None = None,
    events_path: str | None = None,
) -> Signals:
    """Build the full signal set. Raises InsufficientData rather than guessing."""
    if len(bars) < MIN_BARS:
        raise InsufficientData(f"need {MIN_BARS} daily bars for EMA50/ADX, have {len(bars)}")

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    rv = realized_vol(closes)
    rv_median = realized_vol_median(closes)

    # Fall back to realized vol when the chain carried no IV, so the regime is
    # still classified from something real rather than from zero.
    current_iv = implied_vol if (implied_vol and implied_vol > 0) else rv

    history = recorded_iv_history(journal) if journal is not None else []
    if len(history) >= 30:
        rank = iv_rank(current_iv, history)
    else:
        # Proxy: rank today's volatility within its own trailing range.
        rank = iv_rank(rv, realized_vol_series(closes) or [rv])

    hours, event_name = hours_to_next_event(now, load_events(events_path))

    return Signals(
        ema20=ema(closes, 20),
        ema50=ema(closes, 50),
        adx=adx(highs, lows, closes),
        realized_vol=rv,
        realized_vol_median=rv_median,
        implied_vol=current_iv,
        iv_rank=rank,
        hours_to_next_event=hours,
        next_event_name=event_name,
    )
