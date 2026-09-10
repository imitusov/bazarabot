# Task 11/42: Implement `zarabot/db/snapshots.py`

## Product context

Daily equity snapshots. The opening baseline is what the daily loss limit measures against.

## Build order position

Module **11** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `daily_snapshots`

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. **Moscow** calendar date |
| `opening_equity` | TEXT NOT NULL | Baseline for the daily loss limit |
| `closing_equity` | TEXT NULL | Null until the session closes |
| `cash` | TEXT NOT NULL | |
| `realised_pnl` | TEXT NOT NULL | For the day |
| `unrealised_pnl` | TEXT NOT NULL | At snapshot time |
| `open_positions` | INTEGER NOT NULL | |
| `orders_placed` | INTEGER NOT NULL | Observational only — there is no daily cap |
| `benchmark_value` | TEXT NULL | Null when unavailable, never 0 |

## Module contract

### `zarabot/db/snapshots.py`

**Sole owner of `daily_snapshots` rows.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async write_daily(snapshot: DailySnapshot) → None`** — upserts on the Moscow date; a second write for the same date updates rather than duplicates.

**`async list_for_period(start: date, end: date) → list[DailySnapshot]`** — rows
whose `trade_date` falls in `[start, end]`, oldest first. For the weekly report.
Empty list when none.

**Owns rule 12 (v1.75, split v1.76).** Signals, snapshots and the instruments
cache are the non-critical write paths: a failed write here is logged at ERROR
and does not propagate, because losing an analytics row must not stop trading.
The swallow is `aiosqlite.Error` and nothing wider — every other exception
propagates and reaches rule 21's supervisor with its traceback. An analytics
path is where a silently dropped `TypeError` survives longest, since nothing
downstream misses the row until a weekly report is composed without it. Contrast
`db.cooldowns` above, which is rule 11 and propagates everything.

**Why this heading was split (v1.76).** Until v1.76 these two modules shared one
`###` heading, and `list_for_period` was written once as
`→ list[...]` because the elision was standing for two different real return
types — `list[tuple[Signal, RiskDecision]]` here and `list[DailySnapshot]` there
(#116). One heading cannot carry two signatures of the same name: the signature
comparison in `scripts/ci/check_docs.py` reads the last one and compares it
against both modules, so one of the two was guaranteed to be wrong and the
divergence lived in an allowlist instead. Splitting the heading is what makes
each signature exact, which is what `AGENTS.md` requires. `make_tasks.py` needs
no change: its spec-keys are already the two distinct file paths.

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

- A rejected signal is stored with its rejection reason and is retrievable by day
  (proves rejections are analysable, as the brief requires).
- A daily snapshot written twice for the same date updates rather than duplicates
  (proves the date is the key).

## Expected output

- `zarabot/db/snapshots.py` implementing the contract exactly
- `tests/test_db_snapshots.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_snapshots.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/snapshots.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
