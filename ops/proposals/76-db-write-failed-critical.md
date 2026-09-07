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

**Adopted:** spec v1.64 (`091f2a8`, #83) — `transaction(*, critical: bool = True)`.
Keep this file: it is why v1.62 (table regex) was wrong.

**Callers (v1.63+):**
- Rule 11, including `db.cooldowns`: default `critical=True`.
- Rule 12 (`db.signals`, `db.snapshots`; instruments cache when it writes):
  `critical=False`.

**Modules:** #76 rebases onto v1.64 (done). Signals/snapshots pass `False` there.

**Sequencing:** v1.62 (#77) landed and was superseded by v1.64. Do not implement
against table-derivation.
