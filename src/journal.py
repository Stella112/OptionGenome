"""Append-only JSONL journal -- the authoritative audit record.

The database is a convenience; this file is the truth. Every entry is flushed
and fsynced before the write returns, so a crash cannot lose a decision that
downstream code already acted on. Nothing in here ever rewrites or deletes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

DEFAULT_JOURNAL_PATH = Path("journal/optiongenome.jsonl")

#: Every event the desk may record. An unknown event is a bug, not a new type.
EVENTS: frozenset[str] = frozenset(
    {
        "STARTUP",
        "HALT",
        "STATE",
        "REGIME",
        "SCAN",
        "CANDIDATES",
        "RANK",
        "ALLOW",
        "DENY",
        "SUBMIT",
        "FILL",
        "REJECT",
        "RECONCILE",
        "TAKE_PROFIT",
        "DEFEND",
        "ROLL",
        "FLATTEN",
        "EXPIRE",
        "ERROR",
    }
)


#: Pulls the event name without decoding the whole line. Tolerates whitespace,
#: because a scan keyed to this writer's exact spacing counts nothing at all in
#: a journal written any other way.
_EVENT_RE = re.compile(r'"event"\s*:\s*"([^"]+)"')


class JournalError(ValueError):
    """Raised for an unknown event name or an unwritable journal."""


def _encode(obj: Any) -> Any:
    """Make any desk object JSON-safe without losing precision on strikes."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _encode(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_encode(v) for v in obj]
    if isinstance(obj, float) and obj != obj:  # NaN is not valid JSON
        return None
    return obj


class Journal:
    """Append-only writer. One instance per process; safe to call from the loop."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.getenv("OG_JOURNAL") or DEFAULT_JOURNAL_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> dict:
        """Append one entry. Returns the entry as written."""
        if event not in EVENTS:
            raise JournalError(f"unknown journal event: {event!r}")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{k: _encode(v) for k, v in fields.items()},
        }
        line = json.dumps(entry, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def read(self) -> Iterator[dict]:
        """Replay the journal in write order."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JournalError(f"corrupt journal at {self.path}:{lineno}: {exc}") from exc

    def tail(self, limit: int = 500) -> list[dict]:
        """The last `limit` entries, without holding the whole file in memory.

        The dashboard polls every 15 seconds and previously replayed the entire
        journal on each request. That is fine at 700 entries and not fine after
        a week of one-minute passes.
        """
        from collections import deque

        window: deque[str] = deque(maxlen=limit)
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    window.append(line)
        out = []
        for line in window:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line must not break the dashboard
        return out

    def count(self) -> int:
        """Total entries, without decoding any of them.

        api_summary previously reported len(tail(...)), which silently became
        the tail cap once the journal outgrew it -- the deck displayed a flat
        5,000 events forever.
        """
        if not self.path.exists():
            return 0
        total = 0
        with open(self.path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                total += chunk.count(b"\n")
        return total

    def event_counts(self) -> dict[str, int]:
        """Count every entry by event type, across the whole journal.

        api_summary counted over tail(5000), so the headline "refused" figure
        silently became a rolling-window count while "journal events" stayed a
        true total -- two numbers on the same screen disagreeing.

        The result is cached against the file's size and mtime, so a dashboard
        polling every 15 seconds rescans only when something was actually
        written.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return {}
        signature = (stat.st_size, stat.st_mtime_ns)
        if getattr(self, "_counts_signature", None) == signature:
            return dict(self._counts_cache)

        counts: dict[str, int] = {}
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                match = _EVENT_RE.search(line)
                if match:
                    name = match.group(1)
                    counts[name] = counts.get(name, 0) + 1

        self._counts_signature = signature
        self._counts_cache = counts
        return dict(counts)

    def events(self, event: str) -> list[dict]:
        """Every entry for one event type, oldest first, across the whole file.

        Trades are derived from FILL entries, and a windowed read produced a
        different answer per endpoint depending on the window: the summary
        disagreed with the trades table it was meant to summarise. Fills are
        rare enough to scan in full, and correctness here outranks the read.
        """
        return [entry for entry in self.read() if entry.get("event") == event]

    def client_order_ids(self, event: str) -> set[str]:
        """Every client_order_id recorded under `event`, across the whole file.

        Deduplication cannot use tail(). The journal outgrows any fixed window,
        and once an old FILL slides past the end of it the same broker fill is
        journalled again on the next restart. That silently multiplied the
        historical fills and made realised P&L depend on how many entries the
        reading endpoint happened to ask for.
        """
        seen: set[str] = set()
        for entry in self.read():
            if entry.get("event") == event:
                coid = entry.get("client_order_id")
                if coid:
                    seen.add(str(coid))
        return seen

    def first_equity(self) -> float | None:
        """Equity at the desk's first reconcile: the baseline for total P&L.

        Read from the head of the file, not the tail window, so the baseline
        does not drift away as the journal grows. Cached like event_counts.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return None
        if getattr(self, "_first_equity_signature", None) == stat.st_size:
            return self._first_equity_cache

        value = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"RECONCILE"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("equity") is not None:
                    value = float(entry["equity"])
                    break

        self._first_equity_signature = stat.st_size
        self._first_equity_cache = value
        return value

    def is_writable(self) -> bool:
        """Startup gate item 17."""
        try:
            probe = self.path.parent / ".journal_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    def risk_opened_on(self, day: date) -> float:
        """Max loss committed to opening trades on `day`. Feeds the daily budget.

        Counts SUBMIT as well as FILL: risk is committed the moment a working
        order exists, not when it fills. Counting fills alone returned 0.0 on
        every call -- nothing writes a FILL -- so the 2%-per-day cap could never
        bind and the desk could open positions without limit.

        Entries are deduplicated by client_order_id so an order that is both
        submitted and filled is not charged to the budget twice.
        """
        prefix = day.isoformat()
        total = 0.0
        counted: set[str] = set()
        for entry in self.read():
            if entry.get("event") not in ("SUBMIT", "FILL"):
                continue
            if entry.get("intent") not in (None, "open"):
                continue  # closing an existing structure removes risk, never adds it
            if not str(entry.get("ts", "")).startswith(prefix):
                continue
            key = str(entry.get("client_order_id") or entry.get("ticket_id") or id(entry))
            if key in counted:
                continue
            counted.add(key)
            total += float(entry.get("max_loss") or 0.0)
        return total

    def high_water_mark(self, floor: float = 0.0) -> float:
        """Highest reconciled equity ever journaled (spec section 30)."""
        best = floor
        for entry in self.read():
            if entry.get("event") != "RECONCILE":
                continue
            equity = entry.get("equity")
            if equity is not None:
                best = max(best, float(equity))
        return best
