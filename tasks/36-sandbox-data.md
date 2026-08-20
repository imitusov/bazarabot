# Task 36/38: Implement `sandbox/data.py`

## Product context

Historical candle loading for research. Laptop only.

## Build order position

Module **36** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `sandbox/` — laptop research (never imported by server code)

**`async load(ticker: str, start: datetime, end: datetime, interval: CandleInterval, cache_dir: Path = Path("sandbox/cache")) → list[Candle]`**
- Async, because it calls `broker.client` on a cache miss. In a notebook this is
  awaited directly.
- `start` and `end` are timezone-aware; a naive value raises `ValueError`.
- Returns candles oldest-first, empty list when the range holds none.
- Caches to `<cache_dir>/<ticker>_<interval>.parquet`, writing through after a
  fetch. `sandbox/cache/` is gitignored: it is derived data, and committing a
  year of candles would bloat the repository for no benefit.
- A cached range that does not cover the request is extended by fetching only
  the missing span, never by refetching the whole range.
- Must never be imported by `zarabot/`.

**`backtest.run(strategy, candles, config, commission, slippage) → BacktestResult`**
- Replays candles in order, calling the **same** `strategies`, `risk.sizing` and
  `lifecycle.exits` functions the live path uses. Reimplementing any of them here
  is a critical defect: it makes every backtest unfalsifiable.
- A strategy is never passed a candle timestamped at or after the decision
  instant.
- Applies commission and the configured slippage assumption to every fill.
- Returns trades, P&L, win rate, maximum drawdown, exit-trigger distribution, and
  the buy-and-hold benchmark.

**`fit(candles_by_ticker: dict[str, list[Candle]], horizon_days: int, folds: int, seed: int) → FittedModel`**
- Trains a buy/no-buy classifier. The label is whether the take-profit level is
  reached before the stop level within `horizon_days`, so the model is trained on
  the question the live system actually asks it.
- `seed` is required and recorded in the export: an unreproducible model cannot
  be audited after a losing week.
- Uses **walk-forward** validation across `folds`; a single train/test split on a
  time series leaks the future into the past and is not acceptable.
- Returns the fitted model with its validation scores per fold. Reporting one
  averaged number hides a model that works in one regime and fails in another.

**`export(model: FittedModel, path: Path) → Path`**
- Writes a joblib bundle `{"model", "features", "seed", "trained_at"}` where
  `features` is `strategies.ml_model.FEATURE_NAMES` in order, which
  `strategies.ml_model.load` validates.

**Feature construction has one owner.** `strategies.ml_model.build_features`
builds the feature vector, and `sandbox.train` **imports it** rather than
rebuilding the same four features for training. This is the same rule as the
backtester importing the live strategies, for the same reason: features computed
one way at training and another way at inference produce a model that scores well
offline and behaves differently on real money, and nothing in the manifest check
would catch it — the names would still match.

---

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception.

## Expected output

- `sandbox/data.py` implementing the contract exactly
- `tests/test_sandbox_data.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_sandbox_data.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `sandbox/data.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
