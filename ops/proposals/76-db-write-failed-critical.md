# Proposal: `db_write_failed.critical` is a caller argument, not a regex

**Kind:** spec defect. v1.61 hardcoded `critical` true. v1.62 (#77) derived it
from the table name in the SQLite error. That still fails for the errors that
happen in production: disk full, `database is locked`, I/O error, and
`no such column` / datatype mismatch name **no table**, so they become
`unknown` and report `critical` true — including on rule-12 writes. The regex
works for UNIQUE/NOT NULL that tests construct and fails for a disk.

**Should say (in §4 `db.connection.transaction()`):** `transaction(*, critical:
bool = True)`. Emit `db_write_failed` with `table` still parsed from the error
(`unknown` when unnamed) and `critical` from **that argument**. Default true.
The exception still leaves `transaction()`; the repository catch is what stops
it reaching the trading loop.

**Do not:** infer criticality from table-name regex. The owner of the write
already knows which rule it is on.

**Callers (after v1.63):**
- Rule 11, including `db.cooldowns`: omit the argument or pass `critical=True`.
  A lost cooldown row lets the bot re-enter a ticker it just exited.
- Rule 12 (`db.signals`, `db.snapshots`; instruments cache when it writes):
  pass `critical=False`.

**Test contract:** `transaction(critical=False)` plus an `aiosqlite.Error` that
names no table still emits `critical` false. Default / `critical=True` emits
true. A rule-12 repository failure still produces exactly one `db_write_failed`.

**Modules to re-run:** `5b-db-connection` (#76 rebase onto v1.64), then
`db.signals` and `db.snapshots` (`critical=False`). `db.cooldowns` stays
default-true; do not pass `False`.

**Sequencing:** v1.62 already landed. #76 currently implements v1.62; **hold
#76 until v1.64** replaces table-derivation with this argument, then rebase.
Do not merge #76 against v1.62.
