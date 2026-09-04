# OptionGenome — one-page write-up

**An autonomous options desk where the model is the least-trusted component.**

Live: [optiongenome.duckdns.org](https://optiongenome.duckdns.org) · Deck: [/deck](https://optiongenome.duckdns.org/deck) · Alpaca paper account `PA3Y88DE6VC4`

---

Most trading agents ask a language model what to trade and then do it. The model always
answers — it never says *nothing looks good right now* — and in options selling, refusing is
most of the job. OptionGenome inverts that: deterministic code decides everything that
matters, and the model ranks a shortlist it cannot write.

**2,421 decisions. 2,415 refused. 6 traded.** That ratio is the product.

## AI logic

Qwen-2.5-72B via **Featherless** ranks pre-built candidates and writes one sentence of
justification. That is its entire authority:

```json
{ "pick_id": "iro-efb7d21c92", "rationale": "one sentence" }
```

It cannot create or change a strike, expiry, size or limit price, cannot call a broker tool,
and never sees equity, buying power, the risk budget or the rules. Its reply is parsed as
hostile input: fences stripped, shape checked, `pick_id` verified against the supplied set.
Extra keys are ignored rather than obeyed — `"lots": 500` or `"override_risk_officer": true`
changes nothing. Any failure falls back to the first legal candidate and is journalled. 35
adversarial tests cover this path, and it held in production: when `httpx` was missing on the
server the ranker logged `transport_error` and the desk kept trading.

**Everything else is model-free.** MarketDNA classifies the regime from arithmetic — EMA20/50,
Wilder's ADX(14), 20-day realized vol against its 100-day median, IV rank, an economic
calendar — and emits only a permission set. Candidates are ranked on credit net of round-trip
bid-ask cost per dollar of risk, both measured from live quotes. One further gate: the desk
withholds permission entirely unless implied volatility is at least 5% above realized, the one
condition under which selling premium has no edge at all.

## Risk gates

The **Risk Officer** is pure — no network, no filesystem, no clock read. Fifteen checks run on
every ticket; none short-circuits, so a refusal reports every reason at once. Any exception is
a DENY.

Its defining property is that **it does not trust the ticket.** Width, credit, max loss, DTE,
structure type and every leg relationship are recalculated from the OCC symbols and live
quotes. A ticket claiming `max_loss: 1.0` on a $400 spread does not get to buy itself past the
cap.

Frozen limits: max loss ≤ 0.75% of equity, new risk ≤ 2%/day, 5% drawdown triggers
flatten-only, credit/width ≥ 0.15, quotes < 5s old, one lot, three open structures, no
overlapping short exposure, no entry in the final 15 minutes.

Two structures only, by allow-list: **put credit spreads** and **iron condors**, both
defined-risk. Naked, straddle, strangle, ratio and calendar are refused on leg *geometry*, not
on label. Sizing is derived, never proposed.

After entry: take profit at 50% of credit, stop at 2× credit, mandatory flatten at 1 DTE.
Safety exits outrank profit exits. A short strike merely being touched is recorded but
deliberately not acted on — the long wing already caps the loss, and closing on first touch
converts every recoverable position into a certain one.

## Alpaca infrastructure

A hard boundary, enforced by a test that walks the source tree and fails the build if anything
crosses it:

- **All reads via the MCP server.** 72 tools discovered at runtime, mapped to seven internal
  capabilities. No tool name hardcoded; a missing capability halts at startup, not at trading
  time.
- **All writes via the Alpaca CLI.** `order_class=mleg`, always limit, always day. Never a
  market order on a spread.

Every flag is transcribed from the installed binary's captured output, never from memory. The
`--legs` wire format is undocumented, so submission stayed locked until `--dry-run` proved the
encoding against the real binary. A 20-check startup gate runs before the loop: paper-endpoint
assertion, live-flag rejection, account identity, MCP capability verification, CLI
authentication, OCC self-test. Nineteen passes is not READY.

## What running it live caught

Every one of these passed a green test suite and needed a real broker to surface.

- Capability discovery mapped the **read** `get_open_orders` onto the **write**
  `place_stock_order` — "order" is a substring of both. Reading the order book would have
  **placed an order**.
- Opening orders were sent with a **positive** limit price. Alpaca reads that as a debit cap,
  so every entry filled below the limit we believed we had set.
- The drawdown high-water mark was seeded from *current* equity, so drawdown was zero by
  definition and the 5% breaker could never fire.
- Every trade denied over **49 milliseconds** of clock skew.
- The lifecycle layer was handed an empty list, so take-profit, the stop and the forced flatten
  had never once run in production — while 39 tests covering them passed.
- Fills were re-journalled once they aged out of a 4,000-entry dedup window, so the summary
  reported **+$109** realized while the trades table it summarised showed **−$56**.
- Trade pairing took `opens[0]`/`closes[0]` per expiry, so a re-entered expiry vanished from
  the trades table while the reconciler still counted it.

Each is now a named constant with a regression test explaining why it exists.

## Evidence

Everything lands in an append-only, fsynced JSONL journal: every regime call, candidate set,
model rationale, ALLOW, DENY-with-reasons, submission and fill. The dashboard replays the last
decision end to end — what the model chose, in its own words, and what the Risk Officer then
did about it. Nobody stopped those 2,415 trades. The Risk Officer did, and wrote down why.

P&L is **−$14.04 on $100,000** — realized −$56.00, unrealized +$41.96, across six trades at one
lot each. The three figures reconcile exactly because they derive from the same deduplicated
fill history; making them reconcile is what surfaced the last two defects above. Roughly $48 of
the realized loss came from a rule of mine that closed positions on first touch, since
reverted, and it is on the dashboard with what it cost.

**696 tests. 6,346 lines of source, 5,441 lines of tests.** Paper trading only; simulated
funds, real market data. Paper-trading results are hypothetical and do not represent actual
trading. Not investment advice.
