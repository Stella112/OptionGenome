"""MarketDNA regime classification and permission emission (spec sections 16-18).

Deterministic. No model may influence the regime. MarketDNA's only question is
"what is legally allowed right now?" -- it never selects strikes, chooses an
expiry, sizes a position, ranks candidates, or submits an order.

Evaluation order is mandatory and the first matching condition wins:
    1. EVENT        event inside event_window_hours
    2. MOMENTUM     ADX >= adx_trend_threshold
    3. COMPRESSION  IV rank < iv_rank_low
    4. INCOME       otherwise
"""

from __future__ import annotations

from ..config import Config
from ..types import Permission, Regime, StructureType

#: Selling premium pays when implied volatility exceeds what the underlying goes
#: on to realise -- the variance risk premium. When implied sits at or below
#: realised, the desk would be selling volatility cheaply, which is the one
#: condition under which this strategy has no edge to begin with.
#:
#: The cushion keeps marginal cases out rather than trading a coin flip.
MIN_IV_TO_RV = 1.05

#: Structures each regime may open. MOMENTUM and EVENT open nothing; existing
#: positions stay manageable in every regime (spec section 17).
REGIME_STRATEGIES: dict[Regime, tuple[str, ...]] = {
    Regime.INCOME: (
        StructureType.PUT_CREDIT_SPREAD.value,
        StructureType.IRON_CONDOR.value,
    ),
    Regime.COMPRESSION: (
        StructureType.PUT_CREDIT_SPREAD.value,
        StructureType.IRON_CONDOR.value,
    ),
    Regime.MOMENTUM: (),
    Regime.EVENT: (),
}


def classify(signals, config: Config) -> tuple[Regime, tuple[str, ...]]:
    """Return the regime and the machine-readable reasons behind it.

    `signals` is a marketdna.indicators.Signals. Order of evaluation is fixed;
    reasons record both the trigger and the conditions that were ruled out.
    """
    if signals.hours_to_next_event <= config.event_window_hours:
        return Regime.EVENT, (
            "event_in_window",
            f"event={signals.next_event_name or 'unnamed'}",
            f"hours_to_event={signals.hours_to_next_event:.2f}",
            f"event_window_hours={config.event_window_hours}",
        )

    if signals.adx >= config.adx_trend_threshold:
        return Regime.MOMENTUM, (
            "no_event_in_window",
            "adx_at_or_above_threshold",
            f"adx={signals.adx:.2f}",
            f"adx_trend_threshold={config.adx_trend_threshold}",
        )

    if signals.iv_rank < config.iv_rank_low:
        return Regime.COMPRESSION, (
            "no_event_in_window",
            "adx_below_threshold",
            "iv_rank_below_threshold",
            f"iv_rank={signals.iv_rank:.2f}",
            f"iv_rank_low={config.iv_rank_low}",
        )

    return Regime.INCOME, (
        "no_event_in_window",
        "adx_below_threshold",
        "iv_rank_at_or_above_threshold",
        f"adx={signals.adx:.2f}",
        f"iv_rank={signals.iv_rank:.2f}",
    )


def permissions_for(signals, config: Config) -> Permission:
    """MarketDNA's entire output surface (spec section 18).

    Roll Desk consumes only this object. An empty allowed_strategies list is the
    normal blocked-entry path -- Roll Desk must produce NO_CANDIDATE from it
    without special-casing MOMENTUM or EVENT.
    """
    regime, reasons = classify(signals, config)
    max_lots = min(config.max_lots, config.max_lots_for_regime(regime.value))

    # The regime says what kind of tape this is. This says whether selling
    # premium into it is worth doing at all. The regime classification itself
    # is untouched, so the spec's four-way scheme and its mandatory evaluation
    # order still hold; this only withholds permission.
    rv, iv = signals.realized_vol, signals.implied_vol
    if max_lots > 0 and rv > 0 and iv <= rv * MIN_IV_TO_RV:
        max_lots = 0
        reasons = reasons + (
            "no_variance_risk_premium",
            f"implied_vol={iv:.4f}",
            f"realized_vol={rv:.4f}",
            f"required_ratio={MIN_IV_TO_RV}",
        )

    strategies = REGIME_STRATEGIES[regime] if max_lots > 0 else ()
    return Permission(
        regime=regime.value,
        allowed_strategies=strategies,
        max_lots=max_lots,
        reasons=reasons,
    )
