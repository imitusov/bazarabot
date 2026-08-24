# Task 5/40: Implement `zarabot/db/migrations.py`

## Product context

Schema creation and version tracking. Forward-only: a bad migration is fixed by a new one, never by rolling back a database holding real trade history.

## Build order position

Module **5** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `schema_version`

| Column | Type | Notes |
|---|---|---|
| `version` | INTEGER | Primary key. Highest row is the current version |
| `applied_at` | TEXT | UTC ISO-8601 |

## Module contract

### `zarabot/db/migrations.py`

Owns schema creation and version tracking.

**`async apply(conn: Connection) → int`**
- Applies every migration whose version exceeds the database's recorded version,
  in ascending order, each in its own transaction.
- Returns the resulting schema version.
- Raises `MigrationError` if the recorded version exceeds the highest known
  migration, and makes no modification in that case.
- Idempotent: applying twice is a no-op the second time.
- Called by `app.startup` before any repository function, on
  `db.connection.shared()`. This module never calls `aiosqlite.connect` and never
  closes the connection it is given.
- At the start of `apply`, issues `PRAGMA foreign_keys = ON`,
  `PRAGMA busy_timeout = 30000` and `PRAGMA journal_mode = WAL` on that
  connection. `journal_mode` is persistent; the other two are per-connection and
  must be re-issued on every connection, which is why they appear both here and
  in `db.connection.connect`. Setting them here means a test that passes its own
  connection still gets WAL and foreign-key enforcement.
- **Ships `003_position_events.sql`**, which creates the `position_events` table
  that `db.positions` owns. The migration file belongs to this module even though
  the table belongs to that one: `migrations/` is this module's directory, and a
  table specified in §5 with no named file owner reaches no task at all.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

16. **Schema version ahead of the code** → refuse to start, alert, change nothing.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Applying migrations to an empty database creates every table **and leaves
  `schema_version` at the highest migration present in `migrations/`**, asserted
  against the files on disk rather than against a literal (happy path). Asserting
  only that the tables exist passes even when the driver stops after `001`, since
  `001` creates every table and later migrations only add columns — so the case
  must also assert a column a later migration introduces, currently
  `orders.exit_trigger`.
- After `apply`, `PRAGMA journal_mode` reports `wal` and `PRAGMA foreign_keys`
  reports `1` on the connection it was given (proves the pragmas live on the
  connection rather than in a comment — the gap that left every declared foreign
  key decorative at runtime).
- Applying migrations twice makes no changes the second time and does not raise
  (proves idempotency).
- A database at version N−1 is migrated to N without data loss in existing rows
  (proves forward migration preserves history).
- A database whose recorded version is **higher** than the code's raises
  `MigrationError` and does not modify anything (proves a rolled-back deployment
  cannot silently corrupt a newer schema).

## Expected output

- `zarabot/db/migrations.py` implementing the contract exactly
- `tests/test_db_migrations.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_migrations.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/migrations.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
