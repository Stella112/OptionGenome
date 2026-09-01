"""Lot sizing (spec section 29).

Size is derived here and nowhere else. The model never determines size, and a
ticket's proposed_lots is a claim the officer ignores when computing capital.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class Sizing:
    """The full sizing derivation, kept so the journal can show the binding constraint."""

    risk_budget: float
    max_loss_per_lot: float
    risk_based_lots: int
    global_max_lots: int
    regime_max_lots: int
    allowed_lots: int

    @property
    def binding_constraint(self) -> str:
        if self.allowed_lots == self.risk_based_lots:
            return "risk_budget"
        if self.allowed_lots == self.regime_max_lots:
            return "regime_max_lots"
        return "global_max_lots"


def compute_sizing(
    *,
    daily_new_risk_pct: float,
    start_of_day_equity: float,
    day_open_risk: float,
    max_loss_per_lot: float,
    global_max_lots: int,
    regime_max_lots: int,
) -> Sizing:
    """Derive allowed_lots. Returns zero lots rather than raising on a spent budget.

    risk_budget  = daily_new_risk_pct * start_of_day_equity - day_open_risk
    risk_lots    = floor(risk_budget / max_loss_per_lot)
    allowed_lots = min(global_max_lots, regime_max_lots, risk_lots)
    """
    risk_budget = daily_new_risk_pct * start_of_day_equity - day_open_risk

    if max_loss_per_lot <= 0:
        # A non-positive per-lot risk means the geometry is broken, not that the
        # position is free. Zero lots, and the officer's consistency check reports why.
        risk_based_lots = 0
    elif risk_budget <= 0:
        risk_based_lots = 0
    else:
        risk_based_lots = floor(risk_budget / max_loss_per_lot)

    allowed_lots = min(global_max_lots, regime_max_lots, risk_based_lots)
    return Sizing(
        risk_budget=risk_budget,
        max_loss_per_lot=max_loss_per_lot,
        risk_based_lots=risk_based_lots,
        global_max_lots=global_max_lots,
        regime_max_lots=regime_max_lots,
        allowed_lots=max(0, allowed_lots),
    )
