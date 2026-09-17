# Task 15/46: Implement `zarabot/strategies/ma_crossover.py`

## Product context

Moving-average crossover entries. Pure function over candles.

## Build order position

Module **15** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/ma_crossover.py`

Implements the `Strategy` protocol specified under `zarabot/strategies/base.py`,
which applies here unchanged — pure, no I/O and no clock beyond `now`, entry-only,
`BUY` or `None`, never `SELL`, deterministic. `name` is `"ma_crossover"` and
`lookback` is **81**: the slow window plus one bar, because a crossover is a
comparison between two consecutive bars and not a state of one. `lookback` is
**derived** from the slow window in code, not written as a literal, so the two
cannot drift apart.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: a simple moving average of the last **40** closes (fast) against one
  of the last **80** (slow). Both are module constants rather than configuration,
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

**The windows are 40/80 as of v1.94; they were 10/30.** The change is the owner's,
and the cost is signal frequency: measured against the live broker over 608
complete daily bars per ticker across the ten-ticker watchlist (~2.4 years),
**10/30 produced 110 crossovers and 40/80 produced 31** — about 3.5× fewer, or
roughly 13 entries a year across the whole watchlist. That number is recorded here
so the next reader knows what the slower pair cost without re-measuring it. Data
depth is not a constraint: §2.1 records 456 daily candles available against a
floor of 250, `app.loops` sizes its fetch from `max(strategy.lookback)` and
`market.data` converts that to calendar days, so 81 bars is requested
automatically. `ma_crossover` was the longest lookback at 31 and remains the
longest at 81, so no other strategy's fetch depth changes.

**Test fixtures must exceed the lookback, and that is not a style note.** A
candle series shorter than 81 returns `None` at the length guard, which is the
first branch in the function — so a flat-series fixture of 40 bars, or a
no-setup fixture of 31, returns `None` for a reason that has nothing to do with
flatness or with the absence of a crossing, and the test passes while proving
nothing. That is failure class 9, a fixture production cannot produce. Every
fixture in this module's tests other than the deliberately-short-series case is
longer than `lookback`.

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
