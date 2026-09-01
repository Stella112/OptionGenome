"""Small shared types for market data, kept separate to avoid an import cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bar:
    """One daily OHLC bar. Only the fields the regime signals actually use."""

    ts: datetime
    high: float
    low: float
    close: float
