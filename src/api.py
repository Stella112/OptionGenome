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

DASHBOARD = Path(__file__).parent / "dashboard.html"


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
    """Live startup readiness. Shows exactly why the desk is or is not READY."""
    result = run_gate()
    return {
        "state": result.state.value,
        "passed": result.passed,
        "checks": [
            {"index": o.index, "name": o.name, "passed": o.passed, "detail": o.detail}
            for o in result.outcomes
        ],
    }


@app.get("/api/journal")
def api_journal(limit: int = 200, event: str | None = None) -> Any:
    """Recent journal entries, newest first."""
    entries = list(_journal().read())
    if event:
        entries = [e for e in entries if e.get("event") == event]
    return {"count": len(entries), "entries": list(reversed(entries[-limit:]))}


@app.get("/api/summary")
def api_summary() -> Any:
    """Headline numbers for the dashboard."""
    entries = list(_journal().read())
    counts = Counter(e.get("event") for e in entries)

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

    return {
        "events": dict(counts),
        "total_events": len(entries),
        "regime": (latest_regime or {}).get("permission", {}).get("regime"),
        "regime_reasons": (latest_regime or {}).get("permission", {}).get("reasons", []),
        "equity": (latest_reconcile or {}).get("equity"),
        "high_water_mark": (latest_reconcile or {}).get("high_water_mark"),
        "drawdown": (latest_reconcile or {}).get("drawdown"),
        "open_structures": (latest_reconcile or {}).get("open_structures"),
        "allows": counts.get("ALLOW", 0),
        "denies": counts.get("DENY", 0),
        "fills": len(fills),
        "top_deny_reasons": deny_reasons.most_common(8),
        "last_updated": entries[-1]["ts"] if entries else None,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    if not DASHBOARD.exists():
        return HTMLResponse("<h1>OptionGenome</h1><p>Dashboard asset missing.</p>", status_code=500)
    return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))
