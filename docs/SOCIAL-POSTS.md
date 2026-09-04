# Social posts — bonus points

Up to 5 posts on X or LinkedIn, each tagging **both** @lablabai and @AlpacaHQ.

Post them from your own account, spaced out rather than all at once. Attach the screenshot
named under each one. Check the live numbers before posting — they move every minute.

Handles: X → `@lablabai` `@AlpacaHQ` · LinkedIn → `@lablab.ai` `@Alpaca`

---

## 1 — The thesis

> Most AI trading agents let the model decide what to trade.
>
> The problem: a language model always answers. Ask it for a trade and you get one, every
> time, with a confident rationale attached — whether or not there's anything worth trading.
>
> In options selling, refusing is most of the job.
>
> So I built OptionGenome the other way round. The model is the least-trusted component in
> the system. It ranks a shortlist it can't write, and deterministic code can throw its pick
> straight out.
>
> 2,421 decisions so far. 2,415 refused. 6 traded.
>
> @lablabai @AlpacaHQ #AlpacaHackathon

*Attach: `docs/cover.png`*

---

## 2 — The refusal, shown

> Screenshot from my hackathon build: the AI picked a trade, gave a reasonable argument for
> it, and got overruled by code.
>
> The struck-through list is everything the model never received — account equity, buying
> power, the risk budget, the rules it's being judged against. It gets a list it didn't write
> and hands back one ID.
>
> Then 15 deterministic checks re-derive every number from the raw option symbols instead of
> trusting the ticket, and say no in plain English.
>
> The model made a fair case. The code overruled it. That's the whole project.
>
> @lablabai @AlpacaHQ

*Attach: screenshot of the "last decision, end to end" panel*

---

## 3 — Bugs only a live broker finds

> Nine defects in my hackathon project that a fully green test suite never caught. All of them
> needed real orders moving to surface.
>
> — Capability discovery matched a READ tool to an order-PLACING tool, because "order" is a
> substring of both. Reading the order book would have placed an order.
>
> — Opening orders went out with a positive limit price. Alpaca reads positive as a debit cap,
> so every entry filled below the limit I thought I'd set.
>
> — 49 milliseconds of clock skew was denying every single trade.
>
> — The lifecycle layer was handed an empty list, so take-profit and the forced flatten had
> never once run in production. 39 tests covering them passed.
>
> Each one is now a named constant with a regression test explaining why it exists.
>
> Ship it live. The tests will lie to you.
>
> @lablabai @AlpacaHQ

*Attach: screenshot of the "what running it live actually caught" panel*

---

## 4 — Refusing to learn

> My trading agent tracks the conditions behind every trade it closes — delta, width, regime,
> how rich volatility was going in — so it can work out what's actually working.
>
> It has 4 closed trades. It needs 120 before it'll conclude anything.
>
> A credit spread wins ~70% of the time by construction, so telling a real edge from that
> baseline is a proportion test. Detecting a 15-point improvement at 95% confidence needs
> roughly 120 outcomes.
>
> So it measures, says exactly what it would take to know something, and changes nothing.
>
> A system whose whole argument is that it refuses to act without evidence doesn't get an
> exception for itself.
>
> @lablabai @AlpacaHQ

*Attach: screenshot of the learning panel*

---

## 5 — Submission / the honest number

> Submitted OptionGenome to the Alpaca AI Trading Agents Hackathon.
>
> An autonomous defined-risk options desk on Alpaca paper trading. Reads go through the Alpaca
> MCP server, writes go through the Alpaca CLI, and a test walks the source tree and fails the
> build if anything else touches the broker.
>
> Running unattended on a VPS since day one. 696 tests. Every decision in an append-only
> journal it can't edit.
>
> It's down $14 on $100,000. Small negative, and I can tell you where every dollar went —
> including the ~$48 that came from a bad rule I wrote myself, which is on the dashboard with
> what it cost.
>
> I'd rather show you something that refuses honestly than a green number I can't account for.
>
> Live: optiongenome.duckdns.org
>
> @lablabai @AlpacaHQ

*Attach: `docs/cover.png` or the dashboard top*

---

## After posting

Paste the URLs into the **Social Media Post Link 1–5** fields on Step 1 of the lablab form.
