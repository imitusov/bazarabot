# Proposal: `db_write_failed.critical` must follow error rule 12

**Kind:** spec defect. v1.61 assigned `db_write_failed` to `db.connection` and
said `critical` is always true. Rule 12 names a non-critical write class
(signals, snapshots, instruments cache, and in code also cooldowns) that uses
the same `transaction()` and then swallows `aiosqlite.Error`. Those writes
would log `critical: true` and then continue trading — the field an operator
filters on for “this write stopped trading” would be a lie.

**Should say (in §4 `db.connection.transaction()`):** emit `db_write_failed`
with `table` as today, and `critical` taken from an argument
`transaction(*, critical: bool = True)`. Default remains true (orders,
positions, stops). Rule-12 callers pass `critical=False`. The exception still
propagates out of `transaction()`; the repository’s existing catch is what
stops it reaching the trading loop.

**Do not:** infer criticality from table-name regex in `db.connection`. The
owner of the write already knows which rule it is on.

**Test contract:** a failed insert with `critical=False` emits the event with
that flag and still raises into the caller’s `except aiosqlite.Error`. A
failed insert with the default emits `critical` true.

**Modules to re-run after amendment:** `5b-db-connection` (this PR #76,
rebase), then `db.signals`, `db.snapshots`, `db.cooldowns` (pass
`critical=False`). Instruments cache if/when it writes through `transaction()`.

**Do not merge #76 until this amendment lands.**
