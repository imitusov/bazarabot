# Task 37/38: Implement `sandbox/backtest.py`

## Product context

The backtester. Imports the live strategy, sizing and exit modules UNCHANGED - reimplementing any of them makes every backtest meaningless.

## Build order position

Module **37** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `sandbox/` — laptop research (never imported by server code)

**`data.load(ticker, start, end) → list[Candle]`** — from local cache, downloading via `broker.client` when absent.

**`backtest.run(strategy, candles, config, commission, slippage) → BacktestResult`**
- Replays candles in order, calling the **same** `strategies`, `risk.sizing` and
  `lifecycle.exits` functions the live path uses. Reimplementing any of them here
  is a critical defect: it makes every backtest unfalsifiable.
- A strategy is never passed a candle timestamped at or after the decision
  instant.
- Applies commission and the configured slippage assumption to every fill.
- Returns trades, P&L, win rate, maximum drawdown, exit-trigger distribution, and
  the buy-and-hold benchmark.

**`train.fit(...) → Path`** and **`train.export(model, features, path) → Path`**
- Exports the model together with a feature manifest naming the features and
  their order, which `strategies.ml_model` validates on load.
- Uses walk-forward validation; a single train/test split is not acceptable for a
  time series.

---

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A backtest over a known price series produces the hand-computed trade sequence
  (proves the engine is correct against a worked example).
- The backtester and the live path produce identical decisions for identical
  inputs (proves the shared-code invariant — this is the test that makes
  backtests trustworthy).
- A strategy is never given a candle timestamped after the decision instant
  (proves absence of look-ahead bias).
- Commission and a configured slippage assumption are applied to every simulated
  fill (proves the results are not idealised).

---

## Expected output

- `sandbox/backtest.py` implementing the contract exactly
- `tests/test_sandbox_backtest.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_sandbox_backtest.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `sandbox/backtest.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
