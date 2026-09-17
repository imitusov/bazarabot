# Task 17d/46: Implement `zarabot/strategies/macd_trend.py`

## Product context

MACD line crossing above its signal line. Trend like ma_crossover, but momentum-of-momentum rather than a level comparison. Pure function over candles.

## Build order position

Module **17d** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/macd_trend.py`

Implements the `Strategy` protocol under `zarabot/strategies/base.py`, unchanged
and in full. `name` is `"macd_trend"` and `lookback` is **35**, derived in code
as `_SLOW + _SIGNAL`: 26 closes to seed the slow EMA, after which each further
close yields one MACD value, and 9 MACD values to seed the signal EMA — leaving
exactly two signal values, which is the minimum a crossing can be read from. It
is **derived**, not a literal, so none of the three periods can drift from it.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: fast EMA **12**, slow EMA **26**, signal EMA **9** — the
  conventional MACD triple. Module constants rather than configuration, for the
  reason the other strategies give.
- Each EMA is seeded with the simple average of its first `period` values and
  then advanced with the standard `2 / (period + 1)` smoothing factor, in
  `Decimal`. Seeding with an SMA rather than with the first value is what makes
  the result a function of the window rather than of how much history happened
  to be fetched.
- The MACD line is the fast EMA minus the slow EMA, evaluated at every bar from
  the 26th onward. The signal line is the 9-period EMA of the MACD line.
- Returns a `BUY` when the MACD line was **at or below** the signal line one bar
  ago and is **strictly above** it on the latest bar. The condition is the
  crossing, not the ordering — `ma_crossover`'s rule exactly, and for the same
  reason: a series that has been above for weeks crosses nothing and returns
  `None`.
- This is a trend strategy like `ma_crossover`, but it is
  **momentum-of-momentum** rather than a level comparison: `ma_crossover`
  compares two averages of price, while this compares the *rate of change of the
  gap between them* against its own average, so it turns earlier in a move and
  costs more false starts.
- `reference_price` on the returned `Signal` is the latest close and
  `generated_at` is the `now` it was given, as for every strategy.
- Returns `None` when fewer than `lookback` candles are supplied, when no
  crossing occurred on the latest bar, and on a flat series.

**Test fixtures must be at least `lookback` long**, for the reason given under
`volume_breakout`.

**§3.2, additionally for this module:** a test pins `lookback` against
`_SLOW + _SIGNAL` so a future literal cannot drift, a test asserts the
happy-path fixture is at least `lookback` long, and a test asserts a series
already trending upward throughout — MACD above signal on both of the last two
bars — returns `None`, which is what distinguishes a crossing from an ordering.

**The three strategies above are new in v1.95, and none of them lengthens the
candle fetch.** Their lookbacks are 21, 20 and 35 against `ma_crossover`'s 81,
so `max(strategy.lookback)` in `app.loops` is unchanged and §2.1's 250-candle
floor is untouched. None of them is added to `ENABLED_STRATEGIES`'s default:
`registry` knows the names, and which of them trades is the owner's setting.

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

- `zarabot/strategies/macd_trend.py` implementing the contract exactly
- `tests/test_strategies_macd_trend.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_macd_trend.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/macd_trend.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
