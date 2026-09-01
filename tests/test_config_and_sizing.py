"""Frozen-config validation (spec section 13) and lot sizing (spec section 29)."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from src.config import ConfigError, load_config
from src.risk.sizing import compute_sizing

BASE = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))


def write_config(tmp_path, **overrides):
    payload = dict(BASE)
    payload.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


# --- the shipped config ------------------------------------------------------


def test_shipped_config_matches_the_frozen_spec(config):
    assert config.underlyings == ("SPY",)
    assert config.entry_dte == (1, 7)
    assert config.force_flatten_dte == 1
    assert config.max_loss_pct == 0.0075
    assert config.daily_new_risk_pct == 0.02
    assert config.dd_flatten_pct == 0.05
    assert config.tp_frac_of_credit == 0.50
    assert config.defend_mult == 2.0
    assert config.max_lots == 1
    assert config.max_open_structures == 3
    assert config.min_credit_to_width == 0.15
    assert config.max_short_leg_spread_pct == 0.20
    assert config.max_quote_age_ms == 5000
    assert config.adx_trend_threshold == 25
    assert config.iv_rank_low == 30
    assert config.event_window_hours == 24
    assert config.final_session_minutes == 15


def test_qqq_is_not_enabled_yet(config):
    """Spec section 24: QQQ waits for one clean SPY session."""
    assert "QQQ" not in config.underlyings


def test_effective_entry_window_is_two_to_seven(config):
    """entry_dte [1,7] with force_flatten_dte 1 leaves 2-7 actually openable."""
    assert config.min_entry_dte == 2
    assert config.max_entry_dte == 7


def test_blocked_regimes_are_pinned_to_zero_lots(config):
    assert config.max_lots_for_regime("MOMENTUM") == 0
    assert config.max_lots_for_regime("EVENT") == 0
    assert config.max_lots_for_regime("INCOME") == 1
    assert config.max_lots_for_regime("COMPRESSION") == 1


def test_unknown_regime_gets_zero_lots(config):
    """An unrecognised regime must never inherit the global cap."""
    assert config.max_lots_for_regime("SOMETHING_NEW") == 0


# --- validation --------------------------------------------------------------


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(write_config(tmp_path, leverage=3))


def test_missing_key_is_rejected(tmp_path):
    payload = dict(BASE)
    del payload["max_loss_pct"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="missing required key"):
        load_config(path)


def test_position_risk_above_daily_budget_is_rejected(tmp_path):
    """If one position can exceed the daily budget, nothing could ever open."""
    with pytest.raises(ConfigError, match="no single position"):
        load_config(write_config(tmp_path, max_loss_pct=0.05, daily_new_risk_pct=0.02))


def test_nonzero_lots_in_a_blocked_regime_is_rejected(tmp_path):
    lots = dict(BASE["regime_max_lots"])
    lots["MOMENTUM"] = 1
    with pytest.raises(ConfigError, match="MOMENTUM"):
        load_config(write_config(tmp_path, regime_max_lots=lots))


def test_regime_lots_above_global_cap_is_rejected(tmp_path):
    lots = dict(BASE["regime_max_lots"])
    lots["INCOME"] = 5
    with pytest.raises(ConfigError, match="exceeds global max_lots"):
        load_config(write_config(tmp_path, regime_max_lots=lots))


def test_missing_regime_is_rejected(tmp_path):
    lots = dict(BASE["regime_max_lots"])
    del lots["EVENT"]
    with pytest.raises(ConfigError, match="missing regimes"):
        load_config(write_config(tmp_path, regime_max_lots=lots))


def test_empty_entry_window_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no openable DTE"):
        load_config(write_config(tmp_path, entry_dte=[1, 3], force_flatten_dte=5))


def test_inverted_entry_window_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, entry_dte=[7, 1]))


@pytest.mark.parametrize("key", ["max_loss_pct", "dd_flatten_pct", "min_credit_to_width"])
def test_out_of_range_fractions_are_rejected(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(tmp_path, **{key: 1.5}))


# --- lot sizing (spec section 29) -------------------------------------------


def sizing(**overrides):
    defaults = dict(
        daily_new_risk_pct=0.02,
        start_of_day_equity=100_000.0,
        day_open_risk=0.0,
        max_loss_per_lot=400.0,
        global_max_lots=1,
        regime_max_lots=1,
    )
    defaults.update(overrides)
    return compute_sizing(**defaults)


def test_sizing_respects_the_global_cap():
    """$2000 of budget buys 5 lots of $400 risk, but max_lots pins it to 1."""
    result = sizing()
    assert result.risk_based_lots == 5
    assert result.allowed_lots == 1
    assert result.binding_constraint in ("global_max_lots", "regime_max_lots")


def test_sizing_respects_the_regime_cap():
    assert sizing(regime_max_lots=0).allowed_lots == 0


def test_sizing_respects_the_risk_budget():
    result = sizing(global_max_lots=10, regime_max_lots=10, max_loss_per_lot=900.0)
    assert result.risk_based_lots == 2
    assert result.allowed_lots == 2
    assert result.binding_constraint == "risk_budget"


def test_sizing_returns_zero_when_budget_is_spent():
    assert sizing(day_open_risk=2_000.0).allowed_lots == 0


def test_sizing_returns_zero_when_budget_is_overdrawn():
    result = sizing(day_open_risk=5_000.0)
    assert result.risk_budget < 0
    assert result.allowed_lots == 0


def test_sizing_returns_zero_when_a_single_lot_is_unaffordable():
    assert sizing(global_max_lots=10, regime_max_lots=10, max_loss_per_lot=50_000.0).allowed_lots == 0


def test_sizing_returns_zero_on_broken_geometry():
    """Non-positive per-lot risk is a broken structure, not a free position."""
    assert sizing(max_loss_per_lot=0.0).allowed_lots == 0
    assert sizing(max_loss_per_lot=-100.0).allowed_lots == 0


def test_sizing_never_returns_negative_lots():
    assert sizing(day_open_risk=99_999.0).allowed_lots >= 0


def test_sizing_floors_rather_than_rounds():
    """1.9 lots of budget is one lot, never two."""
    result = sizing(global_max_lots=10, regime_max_lots=10, max_loss_per_lot=1_052.0)
    assert result.risk_based_lots == 1
