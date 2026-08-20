# strategies/ — pure functions only

## Purity is load-bearing

No I/O. No database. No broker. No clock — `now` arrives as an argument. This is
not tidiness: `sandbox.backtest` imports these modules unchanged, and the moment
one of them reads a clock or a socket, backtests stop being evidence about live
behaviour and the entire research loop becomes decorative.

## Contract

- `evaluate(ticker, candles, now) -> Signal | None`
- **Entry signals only. Never return a `SELL`.** Strategies decide when to enter;
  `lifecycle.exits` decides when to leave. A strategy that returns `SELL` breaks
  the exit design silently.
- Return `None` — never raise — when there are fewer candles than `lookback`, or
  on degenerate input such as a flat price series.
- Deterministic: identical inputs produce identical outputs, every time.

## Testing

Every strategy needs, at minimum: a series that produces an entry, a series that
produces nothing, a too-short series, a flat series, and a determinism check.
Fixed inputs only — no randomness, no wall-clock.
