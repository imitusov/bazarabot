# Task 23/38: Implement `zarabot/market/data.py`

## Product context

Candles for the watchlist. One failing instrument must never blind the bot to the rest.

## Build order position

Module **23** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/market/data.py`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
- Returns per-ticker candle series, oldest-first.
- A ticker that fails is omitted from the result and logged; the batch still
  returns. One unavailable instrument must never blind the bot to the rest.
- Never pads or interpolates missing candles.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

9. **Candle fetch fails for one ticker** → omit it, WARNING, continue the batch.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Candles for a watchlist ticker are returned newest-last, timezone-aware
  (happy path and ordering contract).
- Fewer candles available than the longest strategy lookback returns what exists
  and the caller can detect insufficiency (proves partial history is visible, not
  silently padded).
- A ticker failing while others succeed does not fail the batch (proves one bad
  instrument cannot blind the bot to the rest).

**`strategies.*`**

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
- With `ML_MODEL_PATH` unset, the strategy is absent from the registry (proves
  disabled-by-default).
- A missing or unreadable model file raises `ModelLoadError` at startup, not at
  first signal (proves failure is loud and early).
- A model whose feature contract does not match the expected names and order
  raises `ModelContractError` (proves a stale model cannot silently mispredict).
- A prediction below the confidence threshold returns `None` (boundary).

## Expected output

- `zarabot/market/data.py` implementing the contract exactly
- `tests/test_market_data.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_market_data.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/market/data.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
