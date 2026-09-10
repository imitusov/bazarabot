# Task 9/42: Implement `zarabot/db/cooldowns.py`

## Product context

Sole owner of per-instrument re-entry cooldowns, which replace a daily order cap as the runaway-loop protection.

## Build order position

Module **9** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `cooldowns`

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT | Primary key |
| `started_at` | TEXT NOT NULL | UTC, instant the position closed |

## Module contract

### `zarabot/db/cooldowns.py`

**Sole owner of cooldown timestamps.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async start(ticker: str, at: datetime) → None`** — records or overwrites with the newer instant.
- **A write failure propagates (v1.63).** This module catches no
  `aiosqlite.Error` and logs no failure of its own: cooldowns are rule 11, and
  `db.connection` already emits `db_write_failed` with `critical` true before
  re-raising. Until v1.63 the implementation swallowed the error and logged an
  unstructured line, which was behaviour the contract never granted — a lost
  cooldown then looked like a successful one and the bot could re-enter a ticker
  it had just exited.

**`async is_active(ticker: str, now: datetime, minutes: int) → bool`**
- True while `now - started_at < minutes`. Exactly at the boundary returns False.

**`async active_until(ticker: str, minutes: int) → datetime | None`** — for display in command replies.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

    **Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error`
    (v1.75)** The swallow exists for a database that will not take the row, not
    for every way the call site can be wrong. Any other exception propagates and
    reaches rule 21's supervisor with its traceback. Unqualified, this rule reads
    as `except Exception: pass` on the analytics path, and an analytics path is
    exactly where a silently dropped `TypeError` survives longest — nothing
    downstream misses the row until a weekly report is composed from it.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A ticker with no recorded cooldown is not in cooldown (happy path).
- A ticker whose cooldown started `COOLDOWN − 1 minute` ago is in cooldown
  (boundary, inside).
- A ticker whose cooldown started exactly `COOLDOWN` ago is **not** in cooldown
  (boundary, proves the interval is exclusive at the end).
- Starting a cooldown for a ticker already in cooldown extends it from the newer
  timestamp (proves the semantics of a re-entry after a rapid second close).

## Expected output

- `zarabot/db/cooldowns.py` implementing the contract exactly
- `tests/test_db_cooldowns.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_cooldowns.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/cooldowns.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
