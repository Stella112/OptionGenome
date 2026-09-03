# Demo video script — OptionGenome

**Target: 2 minutes 30.** Screen recording with voiceover. You never appear on camera.

Numbers below were live at time of writing. **Check the dashboard before recording** and say
whatever it shows — being accurate matters more than matching this text.

---

## Before you hit record

- Open **one** browser tab: `https://optiongenome.duckdns.org`. Nothing else.
- Hard-refresh (`Ctrl+Shift+R`) so the figures are current.
- Close the terminal, or make sure `.env` is nowhere on screen. **Your API keys must not appear.**
- Record at 1920×1080. Zoom the browser to ~110% so text is readable when compressed.
- Speak slower than feels natural. Pause between sections.

---

## 0:00 – 0:20 · The hook

**On screen:** top of the dashboard. The `READY` badge and the P&L tiles visible.

> "This is an autonomous options trading desk. It's running right now, on Alpaca paper
> trading, and it's made just over two thousand decisions.
>
> It refused two thousand and twenty-one of them."

*Pause. Let that land.*

> "That's not a failure. That's the whole design."

---

## 0:20 – 0:45 · What it is

**On screen:** scroll slowly to **How authority flows**. Let the diagram sit still.

> "Authority runs one direction only.
>
> MarketDNA reads the market and decides what's *legal* — that's pure arithmetic, no AI.
> Roll Desk builds defined-risk structures. The model ranks them. And the Risk Officer decides
> whether any capital actually moves.
>
> Notice where the model sits — inside the chain, dashed, fenced in. Not at the top.
> Reads come back through Alpaca's MCP server. Writes leave only through the Alpaca CLI."

---

## 0:45 – 1:30 · The live decision — *this is the important part*

**On screen:** scroll to **The last decision, end to end**. Move slowly through the five steps.

> "Here's one real decision, start to finish.
>
> MarketDNA reads the tape. Roll Desk builds three defined-risk candidates from the live
> option chain.
>
> Then the model picks one — and this is its actual reasoning, in its own words."

*Pause on the quote for two seconds.*

> "But look at what it was never given."

*Point at the struck-through line.*

> "No equity. No buying power. No risk budget. None of the rules. It returns one ID from a
> list it cannot write.
>
> And then — the Risk Officer refused it."

*Pause.*

> "It recalculates every number rather than trusting what it's handed, and it tells you why,
> in plain English. Nothing was sent. No approval means no order is ever constructed.
>
> The AI made a reasonable argument. The code overruled it. That's the project."

---

## 1:30 – 1:55 · What running it live caught

**On screen:** scroll to **What running it live actually caught**.

> "Every one of these got past a green test suite and only appeared once real orders were
> moving.
>
> Capability discovery matched a *read* to an order-*placing* tool — reading the order book
> would have placed an order.
>
> Credits were sent with the wrong sign, so every entry filled below the limit we thought
> we'd set.
>
> And the last one is mine. I made it close positions the moment a short was touched. Four
> trades, four losses, zero percent win rate. It's in the list, with what it cost."

---

## 1:55 – 2:20 · The trades, and the honest number

**On screen:** scroll to **Trades**. The leg-level buy/sell detail visible.

> "Every trade, down to the individual buy and sell orders. Short strikes in red, the
> protection that caps the loss in green.
>
> And the number: down ninety-six dollars on a hundred thousand. Fifty-six realised, forty
> unrealised on two positions that are still open.
>
> Six trades, one lot each, over three days. I'd rather show you a system that refuses
> honestly and explains its losses than a green number I can't account for."

---

## 2:20 – 2:35 · Close

**On screen:** scroll back to the top. Let the `READY` badge and the pulse show.

> "Six hundred and eighty-eight tests. Every decision in an append-only journal. Paper
> trading only — simulated funds, real market data.
>
> It's still running."

*Hold for two seconds. Stop recording.*

---

## If you only have 60 seconds

Cut to: the hook (0:00–0:20), the live decision (0:45–1:30), and the last line. That's the
argument. Everything else is supporting detail.

## What to avoid

- Don't claim profit. The number is negative and a judge will check.
- Don't read the architecture diagram aloud box by box — let it be seen while you talk.
- Don't rush the refusal. It's the most distinctive thing on screen and it needs a beat.
