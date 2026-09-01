"""Data-driven economic event calendar (spec section 19).

Events live in config/events.json. No event condition is ever inlined into
Python, so the EVENT gate can be unit-tested against injected fixtures without
waiting for a real release to print.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DEFAULT_EVENTS_PATH = Path("config/events.json")

#: Returned when no event remains on the calendar. Large enough that any sane
#: event_window_hours comparison is false, without reaching for infinity.
NO_EVENT_HOURS = 1e9


class CalendarError(ValueError):
    """Raised when events.json is missing, malformed, or holds an invalid entry."""


@dataclass(frozen=True)
class Event:
    name: str
    day: date
    time_et: time | None
    market_closed: bool = False

    @property
    def is_all_day(self) -> bool:
        return self.time_et is None

    def starts_at(self) -> datetime:
        """Event start in UTC.

        An all-day entry (a market holiday) is anchored to the ET session open,
        so a closed-market day still registers as an event window rather than
        being silently skipped.
        """
        clock = self.time_et or time(9, 30)
        return datetime.combine(self.day, clock, tzinfo=ET).astimezone(UTC)

    def hours_until(self, now: datetime) -> float:
        return (self.starts_at() - _as_utc(now)).total_seconds() / 3600.0


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise CalendarError("naive datetime: every timestamp crossing a boundary must be aware")
    return moment.astimezone(UTC)


def _parse_event(raw: dict, index: int) -> Event:
    if not isinstance(raw, dict):
        raise CalendarError(f"events[{index}] is not an object")
    for key in ("name", "date"):
        if key not in raw:
            raise CalendarError(f"events[{index}] is missing required key {key!r}")
    try:
        day = date.fromisoformat(str(raw["date"]))
    except ValueError as exc:
        raise CalendarError(f"events[{index}] has invalid date {raw['date']!r}: {exc}") from exc

    raw_time = raw.get("time_et")
    if raw_time in (None, ""):
        clock = None
    else:
        try:
            hour, minute = str(raw_time).split(":")
            clock = time(int(hour), int(minute))
        except (ValueError, TypeError) as exc:
            raise CalendarError(f"events[{index}] has invalid time_et {raw_time!r}") from exc

    return Event(
        name=str(raw["name"]),
        day=day,
        time_et=clock,
        market_closed=bool(raw.get("market_closed", False)),
    )


def load_events(path: Path | str | None = None) -> tuple[Event, ...]:
    """Read and validate events.json. Raises CalendarError rather than trading blind."""
    path = Path(path or os.getenv("OG_EVENTS") or DEFAULT_EVENTS_PATH)
    if not path.exists():
        raise CalendarError(f"events file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalendarError(f"events file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise CalendarError(f"events file {path} must hold a JSON array")
    return tuple(_parse_event(entry, i) for i, entry in enumerate(raw))


def next_event(now: datetime, events: Iterable[Event]) -> Event | None:
    """The soonest event at or after `now`. None when the calendar is exhausted."""
    now_utc = _as_utc(now)
    upcoming = [e for e in events if e.starts_at() >= now_utc]
    if not upcoming:
        return None
    return min(upcoming, key=lambda e: e.starts_at())


def hours_to_next_event(now: datetime, events: Iterable[Event]) -> tuple[float, str]:
    """Hours until the next event and its name. (NO_EVENT_HOURS, '') when clear."""
    event = next_event(now, events)
    if event is None:
        return NO_EVENT_HOURS, ""
    return event.hours_until(now), event.name


def events_within(now: datetime, events: Iterable[Event], window_hours: float) -> tuple[Event, ...]:
    """Every event starting inside [now, now + window_hours]."""
    now_utc = _as_utc(now)
    horizon = now_utc + timedelta(hours=window_hours)
    return tuple(
        sorted(
            (e for e in events if now_utc <= e.starts_at() <= horizon),
            key=lambda e: e.starts_at(),
        )
    )


def is_market_holiday(day: date, events: Sequence[Event]) -> bool:
    return any(e.day == day and e.market_closed for e in events)
