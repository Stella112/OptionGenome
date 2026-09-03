# Demo video script — OptionGenome

**≈3:00. Screen recording, voiceover. You never appear on camera.**

Read every line out loud before you record it. If you stumble on a line, or it feels like
something you'd never say, change the words. Say it the way you'd say it to a friend who
asked what you'd been building. Getting the wording exactly right matters much less than
sounding like a person who actually built the thing.

Check the dashboard first and say whatever it actually shows.

---

## Before you record

- One browser tab. `https://optiongenome.duckdns.org`. Nothing else visible.
- Hard-refresh so the numbers are current.
- **Close your terminal.** If `.env` shows up on screen your API keys are in the video forever.
- 1080p, browser at ~110% zoom.
- Scroll slowly. Slower than feels right.

---

## 0:00 — What it is

*Top of the dashboard.*

> Okay, so this is OptionGenome. It's a trading bot. It trades options, and it runs on its
> own — I don't touch it.
>
> Today it looked at two thousand and twenty-seven possible trades.

*(beat)*

> It took six.

---

## 0:20 — The problem

*Stay where you are. Don't scroll while you're talking.*

> Here's the thing I kept running into.
>
> Everyone's building the same bot right now. You feed a language model some market data,
> and it tells you what to buy. And they're genuinely good at that part. Ask one for a
> trade, you'll get a trade, with a solid-sounding reason attached.
>
> The bit nobody really tests is whether it'll ever say no. And it won't. It'll always find
> you something.

---

## 0:45 — What I did instead

> So I built it the other way round. The model in here is the thing I trust least.
>
> It doesn't pick trades. It gets handed a list the code already built and already checked,
> and all it does is put them in order. Then the code can just ignore what it picked.
>
> The trading itself is deliberately boring. It sells option spreads on SPY — you collect a
> bit of premium up front, and the worst case is a fixed number before you're even in the
> trade. And it manages them itself after that. Exits, adjustments, all of it.

---

## 1:10 — How it works

*Scroll to **How authority flows**. Let the diagram sit there. Don't read the boxes out.*

> It all flows one way.
>
> This first part works out what's even allowed right now — that's just maths, there's no
> model anywhere near it. This builds the actual trades. Model puts them in order. And this
> last one decides whether any money moves.

*(beat)*

> That's the model, there. Dashed box. It's inside the chain, not sat on top of it.
>
> Reads come in through Alpaca's MCP server, orders go out through their CLI. Nothing else
> can talk to the broker at all.

---

## 1:35 — One real decision

*Scroll to **The last decision, end to end**. Slow down here. This is the section that matters.*

> This is a real one from earlier.
>
> Reads the market. Builds three condors off the live option chain. Model picks one — and
> that's what it actually said, I'm not paraphrasing it.

*(pause on the quote — two full seconds)*

> Now, that's everything it didn't get.

*(let them read the struck-through line first)*

> No account balance. No buying power. It doesn't know the risk limits, doesn't know any of
> the rules. It gets a list it didn't write, and it hands back one ID.
>
> And then the risk officer binned it.

*(beat)*

> It doesn't take any of those numbers on trust — it works all of them out again itself. And
> it tells you why, in English. Nothing went anywhere near the broker.
>
> Model made a fair case. Code said no anyway.

---

## 2:10 — It keeps its own record

*Scroll to **Learning from its own record**.*

> It also logs what the market looked like every time it closes something. The delta, the
> width, the regime, how expensive volatility was going in.
>
> It's got four. It wants a hundred and twenty before it'll draw any conclusion from that.
>
> So it doesn't draw one. It just tells you it's got four.

*(beat)*

> You can't tune anything on four trades. You'd only be fitting it to luck.

---

## 2:30 — Things that only turned up live

*Scroll to **What running it live actually caught**.*

> These all got through the test suite. Green, the whole way. They only showed up once real
> orders were going out.
>
> This one, the capability lookup matched a *read* to an order-*placing* tool. So reading the
> order book would have put an order in.
>
> This one, credits were going out with the wrong sign. Everything filled worse than the
> limit I'd set.

*(beat)*

> And that last one's mine. I told it to close out the second a short got touched. Four
> trades, lost all four of them. It's up there with what it cost me.

---

## 2:50 — The trades, and the number

*Scroll to **Trades**. Leg detail visible.*

> Every trade it's taken, down to each individual order. Red's what it sold. Underneath is
> what it bought to cap the downside.
>
> And the number. It's down ninety-six dollars on a hundred grand. Six trades, three days.

*(beat — don't apologise, just keep going)*

> It's a loss. But I can tell you where every dollar of it went.

---

## 3:10 — Out

*Scroll back to the top. Let the READY badge sit for a second.*

> Six hundred and ninety-three tests. Every decision it's ever made is in a log file it can't
> go back and edit.
>
> And it's still running.

*(two seconds, then stop)*

---

## Delivery

**Say it flat.** Don't sell it. The refusal, the struck-through list, the minus ninety-six —
they all land harder said plainly than pushed. If a line feels like it needs energy behind it,
it needs to be shorter instead.

**Don't make every sentence a good one.** This is the thing that makes a voiceover sound
written. Most of what you say should just be plain and functional, so the two or three lines
that are actually good stand out. If you find yourself landing a clever line every fifteen
seconds, flatten some of them out.

**Pause where it's marked.** Those are the spots where the viewer needs a second to read the
screen. Silence is fine.

**Never say:** "as you can see", "let me show you", "what's interesting here is", "this is not
X, it's Y", "imagine if", "at the end of the day". Those are the giveaways.

**Don't describe what's on screen.** They can see it. Say what it means.

Fluff a line, just stop, breathe, and start that section again. Don't apologise on tape.

---

## If you need it shorter

**90 seconds:** *What it is* (0:00) → *The problem* (0:20) → *What I did instead* (0:45) →
*One real decision* (1:35) → the minus ninety-six line → "still running."

Drop the architecture, the record panel and the bug list. The problem and the refusal are the
whole argument — everything else is just backing it up.
