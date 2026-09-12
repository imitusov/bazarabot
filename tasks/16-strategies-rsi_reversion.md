# Task 16/42: Implement `zarabot/strategies/rsi_reversion.py`

## Product context

RSI mean-reversion entries. Pure function over candles.

## Build order position

Module **16** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/rsi_reversion.py`

Implements the `Strategy` protocol under `zarabot/strategies/base.py`, unchanged
and in full. `name` is `"rsi_reversion"` and `lookback` is **15**: the RSI period
plus one close, since fourteen changes need fifteen closes.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: RSI period **14**, oversold threshold **30**. Module constants
  rather than configuration: changing either changes what the strategy means, so
  it travels with the code and a redeploy.
- RSI is computed from the last 14 close-to-close changes as
  `100 − 100 / (1 + mean gain / mean loss)`, both means taken over the period
  rather than over the number of up or down bars.
- Returns a `BUY` when that value is **strictly below 30**. At or above 30 is not
  oversold and returns `None`.
- Two windows have no RSI and therefore yield no signal: one with neither a gain
  nor a loss, and one with no loss at all, which reads as 100 — the opposite end
  of the scale from the entry this strategy takes.
- Returns `None` when fewer than `lookback` candles are supplied, and `None` on a
  flat series.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

For every strategy, independently:
- A price series designed to produce an entry returns a `Signal` naming the
  strategy and the ticker (happy path).
- A price series with no setup returns `None` (proves the nullable contract).
- A series shorter than the strategy's lookback returns `None` and does not raise
  (proves insufficient history is a non-event, not a crash).
- A series containing a flat price run returns `None` rather than dividing by
  zero (proves the degenerate-input path).
- The same input evaluated twice returns equal results (proves purity and
  determinism).
- No strategy returns a `SELL` signal under any input (proves the entry-only
  contract that the whole exit design rests on).

Additionally, `strategies.ml_model`:
- `build_features` returns values in `FEATURE_NAMES` order, and raises
  `ValueError` on fewer candles than `lookback` (proves the shared contract that
  training depends on).
- With `ML_MODEL_PATH` unset, the strategy is absent from the registry (proves
  disabled-by-default).
- A missing or unreadable model file raises `ModelLoadError` at startup, not at
  first signal (proves failure is loud and early).
- A model whose feature contract does not match the expected names and order
  raises `ModelContractError` (proves a stale model cannot silently mispredict).
- A prediction below the confidence threshold returns `None` (boundary).

## Expected output

- `zarabot/strategies/rsi_reversion.py` implementing the contract exactly
- `tests/test_strategies_rsi_reversion.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_rsi_reversion.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/rsi_reversion.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
