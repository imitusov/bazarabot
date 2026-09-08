# Task 8b/42: Implement `zarabot/db/job_runs.py`

## Product context

Sole owner of job_runs. Periodic jobs ask has_run so a restart cannot double-send the weekly report or skip a backup forever.

## Build order position

Module **8b** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `job_runs`

When each periodic job last completed, per period. Owned by `db.job_runs`.

| Column | Type | Notes |
|---|---|---|
| `job` | TEXT | Part of the primary key. `rollover`, `backup`, `weekly_report`, `schedule_refresh`, `heartbeat` |
| `period_key` | TEXT | Part of the primary key. A Moscow date for a daily job, a week-start date for the weekly report |
| `ran_at` | TEXT NOT NULL | UTC. The moment the job **first** completed for that period |

**Invariants.**
- Primary key `(job, period_key)`. A second completion for the same period keeps
  the first `ran_at`: when it first ran is the fact worth having.
- Rows are never deleted. Growth is a handful of rows a day.
- It exists because this state was process-local, so every restart re-armed
  every job — three restarts in a day meant three heartbeats, and a restart
  through the report hour lost the week silently (#27).

## Module contract

### `zarabot/db/job_runs.py`

**Sole owner of the `job_runs` table.** No other module writes it, and no SQL
for it lives anywhere else.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; every write runs inside
`db.connection.transaction()` (rule 31).

**`async has_run(job: str, period_key: str) → bool`**
- Whether `job` has completed for that period. `period_key` is whatever
  identifies the period the caller schedules on — a Moscow date for a daily job,
  a week-start date for the weekly report.

**`async mark_run(job: str, period_key: str, ran_at: datetime) → None`**
- Records completion. Idempotent: a second call for the same pair keeps the
  first `ran_at`, since when the job *first* completed is the fact worth having.
- Raises `ValueError` on a naive `ran_at`.

**`async last_run(job: str) → datetime | None`**
- The most recent completion of `job`, or `None`. This is what makes "did the
  weekly report go out?" answerable from the database, which it was not while
  the answer lived in a module global (#27).

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A job marked run reports `has_run` true for that period and false for the
  next (happy path).
- Marking the same pair twice keeps the first `ran_at` (proves the record is
  *when it first completed*, which is what makes a late run distinguishable
  from a repeated one).
- `last_run` on a job that has never run returns `None` (proves "did the weekly
  report go out?" is answerable, including when the answer is no).
- A naive `ran_at` raises `ValueError`.
- The module calls `aiosqlite.connect` nowhere and issues no `BEGIN`, `commit`
  or `rollback`.

## Expected output

- `zarabot/db/job_runs.py` implementing the contract exactly
- `tests/test_db_job_runs.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_job_runs.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/job_runs.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
