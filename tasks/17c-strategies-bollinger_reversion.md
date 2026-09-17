# Task 17c/46: Implement `zarabot/strategies/bollinger_reversion.py`

## Product context

Mean reversion at the lower Bollinger band. Volatility-scaled where rsi_reversion is a fixed threshold. Decimal square root, never math.sqrt. Pure function over candles.

## Build order position

Module **17c** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/bollinger_reversion.py`

Implements the `Strategy` protocol under `zarabot/strategies/base.py`, unchanged
and in full. `name` is `"bollinger_reversion"` and `lookback` is **20**: the
band is a property of one window of closes, so no extra bar is needed — unlike
`ma_crossover`, nothing here is a comparison between two consecutive bars.
`lookback` is **derived** in code as `_PERIOD`, not written as a literal.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: a **20**-close window and **2** standard deviations. Module
  constants rather than configuration, for the reason the other strategies give.
- The lower band is the simple moving average of the last 20 closes minus 2
  **population** standard deviations of those same 20 closes. Population and not
  sample: the twenty closes are the window, not a draw from a larger one, and
  `N` rather than `N − 1` is the convention every published Bollinger band uses.
- Returns a `BUY` when the latest close is **at or below** that lower band.
  Inclusive, so the band edge is an entry: the band is already a
  two-deviation buffer and a strictly-below bound would make the exact-edge case
  untestable in `Decimal` arithmetic.
- This is mean-reversion like `rsi_reversion`, but **volatility-scaled**. RSI's
  threshold is a fixed 30 regardless of how the instrument has been behaving;
  this band widens as realised volatility rises and narrows as it falls, so the
  same percentage move is an entry in a quiet instrument and not in a violent
  one.
- **Precision.** The standard deviation needs a square root, and `Decimal` has
  one: the variance is accumulated in `Decimal` and rooted with
  `decimal.Context(prec=…).sqrt()`. `math.sqrt` is not used, because it would
  round a price through `float` — the rule is `Decimal` end to end, and a band
  edge computed in binary floating point is a comparison against money whose
  last bits depend on the platform. The context is local to the function, so the
  module never mutates the process-wide decimal context: a module-level
  `setcontext` would be a side effect, which this package forbids.
- Returns `None` when fewer than `lookback` candles are supplied, when the close
  is above the lower band, and on a flat series. **The flat-series guard is
  load-bearing here and not boilerplate:** a flat window has zero standard
  deviation, so the lower band equals the average, equals the close, and the
  inclusive bound would return a `BUY` on a series with no volatility at all.
  That is the degenerate input the protocol requires be answered with `None`.

**Test fixtures must be at least `lookback` long**, for the reason given under
`volume_breakout` — the length guard runs first.

**§3.2, additionally for this module:** a test pins `lookback` against `_PERIOD`
so a future literal cannot drift, a test asserts the happy-path fixture is at
least `lookback` long, and a test asserts a flat window of at least `lookback`
closes returns `None` rather than the `BUY` the inclusive bound would otherwise
produce.

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

- `zarabot/strategies/bollinger_reversion.py` implementing the contract exactly
- `tests/test_strategies_bollinger_reversion.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_bollinger_reversion.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/bollinger_reversion.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
