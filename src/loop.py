"""The trading loop: one full pass through the pipeline (spec section 0).

    market data -> regime -> permission -> chain screen -> candidates
      -> ranking -> Risk Officer -> CLI submission -> reconciliation
      -> lifecycle -> journal

One pass makes at most one new position. Everything it does is journaled, and
every path that could reach the broker goes through the Risk Officer first.

The loop owns no risk logic of its own. It sequences the layers and records what
happened; every decision belongs to the module that made it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from .broker.alpaca_cli import AlpacaCLI, CLIUnavailable
from .broker.alpaca_mcp import AlpacaMCP
from .config import Config
from .journal import Journal
from .marketdna.indicators import Signals
from .marketdna.regime import permissions_for
from .reconcile import ReconciledState, reconcile
from .risk.officer import evaluate
from .rolldesk.candidates import ChainContract, generate_candidates
from .rolldesk.lifecycle import PositionState, decide
from .rolldesk.ranker import FeatherlessRanker
from .safety import ExecutionMode
from .types import Action, Permission, RiskDecision, SystemState, Ticket


@dataclass
class PassResult:
    """What one pass through the loop actually did."""

    regime: str | None = None
    permission: Permission | None = None
    candidates: tuple[Ticket, ...] = ()
    picked: Ticket | None = None
    decision: RiskDecision | None = None
    submitted: bool = False
    lifecycle_actions: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def opened_a_position(self) -> bool:
        return self.submitted

    def as_dict(self) -> dict:
        return {
            "regime": self.regime,
            "candidates": [t.ticket_id for t in self.candidates],
            "picked": self.picked.ticket_id if self.picked else None,
            "decision": self.decision.as_dict() if self.decision else None,
            "submitted": self.submitted,
            "lifecycle": self.lifecycle_actions,
            "notes": self.notes,
        }


class TradingLoop:
    """Sequences one pass. Holds no risk rules; every gate lives in its own module."""

    def __init__(
        self,
        config: Config,
        journal: Journal,
        mcp: AlpacaMCP,
        cli: AlpacaCLI,
        execution_mode: ExecutionMode,
        ranker: FeatherlessRanker | None = None,
    ):
        self.config = config
        self.journal = journal
        self.mcp = mcp
        self.cli = cli
        self.execution_mode = execution_mode
        self.ranker = ranker or FeatherlessRanker()

    # --- lifecycle of already-open positions --------------------------------

    def manage_open_positions(
        self,
        positions: Sequence[PositionState],
        state: ReconciledState,
        today: date,
        result: PassResult,
    ) -> None:
        """Decide and journal an action for every open structure.

        Runs BEFORE any new entry, so a pass that must reduce risk does that
        first and does not open something new on the same tick.
        """
        for position in positions:
            verdict = decide(
                position,
                self.config,
                today,
                flatten_only=state.flatten_only,
                market_open=state.book.market_open,
            )
            result.lifecycle_actions.append((position.ticket.ticket_id, verdict.action.value))

            if verdict.action is Action.HOLD:
                continue

            event = {
                Action.TAKE_PROFIT: "TAKE_PROFIT",
                Action.DEFEND: "DEFEND",
                Action.ROLL: "ROLL",
                Action.FLATTEN: "FLATTEN",
                Action.EXPIRE: "EXPIRE",
            }[verdict.action]
            # What the desk will actually DO, recorded alongside what it decided.
            # DEFEND used to fall through both branches below and take no action
            # at all, while the journal reported a defence that never happened.
            if verdict.closes_position:
                action_taken = "closing_position"
            elif verdict.action is Action.ROLL:
                action_taken = "closing_for_reentry"
            else:
                action_taken = "monitored_only"

            self.journal.record(
                event,
                ticket_id=position.ticket.ticket_id,
                reasons=verdict.reasons,
                dte=position.dte(today),
                captured=round(position.profit_captured, 4),
                action_taken=action_taken,
            )

            if verdict.closes_position:
                self._close(position, state, result)
            elif verdict.action is Action.ROLL:
                # A roll is a brand-new position request. No replacement is built
                # here, so the position is closed and the next pass may open a
                # fresh one through the normal path -- which means the officer
                # evaluates it as the new risk it is.
                #
                # The reason recorded says exactly that. Claiming the replacement
                # was DENIED would put something in the audit record that never
                # happened, and the journal's whole value is that it did not.
                result.notes.append(f"roll_requested:{position.ticket.ticket_id}")
                self._close(
                    position,
                    state,
                    result,
                    ("roll_requested", "closed_for_reentry_through_the_risk_officer"),
                )
            elif verdict.action is Action.DEFEND:
                # No adjustment is made. This desk builds no replacement
                # structures, so the honest options on a breach are to close or
                # to let the existing controls run. The position is defined-risk
                # and the 2x stop and the forced flatten both remain in force, so
                # it is monitored rather than adjusted -- and the record says so.
                result.notes.append(f"defend_monitored:{position.ticket.ticket_id}")

    def _close(
        self,
        position: PositionState,
        state: ReconciledState,
        result: PassResult,
        reasons: Sequence[str] = (),
    ) -> None:
        """Build and send the closing order. Never blocked by FLATTEN_ONLY."""
        try:
            command = self.cli.build_close_command(
                ticket=position.ticket,
                lots=position.lots,
                cost_to_close_mid=position.cost_to_close / max(position.lots, 1) / 100,
                actual_account_id=state.account.account_id,
                system_state=state.system_state.value,
            )
            ok, detail = self.cli.verify_legs_format(command)
            if not ok:
                self.journal.record(
                    "ERROR", stage="close_verify", ticket_id=position.ticket.ticket_id, detail=detail
                )
                result.notes.append(f"close_blocked:{detail[:80]}")
                return
            code, stdout, stderr = self.cli.submit(command, state.account.account_id)
            self.journal.record(
                "SUBMIT",
                intent="close",
                ticket_id=position.ticket.ticket_id,
                client_order_id=command.client_order_id,
                limit_price=command.limit_price,
                lots=command.lots,
                exit_code=code,
                reasons=list(reasons),
                stdout=stdout[:2000],
                stderr=stderr[:2000],
            )
        except Exception as exc:
            self.journal.record(
                "ERROR", stage="close", ticket_id=position.ticket.ticket_id, error=str(exc)
            )
            result.notes.append(f"close_failed:{type(exc).__name__}")

    # --- new entries ---------------------------------------------------------

    def consider_new_entry(
        self,
        chain: Sequence[ChainContract],
        signals: Signals,
        state: ReconciledState,
        today: date,
        result: PassResult,
    ) -> None:
        """Regime -> candidates -> ranking -> Risk Officer -> submission."""
        permission = permissions_for(signals, self.config)
        result.regime = permission.regime
        result.permission = permission
        self.journal.record(
            "REGIME", permission=permission.as_dict(), signals=signals.as_dict()
        )

        if state.flatten_only:
            result.notes.append("flatten_only:no_new_entries")
            return

        candidates = generate_candidates(chain, permission, self.config, today, state.book.now)
        result.candidates = tuple(candidates)
        self.journal.record(
            "CANDIDATES",
            regime=permission.regime,
            count=len(candidates),
            tickets=[t.for_ranking() for t in candidates],
        )
        if not candidates:
            result.notes.append("NO_CANDIDATE")
            return

        ranking = self.ranker.rank(candidates)
        self.journal.record("RANK", **ranking.as_dict())
        picked = ranking.pick
        result.picked = picked

        # The picked ticket is revalidated from scratch. Being chosen by a model
        # earns a candidate nothing.
        decision = evaluate(picked, state.book, state.account, permission, self.config, today=today)
        result.decision = decision
        self.journal.record(
            "ALLOW" if decision.allowed else "DENY",
            ticket_id=picked.ticket_id,
            **decision.as_dict(),
        )
        if not decision.allowed:
            return

        self._submit(picked, decision, state, result)

    def _submit(
        self,
        ticket: Ticket,
        decision: RiskDecision,
        state: ReconciledState,
        result: PassResult,
    ) -> None:
        try:
            command = self.cli.build_submit_command(
                ticket=ticket,
                decision=decision,
                actual_account_id=state.account.account_id,
                system_state=state.system_state.value,
            )
            ok, detail = self.cli.verify_legs_format(command)
            if not ok:
                self.journal.record(
                    "ERROR", stage="submit_verify", ticket_id=ticket.ticket_id, detail=detail
                )
                result.notes.append(f"submit_blocked:{detail[:80]}")
                return

            code, stdout, stderr = self.cli.submit(command, state.account.account_id)
            self.journal.record(
                "SUBMIT",
                intent="open",
                ticket_id=ticket.ticket_id,
                client_order_id=command.client_order_id,
                limit_price=command.limit_price,
                lots=command.lots,
                max_loss=ticket.max_loss * command.lots,
                exit_code=code,
                stdout=stdout[:2000],
                stderr=stderr[:2000],
            )
            result.submitted = code == 0
        except CLIUnavailable as exc:
            self.journal.record("ERROR", stage="submit", ticket_id=ticket.ticket_id, error=str(exc))
            result.notes.append(f"submit_unavailable:{exc}"[:120])
        except Exception as exc:
            self.journal.record("ERROR", stage="submit", ticket_id=ticket.ticket_id, error=str(exc))
            result.notes.append(f"submit_failed:{type(exc).__name__}")

    # --- one full pass -------------------------------------------------------

    def run_once(
        self,
        chain: Sequence[ChainContract],
        signals: Signals,
        open_positions: Sequence[PositionState],
        now: datetime,
        quotes=None,
    ) -> PassResult:
        """Execute one complete pass. Never raises; failures are journaled."""
        result = PassResult()
        try:
            state = reconcile(
                self.mcp,
                self.config,
                self.journal,
                allowed_account_id=self.execution_mode.allowed_account_id,
                now=now,
                quotes=quotes or {c.symbol: c.as_quote() for c in chain},
            )
        except Exception as exc:
            self.journal.record("ERROR", stage="reconcile", error=str(exc))
            result.notes.append(f"reconcile_failed:{type(exc).__name__}")
            return result

        if state.flatten_only:
            self.journal.record(
                "STATE",
                state=SystemState.FLATTEN_ONLY.value,
                drawdown=round(state.account.drawdown, 5),
                limit=self.config.dd_flatten_pct,
            )

        today = now.date()
        self.manage_open_positions(open_positions, state, today, result)
        self.consider_new_entry(chain, signals, state, today, result)
        return result
