"""Frozen contest configuration (spec section 13).

Loaded once from config/config.yaml. Every number the Risk Officer enforces
lives in that file; nothing here invents a default that could silently diverge
from it. A missing or malformed key is a startup failure, not a fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .types import ALLOWED_STRUCTURES, Regime

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Config:
    underlyings: tuple[str, ...]
    entry_dte: tuple[int, int]
    force_flatten_dte: int
    max_loss_pct: float
    daily_new_risk_pct: float
    dd_flatten_pct: float
    tp_frac_of_credit: float
    defend_mult: float
    max_lots: int
    regime_max_lots: Mapping[str, int]
    max_open_structures: int
    min_credit_to_width: float
    max_short_leg_spread_pct: float
    max_quote_age_ms: int
    adx_trend_threshold: float
    iv_rank_low: float
    event_window_hours: int
    final_session_minutes: int

    @property
    def min_entry_dte(self) -> int:
        """Lowest DTE that can actually be opened.

        Spec section 28 check 10 requires both entry_dte[0] <= dte <= entry_dte[1]
        AND dte > force_flatten_dte. With entry_dte [1, 7] and force_flatten_dte 1
        the effective floor is 2. Both conditions are enforced separately in the
        officer; this property exists so the candidate builder does not waste a
        chain scan on expiries that can never pass.
        """
        return max(self.entry_dte[0], self.force_flatten_dte + 1)

    @property
    def max_entry_dte(self) -> int:
        return self.entry_dte[1]

    def max_lots_for_regime(self, regime: str) -> int:
        """Regime lot cap. An unknown regime is zero lots, never the global cap."""
        return int(self.regime_max_lots.get(regime, 0))


def _require(raw: Mapping[str, Any], key: str, kind: type) -> Any:
    if key not in raw:
        raise ConfigError(f"config is missing required key: {key}")
    value = raw[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, kind) or isinstance(value, bool) is (kind is not bool):
        if not isinstance(value, kind):
            raise ConfigError(f"config key {key} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _validate(cfg: Config) -> Config:
    """Configuration consistency checks (startup gate item 20)."""
    problems: list[str] = []

    if not cfg.underlyings:
        problems.append("underlyings is empty")
    if len(cfg.entry_dte) != 2 or cfg.entry_dte[0] > cfg.entry_dte[1]:
        problems.append(f"entry_dte {cfg.entry_dte} is not an ordered [low, high] pair")
    if cfg.force_flatten_dte < 0:
        problems.append("force_flatten_dte must be >= 0")
    if cfg.min_entry_dte > cfg.max_entry_dte:
        problems.append(
            f"no openable DTE exists: entry_dte {list(cfg.entry_dte)} with "
            f"force_flatten_dte {cfg.force_flatten_dte} leaves an empty window"
        )
    for name, value in (
        ("max_loss_pct", cfg.max_loss_pct),
        ("daily_new_risk_pct", cfg.daily_new_risk_pct),
        ("dd_flatten_pct", cfg.dd_flatten_pct),
        ("tp_frac_of_credit", cfg.tp_frac_of_credit),
        ("min_credit_to_width", cfg.min_credit_to_width),
        ("max_short_leg_spread_pct", cfg.max_short_leg_spread_pct),
    ):
        if not 0 < value <= 1:
            problems.append(f"{name} must sit in (0, 1], got {value}")
    if cfg.max_loss_pct > cfg.daily_new_risk_pct:
        problems.append(
            f"max_loss_pct {cfg.max_loss_pct} exceeds daily_new_risk_pct "
            f"{cfg.daily_new_risk_pct}: no single position could ever open"
        )
    if cfg.max_lots < 1:
        problems.append("max_lots must be >= 1")
    if cfg.max_open_structures < 1:
        problems.append("max_open_structures must be >= 1")
    if cfg.max_quote_age_ms <= 0:
        problems.append("max_quote_age_ms must be positive")
    if cfg.defend_mult <= 1:
        problems.append("defend_mult must be > 1")

    missing = {r.value for r in Regime} - set(cfg.regime_max_lots)
    if missing:
        problems.append(f"regime_max_lots is missing regimes: {sorted(missing)}")
    for regime, lots in cfg.regime_max_lots.items():
        if regime not in {r.value for r in Regime}:
            problems.append(f"regime_max_lots has unknown regime {regime!r}")
        if not isinstance(lots, int) or lots < 0:
            problems.append(f"regime_max_lots[{regime}] must be a non-negative int, got {lots!r}")
        elif lots > cfg.max_lots:
            problems.append(
                f"regime_max_lots[{regime}] = {lots} exceeds global max_lots {cfg.max_lots}"
            )
    # MOMENTUM and EVENT are no-new-entry regimes by spec section 17.
    for regime in (Regime.MOMENTUM.value, Regime.EVENT.value):
        if cfg.regime_max_lots.get(regime, 0) != 0:
            problems.append(f"regime_max_lots[{regime}] must be 0: no new entries permitted")

    if problems:
        raise ConfigError("configuration is inconsistent:\n  - " + "\n  - ".join(problems))
    return cfg


def load_config(path: Path | str | None = None) -> Config:
    """Read and validate config.yaml. Raises ConfigError rather than guessing."""
    path = Path(path or os.getenv("OG_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} did not parse to a mapping")

    unknown = set(raw) - {f for f in Config.__dataclass_fields__}
    if unknown:
        raise ConfigError(f"config has unknown keys: {sorted(unknown)}")

    entry_dte = _require(raw, "entry_dte", list)
    if len(entry_dte) != 2 or not all(isinstance(v, int) for v in entry_dte):
        raise ConfigError(f"entry_dte must be two integers, got {entry_dte!r}")

    cfg = Config(
        underlyings=tuple(_require(raw, "underlyings", list)),
        entry_dte=(int(entry_dte[0]), int(entry_dte[1])),
        force_flatten_dte=int(_require(raw, "force_flatten_dte", int)),
        max_loss_pct=float(_require(raw, "max_loss_pct", float)),
        daily_new_risk_pct=float(_require(raw, "daily_new_risk_pct", float)),
        dd_flatten_pct=float(_require(raw, "dd_flatten_pct", float)),
        tp_frac_of_credit=float(_require(raw, "tp_frac_of_credit", float)),
        defend_mult=float(_require(raw, "defend_mult", float)),
        max_lots=int(_require(raw, "max_lots", int)),
        regime_max_lots=dict(_require(raw, "regime_max_lots", dict)),
        max_open_structures=int(_require(raw, "max_open_structures", int)),
        min_credit_to_width=float(_require(raw, "min_credit_to_width", float)),
        max_short_leg_spread_pct=float(_require(raw, "max_short_leg_spread_pct", float)),
        max_quote_age_ms=int(_require(raw, "max_quote_age_ms", int)),
        adx_trend_threshold=float(_require(raw, "adx_trend_threshold", float)),
        iv_rank_low=float(_require(raw, "iv_rank_low", float)),
        event_window_hours=int(_require(raw, "event_window_hours", int)),
        final_session_minutes=int(_require(raw, "final_session_minutes", int)),
    )
    return _validate(cfg)


#: Structures the desk may ever trade. Re-exported so the Risk Officer's
#: allow-list check has one import, not a second copy of the list.
GLOBAL_STRUCTURE_ALLOWLIST = ALLOWED_STRUCTURES
