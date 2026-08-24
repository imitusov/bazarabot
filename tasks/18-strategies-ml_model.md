# Task 18/40: Implement `zarabot/strategies/ml_model.py`

## Product context

Optional ML strategy, disabled unless a model file is configured. Fails loudly at startup, never mid-session.

## Build order position

Module **18** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/strategies/ml_model.py`

**`load(path: Path) → LoadedModel`**
- Loads the exported model and its feature manifest.
- Raises `ModelLoadError` when absent or unreadable, and `ModelContractError`
  when the manifest's feature names or order differ from those the code builds.
- **Trust assumption:** loading a joblib bundle executes code contained in the
  file. `ML_MODEL_PATH` must therefore point only at a model this project's own
  `sandbox.train` produced and the owner copied across. It is not a path to
  accept from anywhere else, and this is a deployment rule rather than something
  the loader can validate.
- Called once at startup, never on the trading path — a model failure must be
  loud and early, never mid-session.

**`build_features(candles: list[Candle]) → list[float]`**
- Pure. Builds the feature vector in `FEATURE_NAMES` order from the most recent
  `lookback` candles.
- Raises `ValueError` when given fewer candles than `lookback`.
- **Sole owner of feature construction.** `sandbox.train` imports this function;
  no other code computes these features. Duplicating it is a critical defect —
  see the sandbox contract.

**`evaluate(...) → Signal | None`** — as the protocol, returning `None` below
`CONFIDENCE_THRESHOLD`, a module constant rather than an environment variable.
The threshold is a property of the trained model, not of the deployment: moving
it changes what the model means, so it travels with the code and a redeploy, the
same way risk limits do. There is deliberately no `ML_CONFIDENCE_THRESHOLD`. Absent from the registry entirely when `ML_MODEL_PATH` is unset.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

17. **Model file missing, unreadable, or with a mismatched feature manifest**
    while ML is enabled → refuse to start. A silently disabled model would mean
    trading a different system than the owner believes.

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

- `zarabot/strategies/ml_model.py` implementing the contract exactly
- `tests/test_strategies_ml_model.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_strategies_ml_model.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/strategies/ml_model.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
