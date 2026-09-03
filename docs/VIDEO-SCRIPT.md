# Demo video script

Screen recording of the dashboard with you talking over it. Around three minutes.

This is written the way you'd actually say it, so it's a bit rambly on purpose. Don't tidy
it up. If you'd say a bit differently, say it your way. Don't try to hit the lines exactly —
just keep the order and cover the points.

Before you hit record:
- one browser tab, `https://optiongenome.duckdns.org`, hard refresh so the numbers are fresh
- close your terminal — if `.env` ends up on screen your API keys are in the video forever
- read the numbers off the screen, don't use the ones written here if they've changed

---

**[top of the dashboard]**

Hey, so this is OptionGenome. It's what I built for the Alpaca hackathon. Basically it's a
bot that trades options on its own on Alpaca paper trading, and the whole idea behind it is
that the AI inside it isn't allowed to actually make decisions.

So let me explain what I mean by that. If you look at most of the trading agent projects out
there, the way they work is you've got an LLM, you give it some market data, you ask it what
to trade, and it tells you, and then you go and do that. And that works, like, the model will
give you an answer every single time. But that's kind of the problem. It gives you an answer
every single time. It never comes back and says "actually nothing looks good right now, don't
trade." And in trading, not trading is most of the job.

So what I did is I flipped it around. The model in OptionGenome can't decide anything. It
can't see the account balance, it can't see buying power, it doesn't know what the risk rules
are. All it gets is a short list of trades that my code already built and already checked,
and it picks the one it likes best. And then there's a completely separate piece of code, I
call it the risk officer, that re-checks that pick against fifteen rules and can just throw
it out. And it does. Like today it ran two thousand and something times and it actually
traded six.

The trades themselves are pretty boring on purpose. It sells option spreads on SPY, so you
collect a bit of premium up front and your worst case is a fixed number before you're even
in the trade. And it manages them itself after that, so exits, adjustments, all of that.

**[scroll to the diagram — "How authority flows"]**

Okay so this is how it's wired up. It all goes one direction. MarketDNA is the first thing,
that figures out what kind of market we're in and whether trading's even allowed right now,
and that's just math, there's no AI in there at all. Then Roll Desk builds the actual trades.
The model ranks them. And the risk officer is the last gate before anything hits the broker.
You can see the model there, the dashed box in the middle. It's inside the pipeline, it's not
running it.

And then all the reads come in through Alpaca's MCP server and all the orders go out through
the Alpaca CLI. I've actually got a test that walks the source code and fails if anything
else tries to talk to the broker.

**[scroll to "The last decision, end to end"]**

This is probably the most interesting panel. This is the last decision it made, start to
finish. So MarketDNA read the market, Roll Desk built three iron condors off the live option
chain, and the model picked one — and that's its actual reasoning, that's copied straight out
of the log, I didn't touch it.

And then here, this bit, that's everything it wasn't shown. Equity, buying power, the risk
budget, the rules. None of that. And then the risk officer looked at the pick and rejected
it, and it tells you why in plain English. So the model made a reasonable argument and it got
overruled anyway and no order went out. That's basically the whole project in one panel.

**[scroll to "Learning from its own record"]**

This one's kind of a self-own. So it tracks the conditions around every trade it closes, the
delta, the width, what regime it was in, how expensive volatility was, and the idea is it
works out over time what's actually working. But it's only got four closed trades. And I put
a threshold on it of a hundred and twenty before it's allowed to conclude anything, because
if you tune your strategy on four trades you're just tuning it to noise. So it's collecting,
it's just not acting on it yet, and it tells you that instead of pretending.

**[scroll to "What running it live actually caught"]**

And this is a list of bugs I only found because it was running live with real orders going
out. All of these passed the test suite. Like, one of them, the capability discovery matched
a read tool to an order-placing tool, so reading the order book would have actually placed an
order. Another one, I had the sign backwards on credit orders, so every fill came in worse
than the limit I'd set. And the last one is honestly just me. I made it close positions the
moment a short strike got touched, and that lost four trades in a row. So it's up there with
what it cost.

**[scroll to Trades]**

And then this is every trade it's done, with the individual legs, so you can see what it
sold and what it bought underneath as protection. And the P&L. It's down about ninety-six
dollars on a hundred grand. Six trades in three days, one lot each. It's a loss, I'm not going
to pretend it isn't, but every dollar of it is in the log and I can tell you exactly where it
went.

**[scroll back to the top]**

Anyway, that's it. Six hundred and ninety-three tests, everything it does goes into a journal
it can't go back and edit, and it's still running right now. Thanks.

---

If you need it shorter, keep the intro, the decision panel, and the P&L bit at the end. Drop
the diagram, the learning panel and the bug list.
