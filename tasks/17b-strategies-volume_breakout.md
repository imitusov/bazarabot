# Task 17b/46: Implement `zarabot/strategies/volume_breakout.py`

## Product context

Momentum breakout confirmed by volume. The only strategy that reads Candle.volume, which every fetch carries and nothing read before. Pure function over candles.

## Build order position

Module **17b** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/volume_breakout.py`

Implements the `Strategy` protocol under `zarabot/strategies/base.py`, unchanged
and in full — pure, no I/O and no clock beyond `now`, entry-only, `BUY` or
`None`, never `SELL`, deterministic. `name` is `"volume_breakout"` and
`lookback` is **21**: the twenty prior bars both the breakout level and the
average volume are measured over, plus the bar that breaks out. `lookback` is
**derived** in code as `max(_BREAKOUT_BARS, _VOLUME_BARS) + 1`, not written as a
literal, so it cannot drift from the two windows it depends on.

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Parameters: a **20**-bar prior window for the breakout level, a **20**-bar
  prior window for the average volume, and a volume multiple of **1.5**. All
  three are module constants rather than configuration, on the same reasoning
  `ma_crossover`, `rsi_reversion` and `momentum` give: changing one changes what
  the strategy means, so it travels with the code and a redeploy.
- Returns a `BUY` when **both** conditions hold on the latest bar: its **close**
  is strictly above the highest **high** of the twenty bars before it, and its
  **volume** is at or above 1.5× the mean volume of those same twenty bars.
- The price half is `momentum`'s condition and is deliberately identical, high
  and not close, for the reason stated there. This strategy exists for the
  second half. `Candle.volume` is populated by `broker.client` on every fetch
  and, before v1.95, was read by **no** strategy: a breakout on thin volume is
  the textbook false breakout, and until now nothing in the bot could tell one
  from a breakout the market actually participated in.
- The threshold is **at or above** (`≥`), not strictly above. 1.5× is a
  conventional participation filter rather than a measured optimum, and the
  inclusive bound is the one a test can pin exactly: a bar at exactly 1.5× is
  confirmation, not a near miss.
- The volume comparison is done in `Decimal`. `Candle.volume` is an `int` and is
  not money, but the mean of twenty integers is not an integer, and `float`
  would make the boundary case above non-deterministic in its last bits.
- Returns `None` when fewer than `lookback` candles are supplied, when the close
  does not exceed the prior high, when it does but the volume filter fails, and
  on a flat series.

**Test fixtures must be at least `lookback` long.** The length guard is the first
branch in the function, so a fixture shorter than 21 returns `None` for a reason
that has nothing to do with the breakout or the volume — failure class 9, and
the trap #252 found twice in this package. Every fixture in this module's tests
other than the deliberately-short-series case is at least `lookback` long.

**§3.2, additionally for this module:** the same price breakout evaluated twice,
once with the breakout bar's volume below the multiple and once at or above it,
returns `None` in the first case and a `BUY` in the second. That pair is the
whole of what distinguishes this strategy from `momentum`; without it the volume
filter could be deleted and the suite would stay green. A test also pins
`lookback` against `max(_BREAKOUT_BARS, _VOLUME_BARS) + 1` so a future literal
cannot drift, and a test asserts the happy-path fixture is at least `lookback`
long.

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

- `zarabot/strategies/volume_breakout.py` implementing the contract exactly
- `tests/test_strategies_volume_breakout.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_volume_breakout.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/volume_breakout.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
