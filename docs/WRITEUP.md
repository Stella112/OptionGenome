# OptionGenome — one-page write-up

**An autonomous options desk where the model is the least-trusted component.**

Live: [optiongenome.duckdns.org](https://optiongenome.duckdns.org) · Deck: [/deck](https://optiongenome.duckdns.org/deck) · Alpaca paper account `PA3Y88DE6VC4`

*Covers the three required topics: AI logic, risk gates, and Alpaca infrastructure implementation.*

---

## The idea

Most trading agents ask *what should I trade?* OptionGenome asks two different questions first: **what kind of market is this**, and **what happens after a position is open?**

That splits the system in two. **MarketDNA** decides what is *legal* right now. **Roll Desk** decides what to trade and manages it to exit. Between them sits a **Risk Officer** that is the only thing allowed to commit capital — and it trusts nothing it is handed.

## AI logic

An open-weight model (Qwen-2.5-72B via **Featherless**) ranks candidates and writes one sentence explaining its choice. That is the entire extent of its authority.

It receives a shortlist deterministic code has already built and structurally validated, and returns one thing:

```json
{ "pick_id": "iro-efb7d21c92", "rationale": "one sentence" }
```

It cannot create or change a strike, an expiry, a size or a limit price. It cannot call a broker tool. It never sees account equity, buying power, the daily risk budget or the risk rules — it doesn't need them, so it isn't given them. The dashboard shows that exclusion list struck through on every pass, so the claim is checkable rather than asserted.

Its reply is parsed as hostile input: fences stripped, shape checked, and `pick_id` verified to belong to the supplied set. Extra keys are ignored, not obeyed — a response carrying `"lots": 500` or `"override_risk_officer": true` changes nothing. Any failure — bad JSON, unknown id, timeout, transport error — falls back to the first legal candidate and is journalled with `fallback=true`.

That path has 35 adversarial tests, and it proved itself in production: when `httpx` was missing on the server, the ranker logged `transport_error:ModuleNotFoundError` and the desk kept trading.

**Candidate selection is model-free.** Structures are ranked on credit net of their own round-trip bid-ask cost, per dollar of risk — both terms measured from live quotes, neither assumed. An earlier version weighted credit by a delta-implied probability of profit; that is the risk-neutral measure, under which every option prices to roughly zero expectancy by construction, so the score collapsed to "prefer the highest win rate" and steered toward thin structures whose spread cost exceeded their premium.

**MarketDNA is model-free too.** Regime classification is arithmetic: EMA20/50, Wilder's ADX(14), 20-day realized volatility against its 100-day median, IV rank, and a data-driven economic calendar. Evaluation order is fixed and the first match wins: EVENT → MOMENTUM → COMPRESSION → INCOME. Its only output is a permission set, never an order.

One further gate sits beside it. Selling premium pays when implied volatility exceeds what the underlying goes on to realise. The desk measures both and withholds permission entirely unless implied is at least 5% above realized — the one condition under which this strategy has no edge at all.

## Risk gates

The **Risk Officer** is pure: no network, no filesystem, no database, no clock read. Same inputs, same verdict, always. Fifteen checks run on every ticket; none short-circuits, so a refusal reports every reason at once. Any exception is a DENY.

Its defining property is that **it does not trust the ticket**. Width, credit, maximum loss, DTE, structure type and every leg relationship are recalculated from the OCC symbols and live quotes. A ticket claiming `max_loss: 1.0` does not get to buy itself past the cap — the officer derives the real number and refuses.

Frozen limits: max loss ≤ 0.75% of equity, new risk ≤ 2%/day, a 5% drawdown triggers flatten-only, credit/width ≥ 0.15, quotes < 5s old, one lot maximum, three open structures maximum, no overlapping short exposure, and no entry inside the final 15 minutes of the session.

Only two structures are permitted, by allow-list: **put credit spreads** and **iron condors**. Both are defined-risk. Anything else — naked, straddle, strangle, ratio, calendar — is refused on leg *geometry*, not on its label.

Sizing is derived, never proposed. The model has no input; the ticket's own `proposed_lots` is ignored.

After entry, Roll Desk manages each structure: take profit at 50% of credit, stop at 2× credit, and a mandatory flatten at 1 DTE — nothing is carried into settlement. Safety exits outrank profit exits, so a winner inside the flatten zone is still closed. A short strike merely being *touched* is recorded but deliberately not acted on: the long wing already caps the loss, and closing on first touch converts every recoverable position into a certain one.

## Alpaca infrastructure

A hard boundary, enforced by a test that walks the source tree and fails the build if anything crosses it:

- **All reads via the MCP server.** 72 tools discovered at runtime and mapped to seven internal capabilities. No tool name is hardcoded; a missing capability halts at startup, not at trading time.
- **All writes via the Alpaca CLI.** `order_class=mleg`, always limit, always day. Never a market order on a spread.

Every flag is transcribed from the installed binary's captured output, never from memory. The `--legs` wire format is undocumented there, so submission stayed locked until `--dry-run` proved the encoding against the real binary.

A 20-check startup gate runs before the loop: paper-endpoint assertion, live-flag rejection, account identity, options level, MCP capability verification, CLI authentication, Risk Officer self-test. Nineteen passes is not READY.

## What running it actually taught us

Tests passed; reality disagreed. Every one of these needed a live broker to find:

- Capability discovery mapped the **read** `get_open_orders` onto the **write** `place_stock_order` — "order" is a substring of both. Reading the order book would have **placed an order**.
- Opening orders were sent with a **positive** limit price. Alpaca reads that as a debit cap — *"a negative value signifies a credit"* — so every entry filled at whatever the market offered, up to 23% below the limit we believed we had set.
- The drawdown high-water mark was seeded from *current* equity, making drawdown zero by definition. An account down 6% looked healthy.
- Every trade was denied over **49 milliseconds** of clock skew between Alpaca's clock and ours.
- The lifecycle layer was handed an empty list, so take-profit, the stop and the forced flatten had never once run in production — while 39 tests covering them passed.

Each is now a named constant with a regression test that explains why it exists.

## Evidence

Everything lands in an append-only, fsynced JSONL journal: every regime call, candidate set, model rationale, ALLOW, DENY-with-reasons, submission and fill. The dashboard replays the last decision end to end — what the model chose, in its own words, and what the Risk Officer then did about it.

The desk has made **1,881 decisions and refused 1,875 of them** — stale quotes, overlapping exposure, adjacent expiry, the final-session window, a closed market. Nobody stopped it. The Risk Officer did, and wrote down why.

P&L is **−$116 on $100,000**, across six trades at one lot each over three days. That is a small negative number and it is the honest one: the strategy has had a handful of trades, and most of what it has cost went to transaction friction that the fixes above address rather than to the strategy being wrong.

**680 tests. 6,107 lines of source, 5,184 lines of tests.** Paper trading only; simulated funds, real market data. Paper-trading results are hypothetical and do not represent actual trading.
