# OptionGenome

**An autonomous options desk where the model is the least-trusted component.**

Live: [optiongenome.duckdns.org](https://optiongenome.duckdns.org) · Alpaca paper account `PA3Y88DE6VC4`

---

## The idea

Most trading agents ask *what should I trade?* OptionGenome asks two different questions first: **what kind of market is this**, and **what happens after a position is open?**

That splits the system in two. **MarketDNA** decides what is *legal* right now. **Roll Desk** decides what to trade and manages it to exit. Between them sits a **Risk Officer** that is the only thing in the system allowed to commit capital — and it trusts nothing it is handed.

## AI logic

An open-weight model (Qwen-2.5-72B via **Featherless**) ranks candidates. That is its entire job.

It receives a shortlist that deterministic code has already built and structurally validated. It returns one thing:

```json
{ "pick_id": "iro-efb7d21c92", "rationale": "one sentence" }
```

It cannot create or change a strike, an expiry, a size, or a limit price. It cannot call a broker tool. It never sees account equity, buying power, the daily risk budget, or the risk rules — it doesn't need them, so it isn't given them.

Its output is parsed as hostile input: fences stripped, shape checked, and `pick_id` verified to belong to the supplied set. Extra keys are ignored, not obeyed — a response containing `"lots": 500` or `"override_risk_officer": true` changes nothing. Any failure (bad JSON, unknown id, timeout, transport error) falls back to the first legal candidate and is journalled with `fallback=true`.

That path is covered by 35 adversarial tests, and it proved itself in production: when `httpx` was missing on the server, the ranker logged `transport_error:ModuleNotFoundError` and the desk kept trading.

**MarketDNA is deliberately model-free.** Regime classification is arithmetic — EMA20/50, Wilder's ADX(14), 20-day realized volatility against its 100-day median, IV rank, and a data-driven economic calendar. Evaluation order is fixed and the first match wins: EVENT → MOMENTUM → COMPRESSION → INCOME. Its only output is a permission set, never an order.

## Risk gates

The **Risk Officer** is pure: no network, no filesystem, no database, no clock read. Same inputs, same verdict, always. Fifteen checks run on every ticket; none short-circuits, so a refusal reports every reason at once. Any exception is a DENY.

Its defining property is that **it does not trust the ticket**. Width, credit, maximum loss, DTE, structure type and every leg relationship are recalculated from the OCC symbols and live quotes. A ticket claiming `max_loss: 1.0` does not get to buy itself past the cap — the officer derives the real number and refuses.

Frozen limits: max loss ≤ 0.75% of equity, new risk ≤ 2%/day, 5% drawdown triggers flatten-only, credit/width ≥ 0.15, quotes < 5s old, one lot maximum, three open structures maximum, no overlapping short exposure on the same or adjacent expiry, and no entry inside the final 15 minutes of the session.

Only two structures are permitted, by allow-list: **put credit spreads** and **iron condors**. Both are defined-risk. Anything else — naked, straddle, strangle, ratio, calendar — is refused on leg geometry, not on its label.

Sizing is derived, never proposed. The model has no input; the ticket's own `proposed_lots` is ignored.

After entry, Roll Desk manages each position: take profit at 50% of credit, stop at 2× credit, roll before expiry, and force-flatten at 1 DTE. Safety exits outrank profit exits — a winner inside the flatten zone is still closed. A roll re-enters the Risk Officer as a brand-new request; if the replacement fails, the existing position is closed instead.

## Alpaca infrastructure

A hard boundary, enforced by a test that walks the source tree and fails the build if anything crosses it:

- **All reads via the MCP server.** 72 tools discovered at runtime and mapped to seven internal capabilities. No tool name is hardcoded.
- **All writes via the Alpaca CLI.** `order_class=mleg`, always limit, always day. Never a market order on a spread.

Every flag is transcribed from the installed binary's captured output, never from memory. The `--legs` wire format is undocumented in that output, so submission stayed locked until `--dry-run` proved the encoding against the real binary.

A 20-check startup gate runs before the loop: paper-endpoint assertion, live-flag rejection, account identity, options level, MCP capability verification, CLI authentication, Risk Officer self-test. Nineteen passes is not READY.

## What running it actually taught us

Tests passed; reality disagreed. Three examples:

- Capability discovery mapped the **read** `get_open_orders` onto the **write** `place_stock_order` — "order" is a substring of both. Reading the order book would have submitted an order.
- The drawdown high-water mark was seeded from *current* equity, so drawdown was zero by definition. An account down 6% looked healthy.
- Every trade was denied over **49 milliseconds** of clock skew between Alpaca's servers and ours.

Each is now a named constant with a regression test explaining why it exists.

## Evidence

Everything is in an append-only, fsynced JSONL journal: every regime call, candidate set, model rationale, ALLOW, DENY-with-reasons, submission and fill.

On day one the desk made **133 decisions and refused 130 of them** — stale quotes, overlapping exposure, adjacent expiry, final-session window, market closed. Nobody stopped it. The Risk Officer did, and wrote down why.

**614 tests. 5,256 lines of source, 4,435 lines of tests.** Paper trading only; simulated funds, real market data.
