# Demo video script — OptionGenome

**≈2:45. Screen recording, voiceover. You never appear on camera.**

Written to be *spoken*, not read. Short sentences. Contractions. Say it flat and let the
screen do the work — the strongest lines here are the ones you don't push.

Check the dashboard before recording and say whatever it actually shows.

---

## Before you record

- One browser tab. `https://optiongenome.duckdns.org`. Nothing else visible.
- Hard-refresh so the numbers are current.
- **Close your terminal.** If `.env` shows up on screen your API keys are in the video forever.
- 1080p, browser at ~110% zoom.
- Scroll slowly. Slower than feels right.

---

## 0:00 — Open cold

*Top of the dashboard. Don't introduce yourself. Don't say "let me show you."*

> Two thousand and twenty-seven decisions today.
>
> It refused all but six.

*(beat)*

> Which is exactly what I built it to do.

---

## 0:15 — What it is

*Scroll to **How authority flows**. Let the diagram sit. Don't narrate the boxes.*

> This is an options desk that runs itself on Alpaca paper trading. Everything flows one
> direction.
>
> MarketDNA decides what's legal — that part's pure arithmetic, no model anywhere near it.
> Roll Desk builds the structures. The model ranks them. The Risk Officer decides whether any
> money actually moves.

*(beat)*

> See where the model sits? Inside the chain. Dashed box. It's fenced in.

---

## 0:40 — One decision, all the way through

*Scroll to **The last decision, end to end**. This is the section that matters. Take your time.*

> Here's a real decision.
>
> MarketDNA reads the tape. Roll Desk builds three defined-risk condors off the live option
> chain. The model picks one — and that's its actual reasoning, word for word.

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

## 1:20 — It watches its own results

*Scroll to **Learning from its own record**.*

> It tracks the conditions behind every trade it closes. Delta, width, regime, how rich
> volatility was going in.
>
> Four closed trades. It needs a hundred and twenty before it'll draw a conclusion. So it says
> that, and changes nothing.

*(beat)*

> Tuning parameters on four results is fitting noise. A system that refuses trades without
> evidence doesn't get an exception for itself.

---

## 1:45 — What running it live caught

*Scroll to **What running it live actually caught**.*

> Every one of these got past a green test suite. They only turned up once real orders were
> moving.
>
> Capability discovery matched a *read* to an order-*placing* tool. Reading the order book
> would have placed an order.
>
> Credits went out with the wrong sign, so every entry filled below the limit I'd set.

*(beat)*

> And the last one's mine. I made it close positions the second a short got touched. Four
> trades, four losses. It's on the list with what it cost.

---

## 2:10 — The trades, and the number

*Scroll to **Trades**. Leg detail visible.*

> Every trade, down to the individual buy and sell orders. Shorts in red. The protection that
> caps the loss underneath.
>
> And the number — down ninety-six dollars on a hundred thousand. Six trades, one lot each,
> three days.

*(beat — don't apologise for this)*

> I'd rather show you something that refuses honestly and explains its losses than a green
> number I can't account for.

---

## 2:35 — Out

*Scroll back to the top. Let the READY badge and the pulse sit there.*

> Six hundred and ninety-three tests. Every decision in an append-only journal.
>
> Still running.

*(two seconds, then stop)*

---

## Delivery

**Confidence is flatness.** Don't sell it. The refusal, the struck-through list, the −$96 —
those land harder said plainly than emphasised. If a line feels like it needs a push, it
doesn't.

**Pause where marked.** Every beat is somewhere the viewer needs a second to read the screen.
Dead air is fine.

**Don't say:** "as you can see", "let me show you", "what's interesting here is", "this is not
X, it's Y". Just say the thing.

**Don't explain what's on screen.** They can read. Talk about what it *means*.

If you fluff a line, stop, breathe, restart that section. Don't apologise on the recording.

---

## 60-second cut

Cold open → the decision at 0:40 → the −$96 line → "Still running."

That's the whole argument. Everything else is supporting material.
