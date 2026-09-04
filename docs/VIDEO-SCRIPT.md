# Demo video script

Screen recording of the dashboard, you talking over it. About two and a half minutes.

Say it your way. Keep the order, cover the points, don't try to hit the lines word for word.

Before you record:
- one browser tab, `https://optiongenome.duckdns.org`, hard refresh
- close your terminal — if `.env` ends up on screen your API keys are in the video forever
- read the numbers off the screen, not the ones written here

---

**[top of the dashboard]**

Hey, this is OptionGenome. It's a bot that trades options on its own on Alpaca paper
trading, and the whole idea is that the AI inside it isn't allowed to make decisions.

Most trading agents work the same way. You give a language model market data, ask it what
to trade, and it tells you. And it always tells you. It never says "nothing looks good,
don't trade." In trading, not trading is most of the job.

So I flipped it. The model here can't see the account balance, can't see buying power,
doesn't know the risk rules. It gets a short list of trades my code already built, picks
one, and then a separate piece of code, the risk officer, re-checks that pick against
fifteen rules and can throw it out. Today it ran two thousand times and traded six.

**[scroll to the diagram]**

This is how it's wired. MarketDNA works out what's allowed right now, that's just math.
Roll Desk builds the trades. The model ranks them. The risk officer is the last gate. You
can see the model there, the dashed box. It's inside the pipeline, it's not running it.

Reads come through Alpaca's MCP server, orders go out through the Alpaca CLI, and a test
fails the build if anything else touches the broker.

**[scroll to "The last decision, end to end"]**

This is a real decision, start to finish. Three iron condors off the live chain, the model
picked one, and that's its actual reasoning from the log.

This bit here is everything it wasn't shown. Equity, buying power, the rules. And then the
risk officer rejected the pick and says why in plain English. Model made a reasonable case,
got overruled, no order went out. That's the whole project in one panel.

**[scroll to Trades]**

Every trade with the individual legs, what it sold and what it bought as protection. And
the P&L. Down about ninety-six dollars on a hundred grand, six trades in three days. It's a
loss, but every dollar of it is in the log and I can tell you exactly where it went.

**[scroll back to top]**

Six hundred and ninety-three tests, everything goes into a journal it can't edit, and it's
still running right now. Thanks.
