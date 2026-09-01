"""Append-only journal tests.

The journal is the authoritative audit record, so the properties that matter are
append-only-ness, durability of the encoding, and refusal of unknown events.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.journal import EVENTS, Journal, JournalError
from src.types import Leg, Permission, RiskDecision


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "audit.jsonl")


def test_record_round_trips(journal):
    journal.record("REGIME", regime="INCOME", adx=10.5)
    entries = list(journal.read())
    assert len(entries) == 1
    assert entries[0]["event"] == "REGIME"
    assert entries[0]["regime"] == "INCOME"
    assert entries[0]["adx"] == 10.5


def test_every_entry_is_timestamped(journal):
    entry = journal.record("SCAN", underlying="SPY")
    assert datetime.fromisoformat(entry["ts"]).tzinfo is not None


def test_writes_append_never_overwrite(journal):
    for i in range(5):
        journal.record("SCAN", seq=i)
    assert [e["seq"] for e in journal.read()] == [0, 1, 2, 3, 4]


def test_a_new_journal_instance_appends_to_the_same_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    Journal(path).record("SCAN", seq=0)
    Journal(path).record("SCAN", seq=1)
    assert [e["seq"] for e in Journal(path).read()] == [0, 1]


def test_unknown_event_is_rejected(journal):
    with pytest.raises(JournalError, match="unknown journal event"):
        journal.record("YOLO", size=100)


def test_rejected_event_writes_nothing(journal):
    with pytest.raises(JournalError):
        journal.record("YOLO")
    assert list(journal.read()) == []


def test_reading_a_missing_file_yields_nothing(tmp_path):
    assert list(Journal(tmp_path / "absent.jsonl").read()) == []


def test_corrupt_line_is_reported_with_its_position(tmp_path):
    path = tmp_path / "audit.jsonl"
    Journal(path).record("SCAN", seq=0)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    with pytest.raises(JournalError, match=":2:"):
        list(Journal(path).read())


# --- encoding ----------------------------------------------------------------


def test_dataclasses_are_encoded_structurally(journal):
    decision = RiskDecision(decision="DENY", reasons=("market_closed",), allowed_lots=0)
    journal.record("DENY", verdict=decision)
    entry = next(iter(journal.read()))
    assert entry["verdict"]["decision"] == "DENY"
    assert entry["verdict"]["reasons"] == ["market_closed"]


def test_nested_dataclasses_are_encoded(journal):
    journal.record("SUBMIT", leg=Leg("SPY260904P00640000", "sell", "sell_to_open", 1))
    entry = next(iter(journal.read()))
    assert entry["leg"]["symbol"] == "SPY260904P00640000"


def test_decimals_keep_full_precision(journal):
    """A strike must never be rounded on its way into the audit record."""
    journal.record("SCAN", strike=Decimal("608.125"))
    assert next(iter(journal.read()))["strike"] == "608.125"


def test_dates_and_datetimes_are_iso_encoded(journal):
    journal.record("EXPIRE", day=date(2026, 9, 4), at=datetime(2026, 9, 4, tzinfo=timezone.utc))
    entry = next(iter(journal.read()))
    assert entry["day"] == "2026-09-04"
    assert entry["at"].startswith("2026-09-04")


def test_nan_is_encoded_as_null_not_invalid_json(journal):
    journal.record("SCAN", value=float("nan"))
    raw = journal.path.read_text(encoding="utf-8").strip()
    assert json.loads(raw)["value"] is None


def test_permission_object_is_encoded(journal):
    journal.record(
        "REGIME",
        permission=Permission("INCOME", ("put_credit_spread",), 1, ("adx_below_threshold",)),
    )
    entry = next(iter(journal.read()))
    assert entry["permission"]["allowed_strategies"] == ["put_credit_spread"]


# --- derived reads -----------------------------------------------------------


def test_daily_risk_sums_only_todays_fills(journal, monkeypatch):
    today = datetime.now(timezone.utc).date()
    journal.record("FILL", max_loss=400.0)
    journal.record("FILL", max_loss=300.0)
    journal.record("DENY", max_loss=9_999.0)  # not a fill
    assert journal.risk_opened_on(today) == pytest.approx(700.0)


def test_daily_risk_excludes_other_days(journal):
    journal.record("FILL", max_loss=400.0)
    assert journal.risk_opened_on(date(2020, 1, 1)) == 0.0


def test_high_water_mark_tracks_the_peak(journal):
    for equity in (100_000.0, 101_500.0, 99_000.0):
        journal.record("RECONCILE", equity=equity)
    assert journal.high_water_mark() == pytest.approx(101_500.0)


def test_high_water_mark_respects_a_persisted_floor(journal):
    journal.record("RECONCILE", equity=99_000.0)
    assert journal.high_water_mark(floor=105_000.0) == pytest.approx(105_000.0)


def test_high_water_mark_of_an_empty_journal_is_the_floor(journal):
    assert journal.high_water_mark(floor=100_000.0) == 100_000.0


def test_is_writable(journal):
    assert journal.is_writable()


def test_lifecycle_events_are_all_recordable(journal):
    for event in ("TAKE_PROFIT", "DEFEND", "ROLL", "FLATTEN", "EXPIRE"):
        journal.record(event, structure_id="s-1")
    assert len(list(journal.read())) == 5


def test_event_vocabulary_covers_the_documented_lifecycle():
    assert {"DENY", "ALLOW", "FILL", "TAKE_PROFIT", "DEFEND", "ROLL", "FLATTEN", "EXPIRE"} <= EVENTS
