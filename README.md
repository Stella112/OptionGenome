# OptionGenome

An autonomous defined-risk options desk on **Alpaca paper trading**, built for the
Alpaca AI Trading Agents Hackathon. The model is the least-trusted component: it ranks a
shortlist it cannot write, and deterministic code holds the veto.

![OptionGenome](docs/cover.png)

| | |
|---|---|
| **Live dashboard** | https://optiongenome.duckdns.org |
| **Slide deck** | https://optiongenome.duckdns.org/deck (arrow keys) · [PDF](docs/OptionGenome-deck.pdf) |
| **Write-up** | [docs/WRITEUP.md](docs/WRITEUP.md) — AI logic, risk gates, Alpaca infrastructure |
| Alpaca paper account | `PA3Y88DE6VC4` |
| Submission copy | [docs/SUBMISSION.md](docs/SUBMISSION.md) |
| Demo video script | [docs/VIDEO-SCRIPT.md](docs/VIDEO-SCRIPT.md) |

## The problem

Most trading agents ask a language model what to trade and then do it. The model always
answers. It never says "nothing looks good, don't trade", and in trading, not trading is
most of the job.

## How it works

Authority flows one direction. Nothing downstream can reach back upstream.

```
MarketDNA ──> Roll Desk ──> [ model ranks ] ──> Risk Officer ──> Alpaca CLI
 permissions    candidates     pick_id only      15 checks        mleg / limit / day
 (arithmetic)   + lifecycle    hostile input     sole capital gate
```

- **MarketDNA** classifies the market from arithmetic only (EMA, ADX, realized vs
  implied vol, IV rank, an events calendar) and outputs a *permission set*: which
  structures are legal right now and how many lots. Never an order. It withholds
  permission entirely when implied vol is not at least 5% above realized.
- **Roll Desk** screens the live SPY option chain and builds fully specified candidates:
  put credit spreads and iron condors, 5 and 10 wide, shorts near 0.16–0.28 delta, ranked
  on credit net of round-trip bid-ask cost per dollar of risk. It also runs every open
  position through its lifecycle each pass.
- **The model** (Qwen-2.5-72B via Featherless) picks one candidate and writes one
  sentence why. That is its entire authority. It never sees equity, buying power, the risk
  budget or the rules. Its reply is parsed as hostile input; extra keys are ignored.
- **The Risk Officer** runs 15 deterministic checks and is the only thing that can commit
  capital. It recalculates width, credit, max loss and DTE from the OCC legs and live
  quotes rather than trusting the ticket. All checks run; a refusal lists every reason.
  Any exception is a DENY.
- **Alpaca.** All reads go through the Alpaca MCP server (72 tools discovered at runtime,
  none hardcoded). All writes go through the Alpaca CLI. A test walks the source tree and
  fails the build if anything else touches the broker.

## Strategy, in one sentence

Open 1-lot defined-risk put credit spreads and iron condors on SPY at 2–18 DTE during
INCOME and COMPRESSION regimes, take profit at 50% of credit, and mandatorily flatten at
1 DTE rather than carry anything into settlement.

Limits: max loss ≤ 0.75% of equity per structure, ≤ 2% new risk per day, 5% drawdown
triggers flatten-only, credit/width ≥ 0.15, quotes < 5s old, one lot, three open
structures, no overlapping short exposure, no entry in the final 15 minutes.

## Status

Running unattended on a VPS as two systemd services (desk loop + API) since 1 Sep 2026.
The dashboard reads everything from the desk's own journal; nothing on it is hardcoded.

At the time of writing: **2,412 decisions judged, 2,406 refused, 6 trades taken,**
P&L −$96 on $100,000 paper. Every dollar of that is in the journal and the dashboard's
"What running it live actually caught" panel explains where it went — including the
defects that only surfaced with real orders moving, and the one that was a bad rule of
mine rather than a bug.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env        # Alpaca paper keys, Featherless key, account ids
.venv/bin/python -m src.startup     # 20-check readiness gate; 19/20 is not READY
.venv/bin/python -m src.main        # the desk loop
.venv/bin/uvicorn src.api:app       # dashboard + JSON API
```

Tests (693):

```bash
.venv/bin/python -m pytest
```

Deploy to the VPS and re-verify: `scripts/deploy.sh`. Export the deck to PDF:
`scripts/build-deck-pdf.ps1`. Close every open structure through the CLI:
`scripts/close_open_structures.py`.

## Safety invariants

Each of these is a test, not a preference.

- **Paper only.** Trading host must be `paper-api.alpaca.markets`. Every live flag is
  rejected. No loop starts after a paper assertion fails.
- **MCP reads, CLI writes.** Direct SDK or REST access anywhere else fails the build.
- **Defined risk only.** Structures are validated by leg geometry, not by name.
- **The model has zero trading authority.** It returns `{pick_id, rationale}` and nothing
  it says can move a single check. 35 adversarial tests cover that path.
- **The Risk Officer is the only capital gate.** Pure, deterministic, no network, no
  filesystem, no clock read. No ALLOW means no broker command is ever constructed.
- **Account isolation.** Development mode rejects the judging account and vice versa.
- **Learning is measured, not applied.** The desk buckets every closed trade by delta,
  width and regime, but refuses to tune anything until it has 120 outcomes. Four trades
  is noise, and a desk whose argument is "no action without evidence" does not get an
  exception for itself.

## Layout

```
config/config.yaml         frozen contest limits
config/events.json         data-driven macro calendar
src/main.py                entry point
src/loop.py                one pass: regime -> candidates -> rank -> risk -> submit -> lifecycle
src/startup.py             20-check readiness gate
src/api.py                 FastAPI: /api/summary /api/decision /api/trades /api/learning /deck
src/dashboard.html         the dashboard
src/journal.py             append-only fsynced JSONL, the authoritative audit record
src/reconcile.py           rebuilds positions from the broker every pass
src/safety.py              paper-only invariant, dev/judge isolation
src/marketdna/             indicators, calendar, regime -> permission set
src/rolldesk/              candidates, ranker (model call), lifecycle, structure geometry
src/risk/                  the Risk Officer and lot sizing
src/broker/occ.py          OCC symbols; the only place they are built or parsed
src/broker/alpaca_mcp.py   read adapter, runtime tool discovery
src/broker/alpaca_cli.py   write adapter, --dry-run verified before every submit
docs/cli-reference.txt     the installed CLI's own captured output
docs/mcp-tools.json        the connected server's discovered tool list
```

## Journal

`journal/optiongenome.jsonl` is append-only and fsynced per write. Regimes: `INCOME`
`COMPRESSION` `MOMENTUM` `EVENT`. Lifecycle actions: `HOLD` `TAKE_PROFIT` `DEFEND`
`ROLL` `FLATTEN` `EXPIRE`. System states: `BOOTING` `READY` `FLATTEN_ONLY` `HALTED`.

---

Paper trading only. Simulated funds, real market data. Paper-trading results are
hypothetical and do not represent actual trading. Not investment advice.
