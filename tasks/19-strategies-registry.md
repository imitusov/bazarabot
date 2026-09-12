# Task 19/42: Implement `zarabot/strategies/registry.py`

## Product context

Builds the active strategy set from configuration.

## Build order position

Module **19** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/registry.py`

**`enabled(config: Config) → list[Strategy]`**
- Builds the active strategy set from `ENABLED_STRATEGIES`, instantiating one
  strategy per name **in the order the setting lists them**, and returning an
  empty list rather than `None` when it enables nothing.
- Raises `ConfigError` naming `ENABLED_STRATEGIES` on a name it does not know.
  This is the whole of the module's validation, and it happens at startup rather
  than mid-session: a typo in a strategy name must stop the bot, never silently
  trade a smaller set than the owner configured.
- `ml_model` is the one name with a condition attached: it is **omitted entirely**
  when `ML_MODEL_PATH` is unset, and otherwise `ml_model.load` is called here, so
  `ModelLoadError` and `ModelContractError` propagate out of this function to
  `app.startup` step 4, which is rule 17's refusal to start. Omitting it is not a
  silent disable — an unset path is the owner saying ML is off.
- Pure apart from that one load: no clock, no database, no broker, no I/O of its
  own. `load` is the exception, and it is the reason this function is called once
  at startup and never on the trading path.

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

- `zarabot/strategies/registry.py` implementing the contract exactly
- `tests/test_strategies_registry.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_registry.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/registry.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
