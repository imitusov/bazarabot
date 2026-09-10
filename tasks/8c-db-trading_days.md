# Task 8c/42: Implement `zarabot/db/trading_days.py`

## Product context

Sole owner of trading_days. The cached exchange calendar, including days that later become holidays.

## Build order position

Module **8c** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `trading_days`

What the broker said about each calendar day, recorded when it said it. Owned by
`db.trading_days`.

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. Moscow calendar date, ISO-8601 |
| `is_trading_day` | INTEGER NOT NULL | 1 or 0 |
| `session_start` | TEXT NULL | UTC. Null on a non-trading day |
| `session_end` | TEXT NULL | UTC. Null on a non-trading day |
| `observed_at` | TEXT NOT NULL | UTC. When the broker was asked |

**Invariants.**
- A day is overwritten by a newer observation. A holiday can be announced after
  the fact, and the broker's most recent answer is the one to keep — unlike
  every other table here, where history is append-only, because this records
  *what is true about a date* rather than *what happened*.
- Rows are never deleted. The table grows by one row a day.
- It exists because the broker serves no schedule before today (§2.1), so the
  only way to know whether last Tuesday was a trading day is to have been told
  at the time and to have written it down (#45).

## Module contract

### `zarabot/db/trading_days.py`

**Sole owner of the `trading_days` table.** No other module writes it, and no
SQL for it lives anywhere else.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; every write runs inside
`db.connection.transaction()` (rule 31).

**`async record_many(sessions: list[SessionInfo]) → int`**
- Upserts one row per day on `trade_date`, returning how many were written. A
  day already recorded is **overwritten** by the newer observation: a holiday
  can be announced after the fact, and the most recent answer from the broker is
  the one to keep.
- Days with no date are skipped rather than stored under a null key.
- Runs in a single transaction, so a partial window is never recorded.

**`async list_since(start: date) → list[SessionInfo]`**
- Recorded days from `start` onwards, oldest first. Empty list when none, never
  `None`.

**`async earliest() → date | None`**
- The oldest recorded date, or `None` when the table is empty. This is what
  `market.session.covers` is built on.

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

- A window recorded then read back returns the same days, oldest first (happy
  path).
- Recording a day twice keeps the **later** observation, including a day that
  changes from trading to not (proves a holiday announced after the fact
  replaces the earlier answer — the one table here that is not append-only).
- `earliest()` on an empty table returns `None`, not a date (proves the caller
  can distinguish "no history" from "history starting today").
- A window containing a day with no date records the rest and skips it (proves
  a malformed entry cannot take the whole window down).
- The module calls `aiosqlite.connect` nowhere and issues no `BEGIN`, `commit`
  or `rollback` (proves it runs on the shared connection inside `transaction()`).

## Expected output

- `zarabot/db/trading_days.py` implementing the contract exactly
- `tests/test_db_trading_days.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_trading_days.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/trading_days.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
