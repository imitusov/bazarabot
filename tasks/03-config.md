# Task 3/40: Implement `zarabot/config.py`

## Product context

Loads and validates every setting once at startup. The bot refuses to start rather than trade on an assumed risk limit.

## Build order position

Module **3** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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
- Adds `allow_foreign_holdings`, defaulting to **false**. The trading account is
  the bot's alone (brief v1.8); this flag is the owner's explicit acknowledgement
  that it is not, and it is deliberately awkward to set by accident. It is not a
  risk limit, so a missing value takes its default.
- `ssl_tbank_verify` defaults to true. **Setting it false must be loud**:
  `config.load()` logs a CRITICAL line naming the risk, because it disables
  certificate verification on the connection carrying the trading token. A
  security control that one environment variable can switch off silently is a
  control nobody can audit after the fact. `app.startup` raises the matching
  alert — see its own contract.
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

32. **The broker reports a holding the bot has no record of at startup** →
    refuse to start, alert, and name every ticker, unless
    `config.allow_foreign_holdings` is true. The account is the bot's alone
    (brief v1.8). The bot cannot distinguish "someone bought this by hand" from
    "local state is wrong", and both readings forbid trading it. When the flag is
    set, the holdings are named in the ready alert and are never traded: no stop
    placed, no exit evaluated, no sale made. Never adopt one — adoption derived a
    stop and target from the holding's average cost, which handed the next cycle
    a position already past its take-profit.

---

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
