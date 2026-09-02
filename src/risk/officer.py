"""The Risk Officer: the only capital gate (spec sections 11, 12, 28, 29).

Properties this module guarantees:

  * pure -- no network, no filesystem, no database, no broker, no clock read
  * deterministic -- same inputs, same verdict, always
  * exhaustive -- every check runs; failures accumulate, they do not short-circuit
  * distrustful -- width, credit, max_loss, dte, structure_type and the leg
    relationships are RECALCULATED from the legs and validated quotes. A field
    the ticket claims is never the field the officer enforces (spec section 12)
  * fail-closed -- any exception is DENY with reason `officer_exception`

No ALLOW means no broker command may be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..config import GLOBAL_STRUCTURE_ALLOWLIST, Config
from ..rolldesk.ratios import is_reduced
from ..rolldesk.structures import Geometry, StructureError, derive_geometry, parse_legs
from ..types import (
    CONTRACT_MULTIPLIER,
    Account,
    Book,
    Leg,
    Permission,
    Quote,
    RiskDecision,
    Ticket,
)
from .sizing import Sizing, compute_sizing

#: Two short structures on the same underlying inside this many calendar days
#: are treated as overlapping exposure (spec section 28 check 6, "adjacent
#: expiry"). SPY lists expiries every trading day, so the adjacent expiry is
#: one or two calendar days away. This was 7, which inside a 2-18 DTE window
#: left room for roughly one position at a time and starved the desk of the
#: three concurrent structures max_open_structures permits.
ADJACENT_EXPIRY_DAYS = 2

#: Tolerance when comparing a ticket's claimed values against recalculated ones.
#: Tight enough to catch a wrong number, loose enough to survive float noise.
CLAIM_TOLERANCE = 0.01

#: How far ahead of our clock a broker quote timestamp may sit before it is
#: treated as anomalous rather than as clock skew.
#:
#: Alpaca stamps quotes on its own clock, which drifts from ours by tens of
#: milliseconds. Rejecting ANY future timestamp denied every ticket on a live
#: run ("quote_timestamp_in_future:-49ms") and would have blocked trading for
#: the whole session. A genuinely future-dated quote is still refused; a quote
#: 49ms ahead is just two clocks disagreeing.
MAX_CLOCK_SKEW_MS = 2000.0


@dataclass
class _Recalc:
    """Independently derived facts. Populated best-effort so later checks still run."""

    geometry: Geometry | None = None
    credit: float | None = None  # net credit per share, from quotes
    width: float | None = None
    max_loss_per_lot: float | None = None
    dte: int | None = None
    max_quote_age_ms: float | None = None
    short_leg_spread: float | None = None
    structure_error: str | None = None
    quote_error: str | None = None


def _net_credit(legs: tuple[Leg, ...], quotes) -> float:
    """Net credit per share: short mids received minus long mids paid.

    Raises KeyError via the caller's guard if any leg is unquoted; an unquoted
    leg must never be silently priced at zero.
    """
    credit = 0.0
    for leg in legs:
        quote: Quote = quotes[leg.symbol]
        signed = quote.mid * leg.ratio_qty
        credit += signed if leg.side == "sell" else -signed
    return credit


def _recalculate(ticket: Ticket, book: Book, today: date) -> _Recalc:
    """Derive every safety-critical value from the legs and quotes themselves."""
    out = _Recalc()

    try:
        out.geometry = derive_geometry(ticket.legs)
        out.width = float(out.geometry.width)
        out.dte = (out.geometry.expiry - today).days
    except StructureError as exc:
        out.structure_error = str(exc)

    quotes = book.quotes or {}
    missing = [leg.symbol for leg in ticket.legs if leg.symbol not in quotes]
    if missing:
        out.quote_error = f"no validated quote for {', '.join(sorted(missing))}"
        return out

    unusable = [
        leg.symbol for leg in ticket.legs if not quotes[leg.symbol].is_two_sided
    ]
    if unusable:
        out.quote_error = f"no two-sided market on {', '.join(sorted(unusable))}"
        return out

    now = book.now
    if now is None:
        out.quote_error = "book has no decision timestamp; quote age cannot be verified"
    else:
        out.max_quote_age_ms = max(quotes[leg.symbol].age_ms(now) for leg in ticket.legs)

    out.credit = _net_credit(ticket.legs, quotes)

    short_symbols = [leg.symbol for leg in ticket.legs if leg.side == "sell"]
    if short_symbols:
        out.short_leg_spread = max(quotes[s].spread for s in short_symbols)

    if out.width is not None and out.credit is not None:
        out.max_loss_per_lot = (out.width - out.credit) * CONTRACT_MULTIPLIER

    return out


# --- the fifteen checks ------------------------------------------------------
# Each appends machine-readable reason codes. None of them return early.


def _check_1_regime_permits(ticket: Ticket, permission: Permission, reasons: list[str]) -> None:
    if permission.max_lots <= 0:
        reasons.append(f"regime_no_new_entries:{permission.regime}")
    if ticket.structure_type not in permission.allowed_strategies:
        reasons.append(f"structure_not_permitted_in_regime:{permission.regime}:{ticket.structure_type}")
    if ticket.regime != permission.regime:
        reasons.append(f"ticket_regime_mismatch:{ticket.regime}!={permission.regime}")


def _check_2_global_allowlist(ticket: Ticket, rc: _Recalc, reasons: list[str]) -> None:
    if ticket.structure_type not in GLOBAL_STRUCTURE_ALLOWLIST:
        reasons.append(f"structure_not_in_global_allowlist:{ticket.structure_type}")
    # The derived shape must also be allowed, not merely the claimed one.
    if rc.geometry is not None and rc.geometry.structure_type not in GLOBAL_STRUCTURE_ALLOWLIST:
        reasons.append(f"derived_structure_not_in_global_allowlist:{rc.geometry.structure_type}")


def _check_3_max_loss_pct(
    account: Account, config: Config, rc: _Recalc, lots: int, reasons: list[str]
) -> None:
    if rc.max_loss_per_lot is None:
        reasons.append("max_loss_not_recalculable")
        return
    if account.equity <= 0:
        reasons.append("non_positive_equity")
        return
    max_loss = rc.max_loss_per_lot * max(lots, 1)
    if max_loss / account.equity > config.max_loss_pct:
        reasons.append(
            f"max_loss_pct_exceeded:{max_loss:.2f}/{account.equity:.2f}"
            f"={max_loss / account.equity:.5f}>{config.max_loss_pct}"
        )


def _check_4_daily_risk(
    account: Account, book: Book, config: Config, rc: _Recalc, lots: int, reasons: list[str]
) -> None:
    if rc.max_loss_per_lot is None:
        reasons.append("daily_risk_not_recalculable")
        return
    projected = book.day_open_risk + rc.max_loss_per_lot * max(lots, 1)
    budget = config.daily_new_risk_pct * account.start_of_day_equity
    if projected > budget:
        reasons.append(f"daily_risk_exceeded:{projected:.2f}>{budget:.2f}")


def _check_5_drawdown(account: Account, config: Config, reasons: list[str]) -> None:
    if account.drawdown >= config.dd_flatten_pct:
        reasons.append(f"drawdown_flatten_only:{account.drawdown:.5f}>={config.dd_flatten_pct}")


def _check_6_overlapping_short(
    ticket: Ticket, book: Book, rc: _Recalc, reasons: list[str]
) -> None:
    if rc.geometry is None:
        reasons.append("overlap_not_checkable_without_geometry")
        return
    for existing in book.open_structures:
        if existing.underlying != rc.geometry.underlying:
            continue
        gap = abs((existing.expiry - rc.geometry.expiry).days)
        if gap == 0:
            reasons.append(
                f"overlapping_short_same_expiry:{existing.underlying}:{existing.expiry.isoformat()}"
            )
        elif gap <= ADJACENT_EXPIRY_DAYS:
            reasons.append(
                f"overlapping_short_adjacent_expiry:{existing.underlying}:"
                f"{existing.expiry.isoformat()}:gap={gap}d"
            )


def _check_7_credit_to_width(config: Config, rc: _Recalc, reasons: list[str]) -> None:
    if rc.credit is None or rc.width is None:
        reasons.append("credit_to_width_not_recalculable")
        return
    if rc.width <= 0:
        reasons.append("non_positive_width")
        return
    if rc.credit <= 0:
        reasons.append(f"not_a_credit:{rc.credit:.4f}")
        return
    ratio = rc.credit / rc.width
    if ratio < config.min_credit_to_width:
        reasons.append(f"credit_to_width_below_min:{ratio:.5f}<{config.min_credit_to_width}")


def _check_8_short_leg_spread(config: Config, rc: _Recalc, reasons: list[str]) -> None:
    """Spec section 28 check 8: spread <= max_short_leg_spread_pct * credit.

    Note this is a fraction of the NET CREDIT, not of the leg's own mid -- a
    deliberately tight liquidity gate that only penny-wide markets clear.
    """
    if rc.short_leg_spread is None or rc.credit is None:
        reasons.append("short_leg_spread_not_recalculable")
        return
    if rc.credit <= 0:
        return  # already reported by check 7; no meaningful budget to compare against
    budget = config.max_short_leg_spread_pct * rc.credit
    if rc.short_leg_spread > budget:
        reasons.append(f"short_leg_spread_too_wide:{rc.short_leg_spread:.4f}>{budget:.4f}")


def _check_9_quote_age(config: Config, rc: _Recalc, reasons: list[str]) -> None:
    if rc.quote_error:
        reasons.append(f"quote_unusable:{rc.quote_error}")
        return
    if rc.max_quote_age_ms is None:
        reasons.append("quote_age_not_verifiable")
        return
    if rc.max_quote_age_ms < -MAX_CLOCK_SKEW_MS:
        reasons.append(
            f"quote_timestamp_in_future:{rc.max_quote_age_ms:.0f}ms"
            f"_beyond_{MAX_CLOCK_SKEW_MS:.0f}ms_skew_allowance"
        )
        return
    if rc.max_quote_age_ms >= config.max_quote_age_ms:
        reasons.append(f"quote_stale:{rc.max_quote_age_ms:.0f}ms>={config.max_quote_age_ms}ms")


def _check_10_dte(config: Config, rc: _Recalc, reasons: list[str]) -> None:
    if rc.dte is None:
        reasons.append("dte_not_recalculable")
        return
    low, high = config.entry_dte
    if not (low <= rc.dte <= high):
        reasons.append(f"dte_out_of_entry_window:{rc.dte}not_in[{low},{high}]")
    if rc.dte <= config.force_flatten_dte:
        reasons.append(f"dte_in_forced_flatten_zone:{rc.dte}<={config.force_flatten_dte}")


def _check_11_session(book: Book, config: Config, reasons: list[str]) -> None:
    if not book.market_open:
        reasons.append("market_closed")
    if book.minutes_to_close <= config.final_session_minutes:
        reasons.append(
            f"final_session_window:{book.minutes_to_close}m<={config.final_session_minutes}m"
        )


def _check_12_open_structures(book: Book, config: Config, reasons: list[str]) -> None:
    if book.open_count >= config.max_open_structures:
        reasons.append(f"max_open_structures:{book.open_count}>={config.max_open_structures}")


def _check_13_ratios(ticket: Ticket, reasons: list[str]) -> None:
    if not ticket.legs:
        reasons.append("no_legs")
        return
    if not is_reduced([leg.ratio_qty for leg in ticket.legs]):
        reasons.append(f"ratios_not_reduced:{list(ticket.ratios)}")


def _check_14_account(account: Account, reasons: list[str]) -> None:
    if not account.allowed_account_id:
        reasons.append("no_allowed_account_configured")
        return
    if account.account_id != account.allowed_account_id:
        reasons.append(f"account_mismatch:{account.account_id}!={account.allowed_account_id}")


def _check_15_internal_consistency(
    ticket: Ticket, rc: _Recalc, today: date, reasons: list[str]
) -> None:
    """Every claimed safety field must match what the legs and quotes actually say."""
    if rc.structure_error:
        reasons.append(f"invalid_structure:{rc.structure_error}")
        return

    geometry = rc.geometry
    assert geometry is not None  # structure_error is None, so geometry parsed

    if geometry.structure_type != ticket.structure_type:
        reasons.append(
            f"structure_type_mismatch:claimed={ticket.structure_type}:derived={geometry.structure_type}"
        )
    if geometry.underlying != ticket.underlying:
        reasons.append(
            f"underlying_mismatch:claimed={ticket.underlying}:derived={geometry.underlying}"
        )

    try:
        claimed_expiry = ticket.expiry_date
    except ValueError:
        reasons.append(f"expiry_unparseable:{ticket.expiry}")
        claimed_expiry = None
    if claimed_expiry is not None and claimed_expiry != geometry.expiry:
        reasons.append(
            f"expiry_mismatch:claimed={ticket.expiry}:derived={geometry.expiry.isoformat()}"
        )

    if rc.width is not None and abs(ticket.width - rc.width) > CLAIM_TOLERANCE:
        reasons.append(f"width_mismatch:claimed={ticket.width}:derived={rc.width}")
    if rc.dte is not None and ticket.dte != rc.dte:
        reasons.append(f"dte_mismatch:claimed={ticket.dte}:derived={rc.dte}")
    if rc.credit is not None and abs(ticket.credit_mid - rc.credit) > CLAIM_TOLERANCE:
        reasons.append(f"credit_mismatch:claimed={ticket.credit_mid}:derived={rc.credit:.4f}")
    if rc.max_loss_per_lot is not None:
        lots = max(ticket.proposed_lots, 1)
        derived_total = rc.max_loss_per_lot * lots
        if abs(ticket.max_loss - derived_total) > CLAIM_TOLERANCE * CONTRACT_MULTIPLIER:
            reasons.append(
                f"max_loss_mismatch:claimed={ticket.max_loss}:derived={derived_total:.2f}"
            )

    if not isinstance(ticket.proposed_lots, int) or ticket.proposed_lots < 1:
        reasons.append(f"invalid_proposed_lots:{ticket.proposed_lots!r}")


# --- entry point -------------------------------------------------------------


def evaluate(
    ticket: Ticket,
    book: Book,
    account: Account,
    regime: Permission,
    config: Config,
    *,
    today: date | None = None,
) -> RiskDecision:
    """Return ALLOW or DENY with every failure reason and the derived lot count.

    `today` defaults to the book's own decision timestamp, keeping the officer
    free of clock reads and therefore deterministic under test.
    """
    try:
        decision_day = today or (book.now.date() if book.now else None)
        if decision_day is None:
            return RiskDecision(
                decision="DENY",
                reasons=("no_decision_timestamp",),
                allowed_lots=0,
            )

        rc = _recalculate(ticket, book, decision_day)

        sizing: Sizing = compute_sizing(
            daily_new_risk_pct=config.daily_new_risk_pct,
            start_of_day_equity=account.start_of_day_equity,
            day_open_risk=book.day_open_risk,
            max_loss_per_lot=rc.max_loss_per_lot or 0.0,
            global_max_lots=config.max_lots,
            regime_max_lots=min(regime.max_lots, config.max_lots_for_regime(regime.regime)),
        )

        reasons: list[str] = []
        _check_1_regime_permits(ticket, regime, reasons)
        _check_2_global_allowlist(ticket, rc, reasons)
        _check_3_max_loss_pct(account, config, rc, sizing.allowed_lots, reasons)
        _check_4_daily_risk(account, book, config, rc, sizing.allowed_lots, reasons)
        _check_5_drawdown(account, config, reasons)
        _check_6_overlapping_short(ticket, book, rc, reasons)
        _check_7_credit_to_width(config, rc, reasons)
        _check_8_short_leg_spread(config, rc, reasons)
        _check_9_quote_age(config, rc, reasons)
        _check_10_dte(config, rc, reasons)
        _check_11_session(book, config, reasons)
        _check_12_open_structures(book, config, reasons)
        _check_13_ratios(ticket, reasons)
        _check_14_account(account, reasons)
        _check_15_internal_consistency(ticket, rc, decision_day, reasons)

        if sizing.allowed_lots < 1:
            reasons.append(
                f"allowed_lots_below_one:risk_budget={sizing.risk_budget:.2f}:"
                f"risk_lots={sizing.risk_based_lots}:regime_lots={sizing.regime_max_lots}"
            )

        if reasons:
            return RiskDecision(decision="DENY", reasons=tuple(reasons), allowed_lots=0)
        return RiskDecision(decision="ALLOW", reasons=(), allowed_lots=sizing.allowed_lots)

    except Exception as exc:  # fail closed, always
        return RiskDecision(
            decision="DENY",
            reasons=(f"officer_exception:{type(exc).__name__}:{exc}",),
            allowed_lots=0,
        )


def self_test() -> bool:
    """Startup gate item 18: prove the officer denies an obviously bad ticket.

    Deliberately minimal and dependency-free -- the full behaviour is covered by
    the unit tests. This exists so a broken deployment halts before trading.
    """
    from datetime import timedelta, timezone

    now = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    naked = Ticket(
        ticket_id="selftest-naked",
        underlying="SPY",
        structure_type="naked_put",
        expiry=(now.date() + timedelta(days=4)).isoformat(),
        dte=4,
        legs=(Leg("SPY260905P00640000", "sell", "sell_to_open", 1),),
        credit_mid=1.0,
        width=0.0,
        max_loss=0.0,
        short_delta=-0.2,
        quote_age_ms=10,
        regime="INCOME",
        proposed_lots=1,
    )
    account = Account(
        account_id="X",
        allowed_account_id="X",
        equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        options_level=3,
        start_of_day_equity=100_000.0,
        high_water_mark=100_000.0,
    )
    permission = Permission(regime="INCOME", allowed_strategies=("put_credit_spread",), max_lots=1, reasons=())
    from ..config import load_config

    verdict = evaluate(naked, Book(now=now), account, permission, load_config())
    return verdict.decision == "DENY"
