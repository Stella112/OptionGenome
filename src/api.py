"""FastAPI service and dashboard.

Read-only by design: this process observes the desk, it never trades. Nothing
here can submit an order, and no endpoint mutates desk state -- the judging
demo cannot become an execution path.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .config import ConfigError, load_config
from .journal import Journal
from .startup import run_gate

app = FastAPI(title="OptionGenome", docs_url="/api/docs", redoc_url=None)

#: Nothing here may be cached. The dashboard reports live desk state, so a
#: browser holding a stale copy shows a judge the wrong equity, the wrong regime
#: and a refusal count that stopped moving. Deployed updates were invisible for
#: the same reason until these headers existed.
NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.middleware("http")
async def no_store(request, call_next):
    response = await call_next(request)
    response.headers.update(NO_STORE)
    return response

DASHBOARD = Path(__file__).parent / "dashboard.html"
DECK = Path(__file__).parent.parent / "docs" / "deck.html"

#: The deck source is a document fragment: title, style and content, with no
#: charset declaration of its own. Served raw that renders as mojibake, so the
#: skeleton is supplied here rather than duplicating the deck into a second file.
DECK_SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="OptionGenome - an autonomous defined-risk options desk on Alpaca paper trading.">
<style>html{{color-scheme:light dark}}body{{margin:0}}img{{max-width:100%}}</style>
</head>
<body>
{body}
</body>
</html>"""


def _journal() -> Journal:
    return Journal()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/config")
def api_config() -> Any:
    """The frozen limits the Risk Officer enforces."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {
        "underlyings": list(cfg.underlyings),
        "entry_dte": list(cfg.entry_dte),
        "effective_entry_dte": [cfg.min_entry_dte, cfg.max_entry_dte],
        "force_flatten_dte": cfg.force_flatten_dte,
        "max_loss_pct": cfg.max_loss_pct,
        "daily_new_risk_pct": cfg.daily_new_risk_pct,
        "dd_flatten_pct": cfg.dd_flatten_pct,
        "tp_frac_of_credit": cfg.tp_frac_of_credit,
        "defend_mult": cfg.defend_mult,
        "max_lots": cfg.max_lots,
        "max_open_structures": cfg.max_open_structures,
        "min_credit_to_width": cfg.min_credit_to_width,
        "event_window_hours": cfg.event_window_hours,
    }


@app.get("/api/gate")
def api_gate() -> Any:
    """The desk's readiness, as the desk itself last measured it.

    This process is read-only and holds no MCP session, so running the gate here
    would fail every broker-dependent check and report HALTED while the desk is
    live. The desk journals its full gate result at startup; that is the honest
    answer, and it is labelled with when it was taken.
    """
    for entry in reversed(_journal().tail(5000)):
        if entry.get("event") == "STARTUP" and entry.get("checks"):
            return {
                "state": entry.get("state"),
                "passed": bool(entry.get("passed")),
                "checks": entry.get("checks", []),
                "measured_at": entry.get("ts"),
                "source": "desk",
            }

    # No desk run recorded yet: fall back to the checks this process can do
    # alone, and say so rather than implying it speaks for the desk.
    result = run_gate()
    payload = result.as_dict()
    payload["measured_at"] = None
    payload["source"] = "api_process_only"
    return payload


@app.get("/api/journal")
def api_journal(limit: int = 200, event: str | None = None) -> Any:
    """Recent journal entries, newest first."""
    entries = _journal().tail(max(limit * 5, 1000))
    if event:
        entries = [e for e in entries if e.get("event") == event]
    return {"count": _journal().count(), "entries": list(reversed(entries[-limit:]))}


@app.get("/api/summary")
def api_summary() -> Any:
    """Headline numbers for the dashboard."""
    journal = _journal()
    entries = journal.tail(5000)
    # Totals come from the whole journal; the tail is only for the most recent
    # regime, reconcile and refusal detail.
    counts = Counter(journal.event_counts())

    latest_regime = next(
        (e for e in reversed(entries) if e.get("event") == "REGIME"), None
    )
    latest_reconcile = next(
        (e for e in reversed(entries) if e.get("event") == "RECONCILE"), None
    )
    denials = [e for e in entries if e.get("event") == "DENY"]

    deny_reasons = Counter()
    for entry in denials:
        for reason in entry.get("reasons", []) or []:
            deny_reasons[str(reason).split(":")[0]] += 1

    fills = [e for e in entries if e.get("event") == "FILL"]

    # P&L is the first judging criterion, so it is stated outright rather than
    # left for the reader to subtract from equity.
    equity = (latest_reconcile or {}).get("equity")
    equity = float(equity) if equity is not None else None
    starting = journal.first_equity()
    start_of_day = (latest_reconcile or {}).get("start_of_day_equity")
    start_of_day = float(start_of_day) if start_of_day is not None else None

    pnl_total = (equity - starting) if (equity is not None and starting) else None
    pnl_today = (equity - start_of_day) if (equity is not None and start_of_day) else None

    # Split the total, or the page shows -95 beside -56 with nothing joining
    # them. Realised comes from matched fills; the remainder is the mark on
    # what is still open, plus whatever the broker took in fees.
    closed_trades = [t for t in _pair_fills(entries) if not t["open"] and t["realized"] is not None]
    pnl_realized = round(sum(t["realized"] for t in closed_trades), 2) if closed_trades else 0.0
    pnl_unrealized = round(pnl_total - pnl_realized, 2) if pnl_total is not None else None

    return {
        "events": dict(counts),
        "total_events": journal.count(),
        "regime": (latest_regime or {}).get("permission", {}).get("regime"),
        "regime_reasons": (latest_regime or {}).get("permission", {}).get("reasons", []),
        "equity": (latest_reconcile or {}).get("equity"),
        "high_water_mark": (latest_reconcile or {}).get("high_water_mark"),
        "drawdown": (latest_reconcile or {}).get("drawdown"),
        "open_structures": (latest_reconcile or {}).get("open_structures"),
        "starting_equity": starting,
        "start_of_day_equity": start_of_day,
        "pnl_total": pnl_total,
        "pnl_total_pct": (pnl_total / starting) if (starting and pnl_total is not None) else None,
        "pnl_today": pnl_today,
        "pnl_realized": pnl_realized,
        "pnl_unrealized": pnl_unrealized,
        "closed_trades": len(closed_trades),
        "allows": counts.get("ALLOW", 0),
        "denies": counts.get("DENY", 0),
        "fills": len(fills),
        "top_deny_reasons": deny_reasons.most_common(8),
        "last_updated": entries[-1]["ts"] if entries else None,
    }


def describe_legs(symbols: list, structure: str) -> list[dict]:
    """Turn OCC symbols into the buy and sell orders a reader can follow.

    Side is derived from the structure's geometry rather than stored, so this
    also works for trades already in the journal. Both permitted structures have
    exactly one arrangement:

      put credit spread - sell the higher strike, buy the lower as protection
      iron condor       - buy the lowest put, sell the next; sell the lower
                          call, buy the highest as protection

    That is the same geometry structures.py validates before a ticket is built,
    so it cannot disagree with what was actually sent.
    """
    from .broker.occ import OCCError, parse_option_symbol

    parsed = []
    for symbol in symbols or []:
        try:
            contract = parse_option_symbol(str(symbol))
        except OCCError:
            continue
        parsed.append({
            "symbol": str(symbol),
            "strike": float(contract.strike),
            "right": contract.right,
            "side": None,
        })

    puts = sorted([l for l in parsed if l["right"] == "P"], key=lambda l: l["strike"])
    calls = sorted([l for l in parsed if l["right"] == "C"], key=lambda l: l["strike"])

    if structure == "iron_condor" and len(puts) == 2 and len(calls) == 2:
        puts[0]["side"], puts[1]["side"] = "buy", "sell"
        calls[0]["side"], calls[1]["side"] = "sell", "buy"
    elif len(puts) == 2 and not calls:
        puts[0]["side"], puts[1]["side"] = "buy", "sell"
    elif len(calls) == 2 and not puts:
        calls[0]["side"], calls[1]["side"] = "sell", "buy"

    # Short legs first: they are the position, the longs are the protection.
    order = {"sell": 0, "buy": 1, None: 2}
    return sorted(puts + calls, key=lambda l: (order[l["side"]], l["strike"]))


def _signed_credit(price: Any) -> float | None:
    """Credit received on an opening fill, as a positive number.

    Alpaca reports a multi-leg fill price signed by cash flow, so a credit
    structure opens at a negative price. Returns the magnitude the desk actually
    collected.
    """
    if price is None:
        return None
    return abs(float(price))


def _signed_debit(price: Any) -> float | None:
    """Cost paid on a closing fill, as a positive number."""
    if price is None:
        return None
    return abs(float(price))


@app.get("/api/decision")
def api_decision() -> Any:
    """The most recent complete pass, as one traceable decision.

    The journal is a flat stream; a judge reading it has to reassemble which
    candidate the model chose and what the Risk Officer then did about it. This
    walks back to the last REGIME entry and returns everything that followed as
    a single chain, so the pipeline can be shown end to end.
    """
    entries = _journal().tail(400)

    start = None
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("event") == "REGIME":
            start = i
            break
    if start is None:
        return {"available": False}

    chain = entries[start:]
    by_event: dict[str, dict] = {}
    for entry in chain:
        by_event.setdefault(str(entry.get("event")), entry)

    regime = by_event.get("REGIME", {})
    candidates = by_event.get("CANDIDATES", {})
    rank = by_event.get("RANK", {})
    verdict = by_event.get("ALLOW") or by_event.get("DENY") or {}
    submit = by_event.get("SUBMIT", {})

    permission = regime.get("permission", {}) or {}
    signals = regime.get("signals", {}) or {}

    shown = candidates.get("tickets", []) or []
    picked_id = rank.get("pick_id")
    picked = next((t for t in shown if t.get("candidate_id") == picked_id), None)

    return {
        "available": True,
        "at": regime.get("ts"),
        "regime": {
            "name": permission.get("regime"),
            "max_lots": permission.get("max_lots"),
            "allowed": permission.get("allowed_strategies", []),
            "reasons": permission.get("reasons", []),
        },
        "signals": {
            "implied_vol": signals.get("implied_vol"),
            "realized_vol": signals.get("realized_vol"),
            "iv_rank": signals.get("iv_rank"),
            "adx": signals.get("adx"),
            "hours_to_next_event": signals.get("hours_to_next_event"),
            "next_event_name": signals.get("next_event_name"),
        },
        # Exactly the payload the model received: no equity, no buying power,
        # no risk budget, no rules. Shown so that can be checked, not asserted.
        "shortlist": shown,
        "model": {
            "pick_id": picked_id,
            "rationale": rank.get("rationale"),
            "fallback": rank.get("fallback"),
            "reason": rank.get("reason"),
            "model": rank.get("model"),
            "latency_ms": rank.get("latency_ms"),
            "picked": picked,
        },
        "officer": {
            "decision": verdict.get("decision") or verdict.get("event"),
            "allowed_lots": verdict.get("allowed_lots"),
            "reasons": verdict.get("reasons", []),
            "ticket_id": verdict.get("ticket_id"),
        },
        "submitted": bool(submit),
        "order": {
            "limit_price": submit.get("limit_price"),
            "wire_limit_price": submit.get("wire_limit_price"),
            "lots": submit.get("lots"),
            "client_order_id": submit.get("client_order_id"),
        } if submit else None,
    }


@app.get("/api/risk")
def api_risk() -> Any:
    """Live readings against each frozen limit, so the governor can be seen working."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    recent = _journal().tail(2000)
    latest = next((e for e in reversed(recent) if e.get("event") == "RECONCILE"), {})
    # The worst case the officer last permitted, so the cap has a reading against
    # it rather than an empty bar.
    last_open = next(
        (e for e in reversed(recent)
         if e.get("event") == "SUBMIT" and e.get("intent") == "open"
         and e.get("max_loss") is not None),
        {},
    )
    equity = latest.get("equity")
    equity = float(equity) if equity is not None else None
    sod = latest.get("start_of_day_equity")
    sod = float(sod) if sod is not None else equity

    def gauge(name, used, limit, unit):
        pct = (used / limit) if (limit and used is not None) else None
        return {
            "name": name, "used": used, "limit": limit, "unit": unit,
            "pct": pct, "breached": bool(pct is not None and pct >= 1.0),
        }

    return {
        "gauges": [
            gauge("Daily new risk", latest.get("day_open_risk"),
                  round(cfg.daily_new_risk_pct * sod, 2) if sod else None, "$"),
            gauge("Drawdown", latest.get("drawdown"), cfg.dd_flatten_pct, "%"),
            gauge("Open structures", latest.get("open_structures"),
                  cfg.max_open_structures, ""),
            gauge("Worst case, last position", last_open.get("max_loss"),
                  round(cfg.max_loss_pct * equity, 2) if equity else None, "$"),
        ],
        "equity": equity,
    }


def _pair_fills(entries: list) -> list[dict]:
    """Match opening fills to closing fills, on underlying and expiry.

    Shared by the trades listing and the summary so the realised figure on the
    page cannot disagree with the trades it is derived from.
    """
    fills = [e for e in entries if e.get("event") == "FILL"]

    books: dict[tuple[str, str], dict] = {}
    for fill in fills:
        key = (str(fill.get("underlying") or ""), str(fill.get("expiry") or ""))
        book = books.setdefault(key, {"opens": [], "closes": []})
        book["closes" if fill.get("intent") == "close" else "opens"].append(fill)

    CONTRACT = 100
    trades = []
    for (underlying, expiry), book in books.items():
        opens, closes = book["opens"], book["closes"]
        if not opens:
            continue
        entry = opens[0]
        lots = entry.get("filled_qty") or 1
        exit_ = closes[0] if closes else None

        credit = _signed_credit(entry.get("filled_avg_price"))
        cost = _signed_debit(exit_.get("filled_avg_price")) if exit_ else None
        realized = None
        if credit is not None and cost is not None:
            realized = round((credit - cost) * CONTRACT * lots, 2)

        trades.append({
            "underlying": underlying,
            "expiry": expiry,
            "structure": entry.get("structure"),
            "lots": lots,
            "opened_at": entry.get("filled_at") or entry.get("ts"),
            "closed_at": (exit_ or {}).get("filled_at") or (exit_ or {}).get("ts"),
            "entry_credit": credit,
            "exit_cost": cost,
            "realized": realized,
            "open": exit_ is None,
            "legs": describe_legs(entry.get("legs") or [], entry.get("structure")),
        })

    trades.sort(key=lambda t: t.get("opened_at") or "", reverse=True)
    return trades


@app.get("/api/trades")
def api_trades() -> Any:
    """Every structure the desk traded, paired open-to-close.

    Built from FILL entries, so the prices are the broker's own fills rather
    than the limits the desk asked for. Opens and closes are matched on
    underlying and expiry, which is what identifies a structure across its life
    -- the ticket id is content-derived at entry and does not survive the roll
    into a close.
    """
    trades = _pair_fills(_journal().tail(6000))
    closed = [t for t in trades if not t["open"] and t["realized"] is not None]
    wins = [t for t in closed if t["realized"] > 0]
    return {
        "trades": trades,
        "open_count": sum(1 for t in trades if t["open"]),
        "closed_count": len(closed),
        "realized_total": round(sum(t["realized"] for t in closed), 2) if closed else 0.0,
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
    }


#: Closed trades needed in a bucket before its win rate says anything.
#:
#: A credit spread wins roughly 70% of the time by construction, so telling a
#: real edge from that baseline is a proportion test. Detecting a 15-point
#: improvement at 95% confidence with 80% power needs on the order of 120
#: outcomes per arm. Anything less is noise wearing a percentage sign.
EVIDENCE_THRESHOLD = 120


def _entry_conditions(entries: list) -> dict[str, dict]:
    """What the market looked like when each structure was opened.

    Joined by expiry: a SUBMIT names the ticket, the CANDIDATES entry just
    before it carries that ticket's delta and width, and the REGIME entry before
    that carries the volatility readings. All three belong to the same pass.
    """
    conditions: dict[str, dict] = {}
    regime: dict = {}
    shortlist: dict = {}

    for entry in entries:
        event = entry.get("event")
        if event == "REGIME":
            regime = entry
        elif event == "CANDIDATES":
            shortlist = entry
        elif event == "SUBMIT" and entry.get("intent") == "open":
            ticket_id = entry.get("ticket_id")
            picked = next(
                (t for t in (shortlist.get("tickets") or [])
                 if t.get("candidate_id") == ticket_id),
                {},
            )
            expiry = picked.get("expiry")
            if not expiry:
                continue
            signals = regime.get("signals") or {}
            iv, rv = signals.get("implied_vol"), signals.get("realized_vol")
            conditions[expiry] = {
                "short_delta": picked.get("short_delta"),
                "width": picked.get("width"),
                "regime": (regime.get("permission") or {}).get("regime"),
                "iv_rv_ratio": round(iv / rv, 3) if (iv and rv) else None,
            }
    return conditions


def _bucket(trades: list, key: str, label) -> list[dict]:
    groups: dict = {}
    for t in trades:
        value = t.get(key)
        if value is None:
            continue
        groups.setdefault(label(value), []).append(t)

    out = []
    for name, rows in sorted(groups.items()):
        wins = [r for r in rows if r["realized"] > 0]
        out.append({
            "bucket": name,
            "n": len(rows),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(rows), 3) if rows else None,
            "total": round(sum(r["realized"] for r in rows), 2),
            "mean": round(sum(r["realized"] for r in rows) / len(rows), 2) if rows else None,
            "conclusive": len(rows) >= EVIDENCE_THRESHOLD,
        })
    return out


@app.get("/api/learning")
def api_learning() -> Any:
    """What the desk's own record does, and does not, support concluding.

    Deliberately does NOT feed back into trading. With a handful of closed
    trades, any parameter fitted to this would be fitted to noise -- and a desk
    whose entire argument is that it refuses to act without evidence would be
    contradicting itself in its own strategy. It measures, states what it would
    take to know something, and waits.
    """
    entries = _journal().tail(12000)
    conditions = _entry_conditions(entries)

    closed = [
        t for t in _pair_fills(entries)
        if not t["open"] and t["realized"] is not None
    ]
    for t in closed:
        t.update(conditions.get(t["expiry"], {}))

    wins = [t for t in closed if t["realized"] > 0]
    n = len(closed)

    return {
        "closed_trades": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n, 3) if n else None,
        "realized_total": round(sum(t["realized"] for t in closed), 2) if closed else 0.0,
        "threshold": EVIDENCE_THRESHOLD,
        "conclusive": n >= EVIDENCE_THRESHOLD,
        "shortfall": max(0, EVIDENCE_THRESHOLD - n),
        "by_delta": _bucket(closed, "short_delta",
                            lambda v: f"{round(float(v), 2):.2f} delta"),
        "by_width": _bucket(closed, "width", lambda v: f"{float(v):.0f} wide"),
        "by_regime": _bucket(closed, "regime", lambda v: str(v)),
        "trades": [
            {
                "expiry": t["expiry"],
                "realized": t["realized"],
                "short_delta": t.get("short_delta"),
                "width": t.get("width"),
                "regime": t.get("regime"),
                "iv_rv_ratio": t.get("iv_rv_ratio"),
            }
            for t in closed
        ],
    }


@app.get("/api/equity")
def api_equity(limit: int = 240) -> Any:
    """Reconciled equity over time, for the dashboard sparkline.

    Points come from RECONCILE entries, which are the desk's own reads of the
    broker rather than anything it inferred.
    """
    points = []
    for entry in _journal().tail(6000):
        if entry.get("event") != "RECONCILE":
            continue
        equity = entry.get("equity")
        if equity is None:
            continue
        points.append({"ts": entry.get("ts"), "equity": float(equity)})
    points = points[-limit:]
    values = [p["equity"] for p in points]
    return {
        "points": points,
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
        "low": min(values) if values else None,
        "high": max(values) if values else None,
        "change": (values[-1] - values[0]) if len(values) > 1 else 0.0,
    }


@app.get("/api/lifecycle")
def api_lifecycle(limit: int = 40) -> Any:
    """Recent lifecycle actions, so the dashboard can show management, not only entry."""
    wanted = {"TAKE_PROFIT", "DEFEND", "ROLL", "FLATTEN", "EXPIRE"}
    out = [e for e in _journal().tail(4000) if e.get("event") in wanted]
    return {"entries": list(reversed(out[-limit:]))}


@app.get("/deck", response_class=HTMLResponse)
def deck() -> HTMLResponse:
    """The submission slide deck, on the same origin as the live demo."""
    if not DECK.exists():
        return HTMLResponse("<h1>Deck not found</h1>", status_code=404)
    return HTMLResponse(DECK_SKELETON.format(body=DECK.read_text(encoding="utf-8")))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    if not DASHBOARD.exists():
        return HTMLResponse("<h1>OptionGenome</h1><p>Dashboard asset missing.</p>", status_code=500)
    return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))
