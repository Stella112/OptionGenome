"""Position lifecycle management (spec section 14 and the Roll Desk mandate).

Opening a trade is not the end of the process. Every open structure is
re-evaluated on each pass and assigned exactly one action.

Pure and deterministic, like the Risk Officer: no network, no clock read, no
broker. The caller supplies the reconciled state; this module only decides.

Evaluation order is mandatory and the first match wins. Safety-driven exits
outrank profit-taking, so a position that is both profitable and inside the
forced-flatten zone is flattened, not held for the last few cents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..config import Config
from ..types import Action, Ticket


@dataclass(frozen=True)
class PositionState:
    """An open structure as reconciled from the broker.

    `entry_credit` and `cost_to_close` are dollars for the whole position, not
    per share and not per lot, so the arithmetic here never needs a multiplier.
    """

    ticket: Ticket
    lots: int
    entry_credit: float
    cost_to_close: float
    underlying_price: float
    opened_at: datetime
    roll_count: int = 0

    @property
    def pnl(self) -> float:
        return self.entry_credit - self.cost_to_close

    @property
    def profit_captured(self) -> float:
        """Fraction of the entry credit realised. 1.0 means the short expired worthless."""
        if self.entry_credit <= 0:
            return 0.0
        return self.pnl / self.entry_credit

    @property
    def loss_multiple(self) -> float:
        """Cost to close as a multiple of the credit received."""
        if self.entry_credit <= 0:
            return float("inf")
        return self.cost_to_close / self.entry_credit

    def dte(self, today: date) -> int:
        return (self.ticket.expiry_date - today).days

    @property
    def short_strikes(self) -> tuple[Decimal, ...]:
        from .structures import derive_geometry

        return derive_geometry(self.ticket.legs).short_strikes

    def breached_short(self) -> Decimal | None:
        """The short strike the underlying has traded through, if any.

        A put short is breached when spot falls below it; a call short when spot
        rises above it. Which side a strike belongs to is read from the legs.

        An unknown spot returns None rather than a breach. A missing price is not
        a price of zero: treating it as zero puts it below every put strike, so a
        failed quote lookup would report every position as breached and trigger a
        defend or close on all of them at once.
        """
        from .structures import parse_legs

        if self.underlying_price is None or self.underlying_price <= 0:
            return None

        spot = Decimal(str(self.underlying_price))
        for parsed in parse_legs(self.ticket.legs):
            if not parsed.is_short:
                continue
            if parsed.right == "P" and spot <= parsed.strike:
                return parsed.strike
            if parsed.right == "C" and spot >= parsed.strike:
                return parsed.strike
        return None


@dataclass(frozen=True)
class LifecycleDecision:
    action: Action
    reasons: tuple[str, ...]

    @property
    def closes_position(self) -> bool:
        """DEFEND closes too.

        This desk builds no replacement structures, so on a breached short the
        only two honest options are to close or to do nothing. Doing nothing
        while recording a "defence" was the previous behaviour and it was a lie.
        A tested short near expiry carries the gamma risk spec section 14 names
        as the cost of trading short-dated, so the breach is closed.
        """
        return self.action in (
            Action.TAKE_PROFIT,
            Action.FLATTEN,
            Action.EXPIRE,
            Action.DEFEND,
        )

    @property
    def needs_new_ticket(self) -> bool:
        """A roll is a brand-new position request and re-enters the Risk Officer."""
        return self.action is Action.ROLL

    def as_dict(self) -> dict:
        return {"action": self.action.value, "reasons": list(self.reasons)}


def decide(
    position: PositionState,
    config: Config,
    today: date,
    *,
    flatten_only: bool = False,
    market_open: bool = True,
) -> LifecycleDecision:
    """Choose the single action for one open structure.

    Order (first match wins):
      1. expired                  -> EXPIRE
      2. inside forced-flatten    -> FLATTEN   (spec section 14, never carry to settlement)
      3. flatten-only mode        -> FLATTEN   (drawdown breach; reduce risk only)
      4. loss multiple breached   -> FLATTEN   (defend_mult stop)
      5. profit target reached    -> TAKE_PROFIT
      6. short strike breached    -> DEFEND    (roll the tested side, or close)
      7. near the flatten zone    -> ROLL      (extend duration while there is still credit)
      8. otherwise                -> HOLD
    """
    dte = position.dte(today)

    if dte < 0:
        return LifecycleDecision(Action.EXPIRE, (f"expired:dte={dte}",))

    if dte <= config.force_flatten_dte:
        return LifecycleDecision(
            Action.FLATTEN,
            (
                f"forced_flatten_zone:dte={dte}<={config.force_flatten_dte}",
                "never_held_into_settlement",
            ),
        )

    if flatten_only:
        return LifecycleDecision(Action.FLATTEN, ("flatten_only_mode",))

    if position.loss_multiple >= config.defend_mult:
        return LifecycleDecision(
            Action.FLATTEN,
            (
                f"stop_loss:cost_to_close={position.cost_to_close:.2f}"
                f">={config.defend_mult}x_credit={position.entry_credit:.2f}",
            ),
        )

    if position.profit_captured >= config.tp_frac_of_credit:
        return LifecycleDecision(
            Action.TAKE_PROFIT,
            (
                f"profit_target:captured={position.profit_captured:.3f}"
                f">={config.tp_frac_of_credit}",
            ),
        )

    breached = position.breached_short()
    if breached is not None:
        return LifecycleDecision(
            Action.DEFEND,
            (
                f"short_strike_breached:{breached}",
                f"spot={position.underlying_price:.2f}",
                f"dte={dte}",
            ),
        )

    # One day before the mandatory flatten zone, prefer rolling out to keeping a
    # position that will be force-closed tomorrow regardless of its P&L.
    if dte == config.force_flatten_dte + 1 and position.roll_count < 1:
        return LifecycleDecision(
            Action.ROLL,
            (f"approaching_flatten_zone:dte={dte}", f"roll_count={position.roll_count}"),
        )

    if not market_open:
        return LifecycleDecision(Action.HOLD, ("market_closed",))

    return LifecycleDecision(
        Action.HOLD,
        (
            f"dte={dte}",
            f"captured={position.profit_captured:.3f}",
            f"loss_multiple={position.loss_multiple:.2f}",
        ),
    )
