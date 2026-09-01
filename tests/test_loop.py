"""End-to-end loop tests.

These wire the real modules together with a fake broker. The question they
answer is whether the ORDERING holds: risk reduction before new entry, officer
before broker, and nothing reaching the broker that was not allowed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.broker.alpaca_cli import AlpacaCLI, load_cli_reference
from src.broker.alpaca_mcp import AlpacaMCP, discover_capabilities
from src.journal import Journal
from src.loop import TradingLoop
from src.marketdna.indicators import Signals
from src.rolldesk.lifecycle import PositionState
from src.rolldesk.ranker import FeatherlessRanker
from src.safety import ExecutionMode

from .conftest import EXPIRY, NOW, TODAY, make_ticket, pcs_legs
from .test_broker_gates import FULL_TOOLSET
from .test_candidates import EXPIRY as CHAIN_EXPIRY, synthetic_chain

DEV_ENV = {"MODE": "development", "DEV_ACCOUNT_ID": "DEV-1", "JUDGE_ACCOUNT_ID": "JUDGE-9"}
PAPER = "https://paper-api.alpaca.markets"

ACCOUNT = {
    "id": "DEV-1",
    "equity": "100000",
    "cash": "100000",
    "buying_power": "100000",
    "last_equity": "100000",
    "options_trading_level": 3,
}
CLOCK = {"is_open": True, "minutes_to_close": 180}


def fake_mcp(account=None, positions=None, clock=None):
    payloads = {
        "get_account": account if account is not None else dict(ACCOUNT),
        "get_positions": positions if positions is not None else [],
        "get_clock": clock if clock is not None else dict(CLOCK),
    }
    capabilities = discover_capabilities(FULL_TOOLSET)
    reverse = {v: k for k, v in capabilities.mapping.items()}

    def call_tool(name, params):
        return payloads.get(reverse.get(name, ""), None)

    return AlpacaMCP(call_tool, capabilities)


class RecordingCLI(AlpacaCLI):
    """A CLI whose runner records commands instead of running them."""

    def __init__(self, **kwargs):
        self.sent = []
        self.verified = []
        super().__init__(
            execution_mode=ExecutionMode.from_env(DEV_ENV),
            trading_host=PAPER,
            reference=load_cli_reference("docs/cli-reference.txt"),
            runner=self._record,
            **kwargs,
        )

    def _record(self, argv):
        if "--dry-run" in argv:
            self.verified.append(argv)
        else:
            self.sent.append(argv)
        return 0, '{"code":0,"error":"","status":200}', ""


def loop(config, journal, mcp=None, cli=None, ranker=None):
    return TradingLoop(
        config=config,
        journal=journal,
        mcp=mcp or fake_mcp(),
        cli=cli or RecordingCLI(),
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        ranker=ranker or FeatherlessRanker(api_key="", model=""),
    )


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "audit.jsonl")


def signals(adx=10.0, iv_rank=50.0, hours_to_event=1e9):
    return Signals(400, 398, adx, 0.12, 0.13, 0.15, iv_rank, hours_to_event, "")


@pytest.fixture
def chain():
    return synthetic_chain(CHAIN_EXPIRY)


def events(journal, name):
    return [e for e in journal.read() if e["event"] == name]


# --- the happy path ----------------------------------------------------------


def test_a_clean_pass_opens_one_position(config, journal, chain):
    desk = RecordingCLI()
    result = loop(config, journal, cli=desk).run_once(chain, signals(), [], NOW)
    assert result.regime == "INCOME"
    assert result.candidates
    assert result.decision is not None and result.decision.allowed, result.decision
    assert result.submitted
    assert len(desk.sent) == 1


def test_the_pass_is_fully_journaled(config, journal, chain):
    loop(config, journal).run_once(chain, signals(), [], NOW)
    recorded = {e["event"] for e in journal.read()}
    assert {"RECONCILE", "REGIME", "CANDIDATES", "RANK", "ALLOW", "SUBMIT"} <= recorded


def test_only_one_position_is_opened_per_pass(config, journal, chain):
    desk = RecordingCLI()
    loop(config, journal, cli=desk).run_once(chain, signals(), [], NOW)
    opens = [a for a in desk.sent if "buy_to_open" in a[a.index("--legs") + 1]]
    assert len(opens) == 1


def test_every_order_is_dry_run_verified_before_being_sent(config, journal, chain):
    """Spec section 4: the --legs format is proven before anything is sent."""
    desk = RecordingCLI()
    loop(config, journal, cli=desk).run_once(chain, signals(), [], NOW)
    assert desk.verified
    assert len(desk.verified) >= len(desk.sent)


# --- blocked regimes ---------------------------------------------------------


def test_momentum_opens_nothing(config, journal, chain):
    desk = RecordingCLI()
    result = loop(config, journal, cli=desk).run_once(chain, signals(adx=40.0), [], NOW)
    assert result.regime == "MOMENTUM"
    assert result.candidates == ()
    assert not result.submitted
    assert desk.sent == []
    assert "NO_CANDIDATE" in result.notes


def test_event_opens_nothing(config, journal, chain):
    desk = RecordingCLI()
    result = loop(config, journal, cli=desk).run_once(
        chain, signals(hours_to_event=1.0), [], NOW
    )
    assert result.regime == "EVENT"
    assert desk.sent == []


def test_blocked_regime_still_journals_the_reason(config, journal, chain):
    loop(config, journal).run_once(chain, signals(adx=40.0), [], NOW)
    regime_events = events(journal, "REGIME")
    assert regime_events
    assert regime_events[0]["permission"]["max_lots"] == 0


# --- drawdown / flatten-only -------------------------------------------------


def drawn_down_account(pct: float):
    equity = 100_000 * (1 - pct)
    return {**ACCOUNT, "equity": str(equity), "last_equity": "100000"}


def test_drawdown_breach_blocks_new_entries(config, journal, chain):
    desk = RecordingCLI()
    mcp = fake_mcp(account=drawn_down_account(0.06))
    result = loop(config, journal, mcp=mcp, cli=desk).run_once(chain, signals(), [], NOW)
    assert not result.submitted
    assert "flatten_only:no_new_entries" in result.notes


def test_drawdown_breach_is_journaled_as_a_state_change(config, journal, chain):
    mcp = fake_mcp(account=drawn_down_account(0.06))
    loop(config, journal, mcp=mcp).run_once(chain, signals(), [], NOW)
    states = events(journal, "STATE")
    assert states and states[0]["state"] == "FLATTEN_ONLY"


def test_shallow_drawdown_still_trades(config, journal, chain):
    mcp = fake_mcp(account=drawn_down_account(0.02))
    result = loop(config, journal, mcp=mcp).run_once(chain, signals(), [], NOW)
    assert result.submitted


# --- lifecycle ordering ------------------------------------------------------


def position(expiry=EXPIRY, entry_credit=100.0, cost_to_close=60.0, spot=645.0):
    return PositionState(
        ticket=make_ticket(pcs_legs(expiry=expiry), expiry=expiry),
        lots=1,
        entry_credit=entry_credit,
        cost_to_close=cost_to_close,
        underlying_price=spot,
        opened_at=NOW,
    )


def test_a_winner_is_closed(config, journal, chain):
    desk = RecordingCLI()
    winner = position(cost_to_close=20.0)
    result = loop(config, journal, cli=desk).run_once(chain, signals(), [winner], NOW)
    assert ("t-1", "TAKE_PROFIT") in result.lifecycle_actions
    assert events(journal, "TAKE_PROFIT")


def test_a_position_in_the_flatten_zone_is_closed(config, journal, chain):
    desk = RecordingCLI()
    expiring = position(expiry=TODAY + timedelta(days=1))
    result = loop(config, journal, cli=desk).run_once(chain, signals(), [expiring], NOW)
    assert ("t-1", "FLATTEN") in result.lifecycle_actions


def test_flatten_only_closes_positions_but_opens_none(config, journal, chain):
    desk = RecordingCLI()
    mcp = fake_mcp(account=drawn_down_account(0.06))
    result = loop(config, journal, mcp=mcp, cli=desk).run_once(
        chain, signals(), [position()], NOW
    )
    assert ("t-1", "FLATTEN") in result.lifecycle_actions
    assert not result.submitted
    assert desk.sent  # the close still went out


def test_risk_is_reduced_before_new_risk_is_added(config, journal, chain):
    """A pass that must close something does that before it opens anything."""
    desk = RecordingCLI()
    loop(config, journal, cli=desk).run_once(chain, signals(), [position(cost_to_close=10.0)], NOW)
    legs = [a[a.index("--legs") + 1] for a in desk.sent]
    assert "buy_to_close" in legs[0]  # the close is first


def test_healthy_position_is_left_alone(config, journal, chain):
    result = loop(config, journal).run_once(
        chain, signals(), [position(expiry=TODAY + timedelta(days=5))], NOW
    )
    assert ("t-1", "HOLD") in result.lifecycle_actions


# --- failure handling --------------------------------------------------------


def test_a_broker_read_failure_does_not_crash_the_loop(config, journal, chain):
    broken = AlpacaMCP(lambda name, params: (_ for _ in ()).throw(RuntimeError("mcp down")),
                       discover_capabilities(FULL_TOOLSET))
    result = loop(config, journal, mcp=broken).run_once(chain, signals(), [], NOW)
    assert not result.submitted
    assert any("reconcile_failed" in n for n in result.notes)
    assert events(journal, "ERROR")


def test_an_unverifiable_legs_format_blocks_submission(config, journal, chain):
    desk = RecordingCLI()
    desk._runner = lambda argv: (1, "", "unknown flag")
    result = loop(config, journal, cli=desk).run_once(chain, signals(), [], NOW)
    assert not result.submitted
    assert any("submit_blocked" in n for n in result.notes)


def test_wrong_account_never_reaches_the_broker(config, journal, chain):
    desk = RecordingCLI()
    mcp = fake_mcp(account={**ACCOUNT, "id": "SOMEONE-ELSE"})
    result = loop(config, journal, mcp=mcp, cli=desk).run_once(chain, signals(), [], NOW)
    assert desk.sent == []
    assert not result.submitted


def test_closed_market_opens_nothing(config, journal, chain):
    desk = RecordingCLI()
    mcp = fake_mcp(clock={"is_open": False, "minutes_to_close": 0})
    result = loop(config, journal, mcp=mcp, cli=desk).run_once(chain, signals(), [], NOW)
    assert not result.submitted
    assert result.decision is not None and not result.decision.allowed


def test_the_model_cannot_cause_an_unallowed_submission(config, journal, chain):
    """Even a model picking a candidate does not bypass the officer."""

    class AlwaysPicksLast(FeatherlessRanker):
        def rank(self, candidates):
            from src.rolldesk.ranker import RankResult

            return RankResult(candidates[-1], "chosen", False, "ok", "fake", 1.0)

    desk = RecordingCLI()
    mcp = fake_mcp(clock={"is_open": False, "minutes_to_close": 0})
    result = loop(config, journal, mcp=mcp, cli=desk, ranker=AlwaysPicksLast()).run_once(
        chain, signals(), [], NOW
    )
    assert desk.sent == []
    assert not result.submitted
