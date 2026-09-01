"""Event calendar tests (spec section 19).

The EVENT gate must be demonstrable from injected fixtures, without waiting for
a real release to print.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.marketdna.calendar import (
    ET,
    NO_EVENT_HOURS,
    CalendarError,
    Event,
    events_within,
    hours_to_next_event,
    is_market_holiday,
    load_events,
    next_event,
)

NFP = Event(name="NFP", day=date(2026, 9, 4), time_et=time(8, 30))
JOLTS = Event(name="JOLTS", day=date(2026, 9, 1), time_et=time(10, 0))
HOLIDAY = Event(name="Labor Day", day=date(2026, 9, 7), time_et=None, market_closed=True)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# --- shipped calendar --------------------------------------------------------


def test_shipped_events_file_loads():
    events = load_events("config/events.json")
    assert len(events) == 5
    assert {e.name for e in events} >= {"NFP", "JOLTS", "Labor Day"}


def test_shipped_calendar_marks_labor_day_closed():
    events = load_events("config/events.json")
    assert is_market_holiday(date(2026, 9, 7), events)
    assert not is_market_holiday(date(2026, 9, 4), events)


# --- event timing ------------------------------------------------------------


def test_event_start_converts_et_to_utc():
    """NFP at 08:30 ET in September is 12:30 UTC (EDT, UTC-4)."""
    assert NFP.starts_at() == utc(2026, 9, 4, 12, 30)


def test_all_day_event_anchors_to_the_session_open():
    assert HOLIDAY.starts_at() == datetime(2026, 9, 7, 9, 30, tzinfo=ET).astimezone(timezone.utc)


def test_hours_until_counts_forward():
    assert NFP.hours_until(utc(2026, 9, 4, 10, 30)) == pytest.approx(2.0)


def test_next_event_picks_the_soonest():
    assert next_event(utc(2026, 9, 1), [NFP, JOLTS, HOLIDAY]) == JOLTS


def test_next_event_ignores_the_past():
    assert next_event(utc(2026, 9, 5), [NFP, JOLTS, HOLIDAY]) == HOLIDAY


def test_next_event_is_none_when_calendar_is_exhausted():
    assert next_event(utc(2027, 1, 1), [NFP, JOLTS]) is None


def test_hours_to_next_event_when_calendar_is_clear():
    hours, name = hours_to_next_event(utc(2027, 1, 1), [NFP])
    assert hours == NO_EVENT_HOURS
    assert name == ""


def test_hours_to_next_event_returns_name():
    hours, name = hours_to_next_event(utc(2026, 9, 4, 8, 30), [NFP, HOLIDAY])
    assert name == "NFP"
    assert hours == pytest.approx(4.0)


# --- windows -----------------------------------------------------------------


def test_events_within_window_includes_boundary():
    within = events_within(utc(2026, 9, 3, 12, 30), [NFP], window_hours=24)
    assert within == (NFP,)


def test_events_within_window_excludes_just_outside():
    assert events_within(utc(2026, 9, 3, 12, 29), [NFP], window_hours=24) == ()


def test_events_within_window_are_sorted():
    within = events_within(utc(2026, 9, 1), [HOLIDAY, NFP, JOLTS], window_hours=24 * 10)
    assert [e.name for e in within] == ["JOLTS", "NFP", "Labor Day"]


def test_naive_datetime_is_rejected():
    """A naive timestamp crossing a boundary is a bug, not a default to UTC."""
    with pytest.raises(CalendarError):
        next_event(datetime(2026, 9, 1, 12, 0), [NFP])


# --- file validation ---------------------------------------------------------


def write(tmp_path, payload):
    path = tmp_path / "events.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
    return path


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="not found"):
        load_events(tmp_path / "nope.json")


def test_invalid_json_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="not valid JSON"):
        load_events(write(tmp_path, "{not json"))


def test_non_array_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="JSON array"):
        load_events(write(tmp_path, {"name": "NFP"}))


def test_missing_required_key_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="missing required key"):
        load_events(write(tmp_path, [{"name": "NFP"}]))


def test_bad_date_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="invalid date"):
        load_events(write(tmp_path, [{"name": "NFP", "date": "2026-13-45"}]))


def test_bad_time_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="invalid time_et"):
        load_events(write(tmp_path, [{"name": "NFP", "date": "2026-09-04", "time_et": "8h30"}]))


def test_null_time_is_accepted_as_all_day(tmp_path):
    events = load_events(
        write(tmp_path, [{"name": "Holiday", "date": "2026-09-07", "time_et": None}])
    )
    assert events[0].is_all_day
