"""Regressions for six defects found by auditing the running system.

Each of these passed the existing suite while being wrong in production, which
is the point: they are failures of wiring and accounting, not of logic. Unit
tests exercised every component correctly; the components were not connected.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.journal import Journal
from src.reconcile import rebuild_positions
from src.rolldesk.lifecycle import decide
from src.types import Action

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def leg(symbol, qty, entry, current):
    return {
        "symbol": symbol,
        "qty": str(qty),
        "avg_entry_price": str(entry),
        "current_price": str(current),
    }


def condor(expiry="260918", *, entry=(5.00, 5.21, 3.53, 3.21), current=(4.50, 4.40, 3.00, 2.80)):
    """The real Sept 18 structure: long 750P / short 751P / short 772C / long 773C."""
    return [
        leg(f"SPY{expiry}P00750000", 1, entry[0], current[0]),
        leg(f"SPY{expiry}P00751000", -1, entry[1], current[1]),
        leg(f"SPY{expiry}C00772000", -1, entry[2], current[2]),
        leg(f"SPY{expiry}C00773000", 1, entry[3], current[3]),
    ]


# --- 1. the lifecycle layer receives real positions -------------------------


def test_broker_legs_rebuild_into_a_managed_structure():
    positions = rebuild_positions(condor(), spot=760.0)
    assert len(positions) == 1
    assert positions[0].ticket.structure_type == "iron_condor"
    assert len(positions[0].ticket.legs) == 4


def test_entry_credit_matches_the_broker_fill():
    """Shorts received 5.21 + 3.53, longs paid 5.00 + 3.21: a 0.53 net credit."""
    position = rebuild_positions(condor(), spot=760.0)[0]
    assert position.entry_credit == pytest.approx(53.0, abs=0.01)


def test_cost_to_close_is_derived_from_current_marks():
    position = rebuild_positions(condor(), spot=760.0)[0]
    assert position.cost_to_close == pytest.approx(10.0, abs=0.01)


def test_a_rebuilt_winner_actually_triggers_take_profit(config):
    """The whole point: an 81% winner must be closed, not carried to expiry."""
    position = rebuild_positions(condor(), spot=760.0)[0]
    assert position.profit_captured > config.tp_frac_of_credit
    expiry = TODAY + timedelta(days=17)
    position = rebuild_positions(condor(expiry.strftime("%y%m%d")), spot=760.0)[0]
    assert decide(position, config, TODAY).action is Action.TAKE_PROFIT


def test_a_rebuilt_position_in_the_flatten_zone_is_flattened(config):
    expiry = TODAY + timedelta(days=1)
    position = rebuild_positions(condor(expiry.strftime("%y%m%d")), spot=760.0)[0]
    assert decide(position, config, TODAY).action is Action.FLATTEN


def test_rebuilt_legs_carry_opening_intents():
    """Closing intents would make derive_geometry reject the structure outright."""
    position = rebuild_positions(condor(), spot=760.0)[0]
    assert {leg.position_intent for leg in position.ticket.legs} == {
        "sell_to_open",
        "buy_to_open",
    }


def test_breach_detection_works_on_a_rebuilt_position():
    breached = rebuild_positions(condor(), spot=745.0)[0]
    assert breached.breached_short() is not None


def test_an_unavailable_spot_disables_breach_detection_only(config):
    """A zero spot must not fabricate a breach, and must not block other rules."""
    position = rebuild_positions(condor(), spot=0.0)[0]
    assert position.breached_short() is None
    expiry = TODAY + timedelta(days=1)
    still_flattens = rebuild_positions(condor(expiry.strftime("%y%m%d")), spot=0.0)[0]
    assert decide(still_flattens, config, TODAY).action is Action.FLATTEN


def test_equity_and_partial_legs_are_ignored():
    noise = [{"symbol": "SPY", "qty": "100", "avg_entry_price": "760", "current_price": "761"}]
    assert rebuild_positions(noise) == []
    assert rebuild_positions(condor()[:3]) == []  # a 3-leg remnant is not a structure


def test_empty_book_rebuilds_to_nothing():
    assert rebuild_positions([]) == []


# --- 2. daily risk accounting -----------------------------------------------


def journal_with(tmp_path, entries):
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return Journal(path)


def submit(ts, coid, max_loss, intent="open"):
    return {
        "ts": ts,
        "event": "SUBMIT",
        "intent": intent,
        "client_order_id": coid,
        "max_loss": max_loss,
    }


def test_submitted_risk_counts_against_the_daily_budget(tmp_path):
    """Counting only FILL returned 0.0 forever, so the 2% cap never bound."""
    j = journal_with(tmp_path, [submit("2026-09-01T14:00:00+00:00", "a", 400.0)])
    assert j.risk_opened_on(date(2026, 9, 1)) == 400.0


def test_risk_is_not_double_counted_when_an_order_also_fills(tmp_path):
    j = journal_with(tmp_path, [
        submit("2026-09-01T14:00:00+00:00", "a", 400.0),
        {"ts": "2026-09-01T14:00:05+00:00", "event": "FILL", "intent": "open",
         "client_order_id": "a", "max_loss": 400.0},
    ])
    assert j.risk_opened_on(date(2026, 9, 1)) == 400.0


def test_closing_orders_do_not_add_risk(tmp_path):
    j = journal_with(tmp_path, [
        submit("2026-09-01T14:00:00+00:00", "a", 400.0),
        submit("2026-09-01T15:00:00+00:00", "b", 400.0, intent="close"),
    ])
    assert j.risk_opened_on(date(2026, 9, 1)) == 400.0


def test_risk_is_scoped_to_the_day(tmp_path):
    j = journal_with(tmp_path, [
        submit("2026-08-31T14:00:00+00:00", "old", 400.0),
        submit("2026-09-01T14:00:00+00:00", "new", 250.0),
    ])
    assert j.risk_opened_on(date(2026, 9, 1)) == 250.0


def test_accumulated_risk_can_now_exhaust_the_budget(tmp_path, config):
    """Four 400-dollar positions exceed 2% of 100k; the cap must be reachable."""
    j = journal_with(tmp_path, [
        submit(f"2026-09-01T1{i}:00:00+00:00", f"o{i}", 500.0) for i in range(4)
    ])
    spent = j.risk_opened_on(date(2026, 9, 1))
    assert spent == 2000.0
    assert spent >= config.daily_new_risk_pct * 100_000


# --- 3. the dashboard reports the desk's gate -------------------------------


def test_api_gate_prefers_the_desks_recorded_result(tmp_path, monkeypatch):
    """The API holds no MCP session; re-running the gate there always says HALTED."""
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({
        "ts": "2026-09-01T18:00:00+00:00",
        "event": "STARTUP",
        "state": "READY",
        "passed": True,
        "checks": [{"index": 1, "name": "python_version", "passed": True, "detail": "3.12"}],
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("OG_JOURNAL", str(path))

    from src import api

    payload = api.api_gate()
    assert payload["state"] == "READY"
    assert payload["passed"] is True
    assert payload["source"] == "desk"
    assert payload["measured_at"] == "2026-09-01T18:00:00+00:00"


def test_api_gate_labels_its_own_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OG_JOURNAL", str(tmp_path / "empty.jsonl"))
    from src import api

    payload = api.api_gate()
    assert payload["source"] == "api_process_only"
    assert payload["measured_at"] is None


# --- 4. implied volatility is real -------------------------------------------


def test_implied_vol_is_parsed_from_the_chain():
    from src.marketdata import snapshot_to_contract

    contract = snapshot_to_contract("SPY260918P00750000", {
        "latestQuote": {"bp": 1.20, "ap": 1.25, "t": "2026-09-01T18:00:00Z"},
        "greeks": {"delta": -0.18},
        "impliedVolatility": 0.1734,
    })
    assert contract.implied_volatility == pytest.approx(0.1734)


def test_atm_implied_vol_picks_the_nearest_strike():
    from decimal import Decimal

    from src.marketdata import atm_implied_vol
    from src.rolldesk.candidates import ChainContract

    def c(strike, iv):
        return ChainContract("S", "SPY", date(2026, 9, 18), Decimal(str(strike)), "P",
                             1.0, 1.1, NOW, -0.2, iv)

    assert atm_implied_vol([c(700, 0.30), c(760, 0.17), c(800, 0.28)], 758.0) == 0.17


def test_atm_implied_vol_is_none_when_the_chain_carries_no_iv():
    from decimal import Decimal

    from src.marketdata import atm_implied_vol
    from src.rolldesk.candidates import ChainContract

    bare = ChainContract("S", "SPY", date(2026, 9, 18), Decimal("760"), "P", 1.0, 1.1, NOW, -0.2)
    assert atm_implied_vol([bare], 760.0) is None


def test_signals_use_supplied_iv_over_realized_vol():
    from src.signals_builder import build_signals
    from src.marketdata import Bar

    closes = [400 + (i % 7) * 0.5 for i in range(120)]
    bars = [Bar(ts=NOW, high=c + 1, low=c - 1, close=c) for c in closes]
    signals = build_signals(bars, implied_vol=0.42, now=NOW)
    assert signals.implied_vol == pytest.approx(0.42)
    assert signals.implied_vol != pytest.approx(signals.realized_vol)


# --- 6. the roll path records what actually happened ------------------------


def test_roll_does_not_claim_a_denial_that_never_happened():
    import inspect

    from src import loop

    source = inspect.getsource(loop.TradingLoop.manage_open_positions)
    assert "roll_replacement_denied" not in source
    assert "roll_requested" in source


# --- 8. the journal records what was DONE, not only what was decided --------


def test_defend_is_recorded_as_monitored_not_as_an_action_taken(config, tmp_path):
    """DEFEND fell through every branch, so the journal claimed a defence that
    never happened. The record must state that no adjustment was made."""
    import inspect

    from src import loop

    source = inspect.getsource(loop.TradingLoop.manage_open_positions)
    assert "action_taken" in source
    assert "monitored_only" in source
    assert "Action.DEFEND" in source


def test_every_lifecycle_branch_sets_an_action_taken():
    import inspect

    from src import loop

    source = inspect.getsource(loop.TradingLoop.manage_open_positions)
    for label in ("closing_position", "closing_for_reentry", "monitored_only"):
        assert label in source, label


# --- 9. bounded journal reads -----------------------------------------------


def test_tail_returns_only_the_last_entries(tmp_path):
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(1200):
            fh.write(json.dumps({"ts": "2026-09-01T00:00:00+00:00", "event": "REGIME", "n": i}) + "\n")
    entries = Journal(path).tail(100)
    assert len(entries) == 100
    assert entries[-1]["n"] == 1199


def test_tail_survives_a_torn_final_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps({"ts": "t", "event": "REGIME"}) + "\n{\"event\": \"REG",
        encoding="utf-8",
    )
    assert len(Journal(path).tail()) == 1


def test_tail_on_a_missing_journal_is_empty(tmp_path):
    assert Journal(tmp_path / "nope.jsonl").tail() == []


# --- 10. optional reads never reach a mutating tool -------------------------


def test_try_call_refuses_mutating_tools():
    from src.broker.alpaca_mcp import AlpacaMCP, MCPUnavailable, discover_capabilities

    mcp = AlpacaMCP(lambda name, params: "should not happen", discover_capabilities(["get_clock"]))
    with pytest.raises(MCPUnavailable, match="refusing to call mutating tool"):
        mcp.try_call("place_option_order", symbols="SPY")


def test_try_call_returns_none_for_an_undiscovered_tool():
    from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities

    mcp = AlpacaMCP(lambda name, params: "x", discover_capabilities(["get_clock"]))
    assert mcp.try_call("get_stock_latest_trade", symbols="SPY") is None


def test_try_call_swallows_a_failing_optional_read():
    from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities

    def boom(name, params):
        raise RuntimeError("upstream down")

    mcp = AlpacaMCP(boom, discover_capabilities(["get_clock", "get_stock_latest_trade"]))
    assert mcp.try_call("get_stock_latest_trade", symbols="SPY") is None


# --- 11. a live dashboard must never be cached ------------------------------


def test_every_response_forbids_caching(tmp_path, monkeypatch):
    """A cached dashboard shows a judge stale equity and a frozen refusal count,
    and makes a deployed fix look like it never shipped."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OG_JOURNAL", str(tmp_path / "audit.jsonl"))
    from src import api

    client = TestClient(api.app)
    for path in ("/", "/deck", "/health", "/api/summary"):
        response = client.get(path)
        assert response.status_code == 200, path
        cache = response.headers.get("cache-control", "")
        assert "no-store" in cache, f"{path} is cacheable: {cache!r}"


def test_event_count_is_not_capped_by_the_tail_window(tmp_path):
    """total_events was len(tail(5000)), so it froze at the cap as the journal grew."""
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(6000):
            fh.write(json.dumps({"ts": "t", "event": "REGIME", "n": i}) + "\n")
    j = Journal(path)
    assert j.count() == 6000
    assert len(j.tail(5000)) == 5000


def test_count_of_a_missing_journal_is_zero(tmp_path):
    assert Journal(tmp_path / "nope.jsonl").count() == 0


def test_event_counts_cover_the_whole_journal_not_a_window(tmp_path):
    """"Refused" was counted over tail(5000) while "journal events" was a true
    total, so the same screen showed two numbers that disagreed."""
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(6000):
            fh.write(json.dumps({"ts": "t", "event": "DENY", "n": i}) + "\n")
        for i in range(7):
            fh.write(json.dumps({"ts": "t", "event": "ALLOW", "n": i}) + "\n")
    counts = Journal(path).event_counts()
    assert counts["DENY"] == 6000
    assert counts["ALLOW"] == 7


def test_event_counts_are_cached_until_the_file_changes(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({"ts": "t", "event": "DENY"}) + "\n", encoding="utf-8")
    j = Journal(path)
    assert j.event_counts()["DENY"] == 1
    assert j.event_counts()["DENY"] == 1  # served from cache

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "t", "event": "DENY"}) + "\n")
    import os
    os.utime(path, ns=(0, 0))  # force a different mtime signature
    assert j.event_counts()["DENY"] == 2


def test_event_counts_of_a_missing_journal_are_empty(tmp_path):
    assert Journal(tmp_path / "nope.jsonl").event_counts() == {}


# --- 12. order filters reach the server -------------------------------------


def test_get_open_orders_passes_filters_through():
    """record_fills needs status="all"; the adapter previously took no arguments."""
    from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities

    seen = {}

    def call(name, params):
        seen["name"] = name
        seen["params"] = params
        return []

    mcp = AlpacaMCP(call, discover_capabilities(["get_orders"]))
    mcp.get_open_orders(status="all", limit=100, nested=True)
    assert seen["name"] == "get_orders"
    assert seen["params"] == {"status": "all", "limit": 100, "nested": True}


def test_get_open_orders_still_works_with_no_filters():
    from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities

    seen = {}
    mcp = AlpacaMCP(lambda n, p: seen.update(params=p) or [], discover_capabilities(["get_orders"]))
    mcp.get_open_orders()
    assert seen["params"] == {}


# --- 13. multi-leg fill prices are signed by cash flow ----------------------


def test_realised_pnl_uses_the_cash_flow_sign(tmp_path, monkeypatch):
    """Alpaca opens a credit structure at a NEGATIVE fill price. Read as plain
    magnitudes, an 8-dollar loss was reported as a 114-dollar one."""
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in [
            {"ts": "2026-09-01T18:59:00+00:00", "event": "FILL", "intent": "open",
             "underlying": "SPY", "expiry": "2026-09-08", "structure": "iron_condor",
             "filled_avg_price": -0.53, "filled_qty": 1, "legs": []},
            {"ts": "2026-09-01T19:14:00+00:00", "event": "FILL", "intent": "close",
             "underlying": "SPY", "expiry": "2026-09-08", "structure": "iron_condor",
             "filled_avg_price": 0.61, "filled_qty": 1, "legs": []},
        ]:
            fh.write(json.dumps(entry) + "\n")
    monkeypatch.setenv("OG_JOURNAL", str(path))

    from src import api

    payload = api.api_trades()
    trade = payload["trades"][0]
    assert trade["entry_credit"] == pytest.approx(0.53)
    assert trade["exit_cost"] == pytest.approx(0.61)
    assert trade["realized"] == pytest.approx(-8.0)
    assert payload["realized_total"] == pytest.approx(-8.0)


def test_a_winning_round_trip_reads_positive(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in [
            {"ts": "2026-09-01T18:00:00+00:00", "event": "FILL", "intent": "open",
             "underlying": "SPY", "expiry": "2026-09-11", "structure": "iron_condor",
             "filled_avg_price": -1.00, "filled_qty": 1, "legs": []},
            {"ts": "2026-09-02T18:00:00+00:00", "event": "FILL", "intent": "close",
             "underlying": "SPY", "expiry": "2026-09-11", "structure": "iron_condor",
             "filled_avg_price": 0.40, "filled_qty": 1, "legs": []},
        ]:
            fh.write(json.dumps(entry) + "\n")
    monkeypatch.setenv("OG_JOURNAL", str(path))

    from src import api

    payload = api.api_trades()
    assert payload["trades"][0]["realized"] == pytest.approx(60.0)
    assert payload["win_rate"] == 1.0


def test_an_open_structure_has_no_realised_pnl(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(
        {"ts": "2026-09-02T18:00:00+00:00", "event": "FILL", "intent": "open",
         "underlying": "SPY", "expiry": "2026-09-18", "structure": "iron_condor",
         "filled_avg_price": -0.53, "filled_qty": 1, "legs": []}) + "\n", encoding="utf-8")
    monkeypatch.setenv("OG_JOURNAL", str(path))

    from src import api

    payload = api.api_trades()
    assert payload["trades"][0]["open"] is True
    assert payload["trades"][0]["realized"] is None
    assert payload["realized_total"] == 0.0


# --- 14. legs resolve to the actual buy and sell orders ---------------------


def test_condor_legs_resolve_to_the_right_sides():
    """Buy the lowest put, sell the next; sell the lower call, buy the highest."""
    from src.api import describe_legs

    legs = describe_legs([
        "SPY260910P00759000", "SPY260910P00760000",
        "SPY260910C00771000", "SPY260910C00772000",
    ], "iron_condor")
    by_symbol = {l["symbol"]: l for l in legs}
    assert by_symbol["SPY260910P00759000"]["side"] == "buy"
    assert by_symbol["SPY260910P00760000"]["side"] == "sell"
    assert by_symbol["SPY260910C00771000"]["side"] == "sell"
    assert by_symbol["SPY260910C00772000"]["side"] == "buy"


def test_shorts_are_listed_before_the_protection():
    from src.api import describe_legs

    legs = describe_legs([
        "SPY260910P00759000", "SPY260910P00760000",
        "SPY260910C00771000", "SPY260910C00772000",
    ], "iron_condor")
    assert [l["side"] for l in legs] == ["sell", "sell", "buy", "buy"]


def test_put_credit_spread_sells_the_higher_strike():
    from src.api import describe_legs

    legs = describe_legs(["SPY260910P00755000", "SPY260910P00760000"], "put_credit_spread")
    by_symbol = {l["symbol"]: l for l in legs}
    assert by_symbol["SPY260910P00760000"]["side"] == "sell"
    assert by_symbol["SPY260910P00755000"]["side"] == "buy"


def test_strike_and_right_are_carried_through():
    from src.api import describe_legs

    leg = describe_legs(["SPY260910C00771000"], "iron_condor")[0]
    assert leg["strike"] == 771.0
    assert leg["right"] == "C"


def test_unparseable_symbols_are_dropped_not_guessed():
    from src.api import describe_legs

    assert describe_legs(["NOT-AN-OCC-SYMBOL"], "iron_condor") == []
    assert describe_legs([], "iron_condor") == []


# --- 15. the three P&L figures must reconcile -------------------------------


def _pnl_journal(tmp_path, monkeypatch):
    """One closed trade at -8, one still open, on a 100k account down 95.62."""
    path = tmp_path / "audit.jsonl"
    rows = [
        {"ts": "2026-09-01T13:00:00+00:00", "event": "RECONCILE", "equity": 100000.0,
         "start_of_day_equity": 100000.0},
        {"ts": "2026-09-01T18:59:00+00:00", "event": "FILL", "intent": "open",
         "underlying": "SPY", "expiry": "2026-09-08", "structure": "iron_condor",
         "filled_avg_price": -0.53, "filled_qty": 1, "legs": []},
        {"ts": "2026-09-01T19:14:00+00:00", "event": "FILL", "intent": "close",
         "underlying": "SPY", "expiry": "2026-09-08", "structure": "iron_condor",
         "filled_avg_price": 0.61, "filled_qty": 1, "legs": []},
        {"ts": "2026-09-03T18:00:00+00:00", "event": "FILL", "intent": "open",
         "underlying": "SPY", "expiry": "2026-09-15", "structure": "iron_condor",
         "filled_avg_price": -2.40, "filled_qty": 1, "legs": []},
        {"ts": "2026-09-03T20:00:00+00:00", "event": "RECONCILE", "equity": 99904.38,
         "start_of_day_equity": 100000.0, "open_structures": 1},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setenv("OG_JOURNAL", str(path))


def test_realised_plus_unrealised_equals_the_total(tmp_path, monkeypatch):
    """The page showed -95 beside -56 with nothing joining them."""
    _pnl_journal(tmp_path, monkeypatch)
    from src import api

    s = api.api_summary()
    assert s["pnl_total"] == pytest.approx(-95.62)
    assert s["pnl_realized"] == pytest.approx(-8.0)
    assert s["pnl_unrealized"] == pytest.approx(-87.62)
    assert s["pnl_realized"] + s["pnl_unrealized"] == pytest.approx(s["pnl_total"], abs=0.01)


def test_only_closed_trades_count_as_realised(tmp_path, monkeypatch):
    _pnl_journal(tmp_path, monkeypatch)
    from src import api

    assert api.api_summary()["closed_trades"] == 1


def test_summary_and_trades_agree_on_realised(tmp_path, monkeypatch):
    """Both read the same pairing routine, so they cannot drift apart."""
    _pnl_journal(tmp_path, monkeypatch)
    from src import api

    assert api.api_summary()["pnl_realized"] == pytest.approx(api.api_trades()["realized_total"])
