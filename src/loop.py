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
            if verdict.action is Action.DEFEND:
                action_taken = "closing_breached_structure"
            elif verdict.closes_position:
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

    # --- fills ---------------------------------------------------------------

    def record_fills(self, result: PassResult) -> int:
        """Journal any newly filled order, with the price it actually got.

        Nothing wrote FILL before this, so the journal recorded what the desk
        asked for (a limit) and never what it received. Realised P&L computed
        from limits is a guess; this reads the broker's own fill prices.

        Deduplicated by client_order_id against what is already journalled.
        """
        try:
            payload = self.mcp.get_open_orders(status="all", limit=100, nested=True)
        except Exception as exc:
            self.journal.record("ERROR", stage="record_fills", error=str(exc))
            return 0

        orders = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            for key in ("result", "orders", "data"):
                if isinstance(payload.get(key), list):
                    orders = payload[key]
                    break

        already = {
            entry.get("client_order_id")
            for entry in self.journal.tail(4000)
            if entry.get("event") == "FILL"
        }

        written = 0
        for order in orders:
            if not isinstance(order, dict) or order.get("status") != "filled":
                continue
            coid = order.get("client_order_id")
            if not coid or coid in already:
                continue

            legs = order.get("legs") or []
            first = legs[0] if legs and isinstance(legs[0], dict) else {}
            intent = str(first.get("position_intent") or "")
            symbol = str(first.get("symbol") or order.get("symbol") or "")

            underlying, expiry = "", ""
            try:
                from .broker.occ import parse_option_symbol

                contract = parse_option_symbol(symbol)
                underlying, expiry = contract.underlying, contract.expiry.isoformat()
            except Exception:
                pass

            price = order.get("filled_avg_price")
            self.journal.record(
                "FILL",
                client_order_id=coid,
                order_id=order.get("id"),
                intent="close" if "close" in intent else "open",
                underlying=underlying,
                expiry=expiry,
                structure="iron_condor" if len(legs) == 4 else "put_credit_spread",
                filled_avg_price=float(price) if price is not None else None,
                filled_qty=int(float(order.get("filled_qty") or 0)),
                filled_at=order.get("filled_at"),
                legs=[str(l.get("symbol")) for l in legs if isinstance(l, dict)],
            )
            already.add(coid)
            written += 1

        if written:
            result.notes.append(f"fills_recorded:{written}")
        return written

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
        self.record_fills(result)
        self.manage_open_positions(open_positions, state, today, result)
        self.consider_new_entry(chain, signals, state, today, result)
        return result
