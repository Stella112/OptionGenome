# Demo video script

Under three minutes. Screen recording of the dashboard, you talking.

Before you record: one tab open at https://optiongenome.duckdns.org, terminal closed,
say the numbers you see on screen.

---

**[top of dashboard]**

Hi, this is OptionGenome. It's a trading bot that trades options by itself on Alpaca.

Here's the problem it solves. Most AI trading bots let the AI decide what to trade. But AI
always says yes. Ask it for a trade and it will give you one, every time, even when it
shouldn't. In trading, that's how you lose money.

So in OptionGenome the AI is not in charge. The code is.

Here's how it works. Step one, the code checks the market and decides if trading is even
allowed right now. Step two, the code builds a few safe trades, where the most you can lose
is fixed up front. Step three, the AI picks the one it likes best. Step four, a risk checker
goes through that pick with fifteen rules, and if anything fails, the trade is blocked.

Today it looked at over two thousand trades and only took six. That's the point.

**[scroll to the diagram]**

This is the flow. Market check, build trades, AI picks, risk check, then the broker. The AI
is the dashed box in the middle. It can't see the account balance, can't see the rules, and
can't place an order. It only picks from a list the code gives it.

**[scroll to "The last decision"]**

Here's a real example. The code built three trades. The AI picked one, and this is its
reason, straight from the log. Then the risk checker said no, and here's why. So no trade
happened. The AI was overruled by the code. That's the whole system in one screen.

**[scroll to Trades]**

These are all the trades it's made, with every buy and sell. And the profit and loss. It's
down about ninety-six dollars on a hundred thousand. Small loss, six trades, and every
dollar is logged so you can see exactly what happened.

**[scroll to top]**

It's been running on its own for three days with no crashes. Six hundred and ninety-three
tests. Every decision is written down. That's OptionGenome. Thanks.
