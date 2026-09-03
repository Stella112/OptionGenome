# Submission fields — copy and paste these

Everything below is checked against the running system as of 2026-09-03.

---

## Project title

```
OptionGenome
```

## Short description

```
An autonomous defined-risk options desk on Alpaca paper trading, where the
model is the least-trusted component. MarketDNA classifies the market and
decides what is legal. Roll Desk builds SPY credit spreads and iron condors
and manages each one to exit. A deterministic Risk Officer recalculates every
number it is handed and is the only thing permitted to commit capital. The
model ranks a shortlist it cannot write, and code can overrule it.
```

## Long description

```
Most trading agents ask one question: what should I trade?

OptionGenome asks two others first. What kind of market is this right now, and
what happens after a position is open?

MARKETDNA — PERMISSIONS, NOT ORDERS

MarketDNA classifies the tape from arithmetic, never from a model: EMA20/50,
Wilder's ADX(14), 20-day realized volatility against its 100-day median, IV
rank, and a data-driven calendar of scheduled economic events. Evaluation
order is fixed and the first match wins — EVENT, MOMENTUM, COMPRESSION,
INCOME.

Its only output is a permission set: which structures are legal right now and
how many lots. It never selects a strike, chooses an expiry, sizes anything,
or submits an order.

One more gate sits alongside it. Selling premium pays when implied volatility
exceeds what the underlying goes on to realise. The desk measures both and
withholds permission entirely when implied is not at least 5% above realized —
the one condition under which this strategy has no edge to begin with.

ROLL DESK — ENTRY THROUGH EXIT

When MarketDNA permits, Roll Desk screens the live SPY option chain — around
2,000 contracts inside the entry window — and builds fully specified
defined-risk candidates: put credit spreads and iron condors, five and ten
points wide, with short strikes near 0.16 to 0.28 delta.

Candidates are ranked on credit net of their own round-trip bid-ask cost, per
dollar of risk. Both terms are measured from live quotes; neither is assumed.

An open-weight model (Qwen-2.5-72B via Featherless) then picks one and writes
a single sentence explaining why. That is the entire extent of its authority.
It cannot create or change a strike, an expiry, a size or a limit price. It
cannot call a broker tool. It never sees account equity, buying power, the
daily risk budget or the risk rules. Its reply is parsed as hostile input:
fences stripped, shape checked, and the chosen id verified to belong to the
shortlist it was given. Extra keys are ignored rather than obeyed — a response
containing "lots": 500 or "override_risk_officer": true changes nothing. Any
failure falls back to the first legal candidate and is journalled.

THE RISK OFFICER — THE LAST WORD ON CAPITAL

Fifteen deterministic checks, pure: no network, no filesystem, no clock read.
Same inputs, same verdict, always. Every check runs — failures accumulate
rather than short-circuit — so a refusal reports every reason at once. Any
exception is a DENY.

Its defining property is that it does not trust the ticket. Width, credit,
maximum loss, DTE, structure type and every leg relationship are recalculated
from the OCC symbols and live quotes. A ticket claiming max_loss: 1.0 does not
get to buy itself past the cap.

Limits: max loss at most 0.75% of equity, new risk at most 2% per day, a 5%
drawdown triggers flatten-only, credit-to-width at least 0.15, quotes under
five seconds old, one lot maximum, three open structures maximum, no
overlapping short exposure, and no entry inside the final fifteen minutes of
the session.

Sizing is derived, never proposed. No ALLOW means no broker command is ever
constructed.

LIFECYCLE

Opening a trade is not the end of it. Every open structure is re-evaluated
each pass: take profit at 50% of credit, stop at twice the credit, and a
mandatory flatten at 1 DTE — nothing is carried into settlement. Safety exits
outrank profit exits, so a winner inside the flatten zone is still closed.

ALPACA INFRASTRUCTURE

A hard boundary, enforced by a test that walks the source tree and fails the
build if anything crosses it. All reads go through the MCP server: 72 tools
discovered at runtime and mapped to seven internal capabilities, with no tool
name hardcoded. All writes go through the Alpaca CLI: order_class mleg, always
limit, always day, never a market order on a spread.

Every flag is transcribed from the installed binary's captured output rather
than from memory. The --legs wire format is undocumented there, so submission
stayed locked until --dry-run proved the encoding against the real binary.

A twenty-check startup gate runs before the loop begins. Nineteen passes is
not READY.

WHAT RUNNING IT TAUGHT US

Tests passed; reality disagreed. Capability discovery mapped the READ
get_open_orders onto the WRITE place_stock_order, because "order" is a
substring of both — reading the order book would have placed an order. The
drawdown high-water mark was seeded from current equity, making drawdown zero
by definition. Every trade was denied over 49 milliseconds of clock skew. And
opening orders were sent with a positive limit price, which Alpaca reads as a
debit cap, so every entry filled below the limit we believed we had set.

Each is now a named constant with a regression test that explains why it
exists. None was found by unit tests; all of them needed a live broker.

EVIDENCE

Everything lands in an append-only, fsynced JSONL journal: every regime call,
candidate set, model rationale, ALLOW, DENY with its reasons, submission and
fill. The dashboard shows the last decision end to end — what the model chose,
in its own words, and what the Risk Officer then did about it.

At the time of writing the desk has made 1,871 decisions and refused 1,866 of
them. Nobody stopped it. The Risk Officer did, and wrote down why.

Paper trading only. Simulated funds, real market data.
```

## Technology tags

```
Alpaca, Featherless, Python, MCP, options trading, algorithmic trading
```

## Links

| Field | Value |
|---|---|
| Application URL | `https://optiongenome.duckdns.org` |
| Slide presentation | `https://optiongenome.duckdns.org/deck` |
| GitHub repository | `https://github.com/Stella112/OptionGenome` |
| Alpaca paper account ID | `PA3Y88DE6VC4` |
| Cover image | `docs/cover.png` in the repo |

---

## Note on the numbers

The refusal counts above move every minute the desk runs. Check
`https://optiongenome.duckdns.org/api/summary` before pasting, and update
`denies` and `allows` if they have drifted. Everything else is stable.
