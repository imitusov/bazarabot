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
- **Closed days are rows too (v1.81).** `is_trading_day = 0` with both timestamp
  columns null is a complete, expected row, not a degenerate one — the columns
  have been `NULL` with the note above since `006_trading_days.sql`, and the
  table was designed for it from the start. **No migration is required**: the
  amendment changes which rows are written, not what a row may hold.
  Until v1.81 no such row was ever written, because the `SessionInfo` the writer
  was handed had no date to key one on (#51).
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
- **Every day of the window is recorded, closed days included (v1.81).** The row
  is keyed on `SessionInfo.trade_date`, which is present on every entry, so a
  non-trading day is written with `is_trading_day = 0` and
  `session_start`/`session_end` null. This is what the table was built for: §5
  has declared both columns `TEXT NULL`, "Null on a non-trading day", since
  `006_trading_days.sql`, and no migration is required to start using them.
- **"Days with no date are skipped rather than stored under a null key" is
  withdrawn (v1.81).** Its premise — that a closed `SessionInfo` carried no date
  — is gone: `trade_date` is a `date`, never `None`. The clause was correct for
  the type as it stood and was the mechanism by which the table held only trading
  days, so `earliest()` reported the oldest recorded *trading* day and
  `market.session.covers` was `False` for a closed day the bot had in fact
  observed. There is nothing left for the skip to catch, and a `SessionInfo` that
  reaches this function without a date is a construction that `models` refuses,
  not an input to filter.
- Runs in a single transaction, so a partial window is never recorded.

**`async list_since(start: date) → list[SessionInfo]`**
- Recorded days from `start` onwards, oldest first. Empty list when none, never
  `None`.
- **Reconstructs `trade_date` from the `trade_date` column, not from
  `session_start` (v1.81).** For a trading day the two agree by the producer
  obligation in §4 `models`; for a closed day there is no `session_start` to
  derive anything from, which is exactly the case this repository now returns.
- Returns closed days as well as trading ones, so the round trip through this
  table is lossless: what `record_many` was given is what comes back.
  `clock.trading_days_between` is unaffected — it counts entries with
  `is_trading_day` true and a non-null `start`, and closed entries satisfy
  neither.

**`async earliest() → date | None`**
- The oldest recorded date, or `None` when the table is empty. This is what
  `market.session.covers` is built on.
- **It is the oldest recorded *calendar* day, not the oldest recorded trading
  day (v1.81).** Recording closed days moves this backwards, never forwards, so
  `covers` becomes true for days it was previously false for and false for none
  it was previously true for. That is a correction, not a loosening: a Saturday
  at the head of an observed window was a day the bot had been told about and
  written down, and reporting it as uncovered understated what the calendar
  knows.

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
- **"A window containing a day with no date records the rest and skips it" is
  withdrawn (v1.81).** `SessionInfo.trade_date` is a `date` with no default and
  no null, so the input that case described can no longer be constructed —
  `models` refuses it, and §3.2 `models` pins that refusal. It is withdrawn
  rather than deleted silently because it was the mechanism by which this table
  held trading days only, and a reader finding it gone needs to know it was
  replaced by a stronger check upstream, not dropped to make a suite pass.
- A window containing closed days records **every** day, closed ones with
  `is_trading_day` 0 and both timestamp columns null, and reads them all back
  (v1.81; proves the round trip is lossless and that the schema needed no
  migration to hold a closed day — §5 declared both columns `NULL` in `006`).
- A closed day read back reports its `trade_date` from the `trade_date` column
  (v1.81; proves the date is not reconstructed from `session_start`, which is
  null on exactly the rows that made #51 invisible).
- `earliest()` over a window whose first day is a **Saturday** returns that
  Saturday (v1.81; proves coverage begins at the oldest recorded calendar day,
  not the oldest recorded trading day — `market.session.covers` was false for a
  day the bot had been told about and written down).
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
