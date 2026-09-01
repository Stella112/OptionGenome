# OptionGenome

An autonomous defined-risk options desk running against **Alpaca paper trading only**.

Two strictly separated layers, one-way authority:

```
MarketDNA -> Permission set -> Roll Desk candidate builder -> Structural validation
  -> Featherless ranking -> Candidate revalidation -> Risk Officer -> Alpaca CLI
  -> Reconciliation -> Lifecycle manager -> Journal
```

No reverse authority is permitted. MarketDNA decides what is *legal*; Roll Desk
decides *what and when* within that permission; the Risk Officer decides whether
capital moves at all.

## Strategy, in one sentence

Open 1-lot defined-risk put credit spreads and iron condors on SPY at 2-7 DTE
during INCOME and COMPRESSION regimes, take profit at 50% of credit, and
mandatorily flatten at or below 1 DTE rather than carry a position into
expiration or settlement.

The effective entry window is **2-7 DTE**: `entry_dte` is `[1, 7]` and
`force_flatten_dte` is `1`, and the Risk Officer enforces both `entry_dte[0] <=
dte <= entry_dte[1]` **and** `dte > force_flatten_dte`.

## Current status

The deterministic layers are built and tested. The broker layers are gated.

| Layer | Status |
| --- | --- |
| OCC symbols, ratio reduction, structure validation | Built, tested |
| MarketDNA: indicators, calendar, regime, permissions | Built, tested |
| Risk Officer: 15 checks, lot sizing | Built, tested |
| Frozen config, event calendar, append-only journal | Built, tested |
| Paper-only invariant, dev/judge isolation | Built, tested |
| Startup readiness gate (20 checks) | Built, tested |
| MCP read adapter | Built; **halts** until a server is connected |
| CLI write adapter | Built; **halts** until the schema is captured |
| Candidate generation, Featherless ranking, lifecycle, reconciliation, FastAPI, dashboard | Not built |

`398 tests pass.` Run them with:

```bash
.venv/Scripts/python.exe -m pytest
```

Run the readiness gate:

```bash
.venv/Scripts/python.exe -m src.startup
```

It currently reports `SYSTEM_STATE = HALTED` with 12 failing checks, all of them
Day 0 blockers listed below. That is the gate working.

## Blocked on Day 0 (manual, cannot be automated)

1. **DEV_ACCOUNT** — create an Alpaca **paper** account, enable **Options Level 3**,
   record the account id, options level and buying power.
2. **Manually place and close one multi-leg iron condor** on that account. If this
   fails, stop and diagnose the Alpaca account before writing any more code.
3. **Install the Alpaca CLI** — `brew install alpacahq/tap/cli` or
   `go install github.com/alpacahq/cli/cmd/alpaca@latest` — then `alpaca profile login`.
4. **Capture the installed binary's own output** into `docs/cli-reference.txt`:
   `alpaca doctor`, `alpaca --help-all`, `alpaca order submit --help`,
   `alpaca order submit --schema`. The installed CLI is the source of truth; no
   argument is implemented from memory.
5. **Connect `alpacahq/alpaca-mcp-server`** and let discovery write the live tool
   list to `docs/mcp-tools.json`. Historical v1 tool names are never hardcoded.
6. **JUDGE_ACCOUNT** — only after the suite is green: a brand-new paper account,
   never used for development, funded with $100,000, Options Level 3.

## Safety invariants

These are assertions, not preferences. Each one has tests.

- **Paper only.** The trading host must be `paper-api.alpaca.markets` over HTTPS.
  `ALPACA_LIVE_TRADE=true` and every equivalent flag, and any `--live` style
  argument, is rejected. No loop starts after a paper assertion fails.
- **MCP reads, CLI writes.** All broker/account/market state comes through
  `src/broker/alpaca_mcp.py`; every write goes through `src/broker/alpaca_cli.py`.
  A test walks the tree and fails the build on direct SDK or REST access anywhere else.
- **Defined risk only.** `put_credit_spread` and `iron_condor`, enforced by
  allow-list. Naked legs, straddles, strangles, ratio spreads, calendars and
  butterflies are rejected by leg-geometry derivation, not by name matching.
- **The LLM has zero trading authority.** Featherless receives immutable candidate
  descriptions and may return only `{"pick_id", "rationale"}`. It cannot create or
  change a strike, expiry, size, credit or limit, cannot call a broker tool, and
  cannot override a deny. A test asserts an adversarial `model_note` cannot move a
  single check.
- **The Risk Officer is the only capital gate.** Pure, deterministic, no network,
  no filesystem, no database, no broker, no clock read. Every check runs and all
  failure reasons are returned. Any exception is `DENY`.
- **The officer does not trust the ticket.** `width`, `credit`, `max_loss`, `dte`,
  `structure_type` and the leg relationships are recalculated from the OCC legs
  and validated quotes. A ticket claiming `max_loss: 1.0` on a $400 spread is denied.
- **Account isolation.** Development mode rejects the judging account and vice
  versa. There is no mode in which an unknown account is accepted.

## Layout

```
config/config.yaml        frozen contest limits (spec section 13)
config/events.json        data-driven macro calendar; no event logic in Python
docs/cli-reference.txt    PENDING - captured CLI schema
docs/mcp-tools.json       PENDING - discovered MCP tool list
src/config.py             config loading and consistency validation
src/types.py              Ticket, Permission, RiskDecision and friends
src/safety.py             paper-only invariant, dev/judge isolation
src/startup.py            20-check readiness gate
src/journal.py            append-only JSONL, the authoritative audit record
src/marketdna/            indicators, calendar, regime, permissions
src/rolldesk/             ratios, structure validation and geometry
src/risk/                 the Risk Officer and lot sizing
src/broker/occ.py         OCC symbol build/parse, the only place symbols are made
src/broker/alpaca_mcp.py  read adapter, runtime tool discovery
src/broker/alpaca_cli.py  write adapter, gated on the captured schema
```

## Journal

`journal/optiongenome.jsonl` is append-only and fsynced per write. The database is
a convenience; this file is the audit record. Events: `STARTUP`, `HALT`, `STATE`,
`REGIME`, `SCAN`, `CANDIDATES`, `RANK`, `ALLOW`, `DENY`, `SUBMIT`, `FILL`,
`REJECT`, `RECONCILE`, `TAKE_PROFIT`, `DEFEND`, `ROLL`, `FLATTEN`, `EXPIRE`, `ERROR`.
