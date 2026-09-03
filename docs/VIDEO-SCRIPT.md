# Demo video script — OptionGenome

**≈3:00. Screen recording, voiceover. You never appear on camera.**

Written to be *spoken*, not read. Short sentences. Contractions. Say it flat and let the
screen carry it — the strongest lines here are the ones you don't push.

Check the dashboard before recording and say whatever it actually shows.

---

## Before you record

- One browser tab. `https://optiongenome.duckdns.org`. Nothing else visible.
- Hard-refresh so the numbers are current.
- **Close your terminal.** If `.env` appears on screen your API keys are in the video forever.
- 1080p, browser at ~110% zoom.
- Scroll slowly. Slower than feels right.

---

## 0:00 — What it is

*Top of the dashboard. No throat-clearing, no "hi, my name is."*

> This is OptionGenome. It's an options trading desk that runs itself.
>
> Today it made two thousand and twenty-seven decisions.

*(beat)*

> It refused all but six.

---

## 0:20 — The problem

*Stay at the top. Don't scroll yet — let them hear this.*

> Almost every trading agent right now works the same way. You give a language model some
> market data and it tells you what to buy.
>
> The trouble is what those models are actually good at. They're excellent at producing a
> confident, plausible-sounding trade. They're terrible at saying "no, not this one."
>
> Give one authority over real money and it'll do something stupid — fluently, with a good
> explanation attached.

---

## 0:45 — The solution

> So I flipped it round. Here, the model is the least-trusted thing in the system.
>
> It never decides anything. It ranks a shortlist that deterministic code has already built
> and checked, and code can throw its pick straight in the bin.
>
> What it actually trades is defined-risk options spreads on SPY. It sells a spread, collects
> a premium, and the worst case is capped before the position opens. Then it manages every
> one of them to exit on its own — no one's watching it.

---

## 1:10 — How it works

*Scroll to **How authority flows**. Let the diagram sit. Don't read the boxes aloud.*

> Everything moves one direction.
>
> MarketDNA decides what's legal right now — that part's pure arithmetic, no model anywhere
> near it. Roll Desk builds the structures. The model ranks them. The Risk Officer decides
> whether any money actually moves.

*(beat)*

> See where the model sits? Inside the chain. Dashed box. It's fenced in.
>
> Reads come back through Alpaca's MCP server. Writes leave only through the Alpaca CLI.
> Nothing else touches the broker.

---

## 1:35 — One decision, all the way through

*Scroll to **The last decision, end to end**. This is the section that matters. Take your time.*

> Here's a real decision.
>
> MarketDNA reads the tape. Roll Desk builds three defined-risk condors off the live option
> chain. The model picks one — that's its actual reasoning, word for word.

*(pause on the quote — two full seconds)*

> Now look at what it never got.

*(let them read the struck-through line before you say it)*

> No equity. No buying power. No risk budget. None of the rules. It gets a list it can't write
> and hands back one ID.
>
> And the Risk Officer threw it out.

*(beat)*

> It recalculates every number instead of trusting any of them. Tells you why, in plain
> English. Nothing reached the broker.
>
> The model made a decent argument. The code overruled it.
>
> That's the project.

---

## 2:10 — It watches its own results

*Scroll to **Learning from its own record**.*

> It also tracks the conditions behind every trade it closes. Delta, width, regime, how rich
> volatility was going in.
>
> Four closed trades. It needs a hundred and twenty before it'll draw a conclusion. So it says
> that, and changes nothing.

*(beat)*

> Tuning parameters on four results is fitting noise. A system that refuses trades without
> evidence doesn't get an exception for itself.

---

## 2:30 — What running it live caught

*Scroll to **What running it live actually caught**.*

> All of these got past a green test suite. They only turned up once real orders were moving.
>
> Capability discovery matched a *read* to an order-*placing* tool. Reading the order book
> would have placed an order.
>
> Credits went out with the wrong sign, so every entry filled below the limit I'd set.

*(beat)*

> And the last one's mine. I made it close positions the second a short got touched. Four
> trades, four losses. It's on the list, with what it cost.

---

## 2:50 — The trades, and the number

*Scroll to **Trades**. Leg detail visible.*

> Every trade, down to the individual buy and sell orders. Shorts in red, the protection that
> caps the loss underneath.
>
> And the number — down ninety-six dollars on a hundred thousand. Six trades, one lot each,
> three days.

*(beat — don't apologise for this)*

> I'd rather show you something that refuses honestly and explains its losses than a green
> number I can't account for.

---

## 3:10 — Out

*Scroll back to the top. Let the READY badge and the pulse sit.*

> Six hundred and ninety-three tests. Every decision in an append-only journal.
>
> Still running.

*(two seconds, then stop)*

---

## Delivery

**Confidence is flatness.** Don't sell it. The refusal, the struck-through list, the −$96 —
all of those land harder said plainly. If a line feels like it needs a push, it needs to be
shorter, not louder.

**Pause where marked.** Every beat is a place the viewer needs a second to read the screen.
Dead air is fine.

**Don't say:** "as you can see", "let me show you", "what's interesting here is", "this is not
X, it's Y". Those are the tells.

**Don't narrate the screen.** They can read it. Say what it *means*.

Fluff a line? Stop, breathe, restart that section. Never apologise on the recording.

---

## If you need it shorter

**90 seconds:** *What it is* (0:00) → *The problem* (0:20) → *The solution* (0:45) → *One
decision* (1:35) → the −$96 line → "Still running."

Cut the architecture, the learning panel and the bug list. The problem and the refusal are the
argument; everything else is evidence for it.
