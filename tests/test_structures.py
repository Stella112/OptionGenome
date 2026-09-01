"""Structure validation tests (spec sections 9.3 and 23).

The forbidden-structure cases are the ones that matter: an undefined-risk shape
must never be coercible into a permitted one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.rolldesk.structures import (
    StructureError,
    derive_geometry,
    validate_structure,
)
from src.types import Leg

from .conftest import EXPIRY, condor_legs, pcs_legs, sym


def test_put_credit_spread_geometry():
    geometry = derive_geometry(pcs_legs("640", "635"))
    assert geometry.structure_type == "put_credit_spread"
    assert geometry.underlying == "SPY"
    assert geometry.expiry == EXPIRY
    assert geometry.width == Decimal("5")
    assert geometry.short_strikes == (Decimal("640"),)


def test_iron_condor_geometry():
    geometry = derive_geometry(condor_legs())
    assert geometry.structure_type == "iron_condor"
    assert geometry.width == Decimal("5")
    assert geometry.short_strikes == (Decimal("635"), Decimal("650"))
    assert geometry.long_strikes == (Decimal("630"), Decimal("655"))


def test_iron_condor_width_is_the_wider_wing():
    """Only one side can lose, so risk is the wider wing, never the sum."""
    legs = (
        Leg(sym("620", "P"), "buy", "buy_to_open", 1),  # 15-wide put wing
        Leg(sym("635", "P"), "sell", "sell_to_open", 1),
        Leg(sym("650", "C"), "sell", "sell_to_open", 1),  # 5-wide call wing
        Leg(sym("655", "C"), "buy", "buy_to_open", 1),
    )
    assert derive_geometry(legs).width == Decimal("15")


def test_max_loss_per_lot_uses_width_minus_credit():
    geometry = derive_geometry(pcs_legs("640", "635"))
    assert geometry.max_loss_per_lot(1.00) == pytest.approx(400.0)


# --- forbidden and malformed structures (spec section 9.3) -------------------


def test_naked_put_is_rejected():
    with pytest.raises(StructureError):
        derive_geometry((Leg(sym("640"), "sell", "sell_to_open", 1),))


def test_naked_call_is_rejected():
    with pytest.raises(StructureError):
        derive_geometry((Leg(sym("650", "C"), "sell", "sell_to_open", 1),))


def test_strangle_is_rejected():
    """Two short legs of opposite rights is undefined risk, not a credit spread."""
    legs = (
        Leg(sym("635", "P"), "sell", "sell_to_open", 1),
        Leg(sym("650", "C"), "sell", "sell_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_straddle_is_rejected():
    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_open", 1),
        Leg(sym("640", "C"), "sell", "sell_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_put_debit_spread_is_rejected():
    """Short the lower strike is a debit spread, not the permitted credit spread."""
    legs = (
        Leg(sym("635", "P"), "sell", "sell_to_open", 1),
        Leg(sym("640", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_ratio_spread_is_rejected():
    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_open", 2),
        Leg(sym("635", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_butterfly_is_rejected_three_legs():
    legs = (
        Leg(sym("630", "P"), "buy", "buy_to_open", 1),
        Leg(sym("635", "P"), "sell", "sell_to_open", 1),
        Leg(sym("640", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_calendar_spread_is_rejected():
    from datetime import timedelta

    legs = (
        Leg(sym("640", "P", EXPIRY), "sell", "sell_to_open", 1),
        Leg(sym("640", "P", EXPIRY + timedelta(days=7)), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError, match="multiple expiries"):
        derive_geometry(legs)


def test_mixed_underlyings_rejected():
    from src.broker.occ import build_option_symbol

    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_open", 1),
        Leg(build_option_symbol("QQQ", EXPIRY, "P", Decimal("635")), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError, match="multiple underlyings"):
        derive_geometry(legs)


def test_zero_width_spread_rejected():
    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_open", 1),
        Leg(sym("640", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


def test_empty_legs_rejected():
    with pytest.raises(StructureError, match="no legs"):
        derive_geometry(())


def test_closing_intent_on_opening_ticket_rejected():
    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_close", 1),
        Leg(sym("635", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError, match="closing intent"):
        derive_geometry(legs)


def test_side_contradicting_position_intent_rejected():
    legs = (
        Leg(sym("640", "P"), "buy", "sell_to_open", 1),
        Leg(sym("635", "P"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError, match="contradicts"):
        derive_geometry(legs)


def test_unreduced_ratios_rejected():
    legs = (
        Leg(sym("640", "P"), "sell", "sell_to_open", 2),
        Leg(sym("635", "P"), "buy", "buy_to_open", 2),
    )
    with pytest.raises(StructureError, match="GCD"):
        derive_geometry(legs)


def test_condor_with_inverted_wing_rejected():
    legs = (
        Leg(sym("630", "P"), "sell", "sell_to_open", 1),  # short the protective strike
        Leg(sym("635", "P"), "buy", "buy_to_open", 1),
        Leg(sym("650", "C"), "sell", "sell_to_open", 1),
        Leg(sym("655", "C"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError, match="put wing"):
        derive_geometry(legs)


def test_condor_with_crossed_shorts_rejected():
    legs = (
        Leg(sym("645", "P"), "buy", "buy_to_open", 1),
        Leg(sym("650", "P"), "sell", "sell_to_open", 1),
        Leg(sym("640", "C"), "sell", "sell_to_open", 1),
        Leg(sym("655", "C"), "buy", "buy_to_open", 1),
    )
    with pytest.raises(StructureError):
        derive_geometry(legs)


# --- claimed type must match derived type ------------------------------------


def test_validate_structure_accepts_matching_claim():
    assert validate_structure(pcs_legs(), "put_credit_spread").width == Decimal("5")


def test_validate_structure_rejects_mismatched_claim():
    with pytest.raises(StructureError, match="claims"):
        validate_structure(pcs_legs(), "iron_condor")


def test_validate_structure_rejects_unknown_claim():
    with pytest.raises(StructureError, match="claims"):
        validate_structure(pcs_legs(), "naked_put")
