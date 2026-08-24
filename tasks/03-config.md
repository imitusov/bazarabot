# Task 3/39: Implement `zarabot/config.py`

## Product context

Loads and validates every setting once at startup. The bot refuses to start rather than trade on an assumed risk limit.

## Build order position

Module **3** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/config.py`

Loads and validates every setting once at startup.

**`load() → Config`**
- Reads all variables in the brief's environment-variable table (§18), applies
  defaults, coerces types, and validates.
- Returns a frozen `Config`.
- Resolves `tinvest_token` and `tinvest_account_id` for the mode in force: when
  `TRADING_MODE` is `sandbox`, each is taken from its `*_SANDBOX` variable and
  falls back to the base variable when that is unset or blank. Live mode ignores
  the overrides. The rest of the codebase sees one token and one account id and
  never branches on mode — sandbox remains selected by endpoint alone.
- Adds `price_max_age_seconds` (default 120) and `price_max_move_pct` (default
  20), the bounds `broker.client` validates quotes against.
- `ssl_tbank_verify` defaults to true. **Setting it false must be loud**: it
  disables certificate verification on the connection carrying the trading
  token, so `config.load()` logs a CRITICAL line naming the risk, and
  `app.startup` alerts the owner before the first broker call. A security
  control that can be turned off silently by one environment variable is a
  control nobody can audit after the fact.
- Raises `ConfigError` naming the offending variable when: a required variable is
  missing or empty; a numeric value is out of range; `POSITION_SIZE_PCT` exceeds
  `MAX_POSITION_PCT`; `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeds 100;
  `TAKE_PROFIT_PCT` is not greater than `STOP_LOSS_PCT`; `WATCHLIST` is empty;
  or `ML_MODEL_PATH` is set but unreadable.
- Must never substitute a default for a missing **risk** variable.
- Must never include a token value in an exception message or in `__repr__`.
- Called before any other module is initialised.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

15. **Configuration missing or invalid at startup** → refuse to start, alert if
    Telegram credentials are among the valid ones, sleep 30 seconds, exit
    non-zero. The sleep exists so the container restart policy cannot produce an
    alert loop.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception.

## Expected output

- `zarabot/config.py` implementing the contract exactly
- `tests/test_config.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_config.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/config.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
