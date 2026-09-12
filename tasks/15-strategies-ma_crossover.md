# Task 15/42: Implement `zarabot/strategies/ma_crossover.py`

## Product context

Moving-average crossover entries. Pure function over candles.

## Build order position

Module **15** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/ma_crossover.py`

Implements the `Strategy` protocol specified under `zarabot/strategies/base.py`,
which applies here unchanged — pure, no I/O and no clock beyond `now`, entry-only,
`BUY` or `None`, never `SELL`, deterministic. `name` is `"ma_crossover"` and
`lookback` is **31**: the slow window plus one bar, because a crossover is a
comparison between two consecutive bars and not a state of one.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: a simple moving average of the last **10** closes (fast) against one
  of the last **30** (slow). Both are module constants rather than configuration,
  on the same reasoning as `ml_model`'s confidence threshold: changing one changes
  what the strategy means, so it travels with the code and a redeploy.
- Returns a `BUY` when the fast average was **at or below** the slow average one
  bar ago and is **strictly above** it on the latest bar. The condition is the
  crossing, not the ordering: a series that has been above for weeks crosses
  nothing and returns `None`.
- `reference_price` on the returned `Signal` is the latest close, and
  `generated_at` is the `now` it was given.
- Returns `None` when fewer than `lookback` candles are supplied, and `None` when
  every supplied close is identical — a flat series is the degenerate input the
  protocol requires be answered with `None` rather than an exception.

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

- `zarabot/strategies/ma_crossover.py` implementing the contract exactly
- `tests/test_strategies_ma_crossover.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_ma_crossover.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/ma_crossover.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
