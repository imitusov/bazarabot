# sandbox/ — research, and it must never lie

## The one rule

`sandbox.backtest` imports `zarabot.strategies`, `zarabot.risk.sizing` and
`zarabot.lifecycle.exits` **directly and unchanged**. Never reimplement a
strategy, a sizing rule, or an exit condition here — not "just for the
backtest", not "a simplified version", not "temporarily".

A backtester that reimplements the rules tests the reimplementation. It will
agree with itself perfectly and tell you nothing about the system that trades
your money. There is a test asserting the backtester and the live path produce
identical decisions for identical inputs; it exists to catch exactly this drift.

## Never imported by the server

Nothing under `zarabot/` may import from `sandbox/`. The dependency points one
way only.

## Honest simulation

- A strategy is never passed a candle timestamped at or after the decision
  instant. Look-ahead bias is the most common way a backtest flatters a strategy.
- Commission and a configured slippage assumption apply to every simulated fill.
- Model training uses walk-forward validation. A single train/test split on a
  time series proves nothing.
