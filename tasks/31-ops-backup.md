# Task 31/46: Implement `zarabot/ops/backup.py`

## Product context

Nightly database backup. A failure alerts but never stops trading.

## Build order position

Module **31** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/ops/backup.py`

**`async run(db_path: Path, backup_dir: Path) → Path`** — produces a consistent copy using SQLite's own backup mechanism, never a raw file copy of a live database.
- **On success, emit `backup_ok` (INFO) with `path` and `bytes` (v1.61).** On
  failure, emit `backup_failed` (ERROR) with `error`, alert, and return; it must
  never stop trading.
- **Success means written *and verified* (v1.86).** A copy that lands and cannot
  be read back is a failure, not a success with a caveat. The destination is
  proved readable before `backup_ok` is emitted, and a destination that fails
  that proof is deleted. The three checks, the deletion, the `error` value and
  the alert latch are specified below.

**`async prune(backup_dir: Path, retention_days: int) → int`** — removes backups strictly older than the window; returns the count removed.

- Failure alerts and returns; it must never stop trading.

**This module owns rule 18 (v1.75), and the catch is narrow.** A backup failure
is `sqlite3.Error` or `OSError` and only those: the database refusing the copy,
or the filesystem refusing the destination — a full disk, a missing mount, a
permission. Note `sqlite3`, not `aiosqlite`: this path deliberately opens its own
synchronous connections in a worker thread rather than using the shared one, so
rule 30 does not apply to it. **Every other exception propagates** under rule 21
rather than being reported as "backup failed", which is a message that sends the
reader to inspect a disk when the fault is in the code.

**Verification is part of `run`, and it is what makes the copy a backup
(v1.86).** Copying a file and asserting the copy exists verifies the filesystem,
not the backup. Before `run` may emit `backup_ok` it opens the destination as a
**separate, read-only** `sqlite3` connection — `mode=ro`, in the same worker
thread — and proves three things:

1. `PRAGMA integrity_check` returns exactly one row whose single value is the
   string `ok`.
2. `SELECT MAX(version) FROM schema_version` on the destination is not null and
   **equals** the same read on the source.
3. `SELECT COUNT(*) FROM positions` on the destination completes.

All three must hold. The count in (3) is compared against nothing: the source is
live and may legitimately have moved on between the copy and the check, so an
equality assertion there would fail on a working system. What (3) proves is that
the destination carries this project's schema and that its pages are reachable
through it. Any one of the three false, or a `sqlite3.Error` or `OSError` raised
while checking, is a **verification failure**.

This catches what the issue reports it should: a truncated or torn destination,
a copy interrupted by a full disk that returned without raising, a filesystem
that silently discarded writes, and a destination that is not this project's
database.

**These are reads, and they are licensed here exactly as `Connection.backup` is
(v1.86).** This module already opens its own synchronous connections instead of
`db.connection.shared()`, and rule 30 does not apply to it; verification adds
three read statements on that same footing and writes nothing. `db.migrations`
remains the sole writer of `schema_version` and `db.positions` the sole writer of
`positions`. The destination is opened **read-only** so that verifying cannot
journal into the very file an operator would later restore.

**A failed destination is deleted (v1.86).** On a verification failure, and on a
copy failure, `run` removes `dest` before it returns. This is not tidiness.
`prune` selects by mtime alone, so an unreadable file written tonight is the
newest thing in `backup_dir` and survives the entire retention window — and a
restore would prefer it precisely for being newest. Deleting also closes a
pre-existing defect: `_copy` opens the destination *before* the copy runs, so an
`OSError` part-way through leaves a partial file with a fresh mtime that `prune`
then preserves for thirty days.

If the delete itself raises `OSError` it is caught, `run` still returns, and the
`backup_failed` record additionally carries `path`. That is the one case where an
unusable file stays in `backup_dir`, so it is the one case that must be greppable
and must be named in the alert; the operator removes it by hand. `run` never
raises, on any of these paths.

**A verification failure is `backup_failed` with `error` = `verification`, not a
new event name (v1.86).** A backup that cannot be read back did fail, so the
existing row is the honest one; it is also the cheap one. A new §7.1 row obliges
two places nobody would think to re-run in the same change: its owning module
must emit it (`scripts/ci/check_events.py`) and it must be added to the hardcoded
`KNOWN_EVENTS` frozenset in `scripts/deploy/export_health.py`, which exits
non-zero on a name it does not know. `error` therefore takes two kinds of value —
the exception class name when something raised, and the literal `verification`
when the destination opened and answered wrongly.

**The alert latch is `_backup_alerted`, and this names its reset (v1.86).** The
backup job runs at most once per Moscow day: `app.loops` asks
`db.job_runs.has_run("backup", …)` before running and calls `mark_run` after, so
a restart can neither re-run it nor lose it, and **nothing about scheduling
changes here.** Unlatched, a backup that stays broken would therefore alert every
night for as long as it stays broken, into a channel whose whole premise is that
silence means healthy. That is an alert equivalent to no alert.

- `_backup_alerted: bool = False` at module level.
- `run` alerts on a failure **only while it is `False`**, and sets it to `True`
  immediately after sending.
- `run` sets it back to `False` on the success path — a destination written
  **and verified**. That is the only reset. A copy that succeeds and then fails
  verification is not a success and does not clear it.
- The `backup_failed` ERROR record is emitted on **every** failing run, latched
  or not. The log stays complete; only the Telegram alert is rationed.

**The latch is process-local, and that bound is deliberate (v1.86).** A restart
re-arms it, so a persistently failing backup alerts once per process rather than
once ever. Making it durable would mean a row in a table this module does not
own, and restarts are not frequent enough for the extra alert to be noise.
Nothing above is a claim that the owner is told exactly once.

**What verification does not prove (v1.86).** Written out because a check
described as proving more than it proves is the defect this project has produced
most often.

- **Not row-level completeness.** `integrity_check` reads every page and proves
  the b-trees are internally consistent. It compares nothing against the source.
  A row the copy is missing passes, provided the file is otherwise well-formed.
- **Not correctness.** A database that is structurally perfect and logically
  wrong — a position that never closed, a P&L that double-counts — is copied
  faithfully and verified happily. Verification is about the file, never about
  whether the record in it is true.
- **Not durability after tonight.** The check runs once, on the file just
  written. Nothing ever re-reads an older backup, and `prune` deletes by mtime
  without opening anything, so bit-rot in last week's backup stays invisible
  until someone tries to restore it.
- **Not the restore.** Verification proves a file opens; it does not prove the
  bot starts against it. An untested restore is not a backup. Writing and
  rehearsing that procedure belongs to `environment-setup.md` and is not done
  here.
- **Not volume loss, which is the severity #25 actually reports.** The database
  and every backup sit on one mount (§10; brief §17). Verification proves the
  copy on that mount is readable and does nothing whatever about losing the
  mount. No code in this module can. See the open decision below.

**Open decision — where backups live (v1.86). Not settled here.** #25 proposes
pushing the nightly backup to S3-compatible object storage. That is not available
as written. `business-brief.md` §10 says the database is unencrypted because an
on-server key would protect against nothing realistic, and then: "Backups inherit
the same posture and must not be placed in cloud storage that the owner would not
be comfortable with a stranger reading." The backups are unencrypted and hold the
whole trading record — the strategy attribution and signal history that exist in
exactly one place and are not reconstructible from the broker (brief §11). The
brief takes precedence over this document, so where backups live is a
data-at-rest decision for the owner, not an implementation detail an agent may
settle. The options, with what each actually buys:

(a) **One volume, as today.** Verified copies beside the database. Covers
    logical corruption of the database file. Covers nothing about the volume.
(b) **A second mount on the same host** — an attached block device or a second
    filesystem, same posture, no third party reads anything. Survives corruption
    of the `/data` filesystem and an accidental `docker volume rm`. Does not
    survive losing the host or the provider.
(c) **An offsite target the owner controls** — rsync over ssh to a machine the
    owner owns, or the provider's own snapshots. Survives host loss. A provider
    snapshot is still storage a stranger could in principle read, so it is the
    same brief question in a different shape and has to be answered knowingly
    rather than by default.
(d) **Encrypted offsite.** Encrypt before upload and the brief's objection
    disappears, because the stranger reads ciphertext. It reintroduces the key
    problem the brief already reasoned through: a key on the server protects
    against nothing, and a key held only off it is one more thing whose loss
    destroys the backups.

**Until it is decided, (a) is binding.** No agent adds a second destination, an
upload, a credential or an encryption step on its own reading of this paragraph.
Whichever way it goes the change is not in this module alone: `docker-compose.yml`
and `docker-compose.deploy.yml` each mount exactly one volume today, §10 describes
that one volume, and (c) or (d) add configuration that must be read through
`config` and must never be logged.

**Open decision — the retention window and a long copy (v1.86). Not settled
here.** `prune` deletes everything older than the window and the window is thirty
days, so the oldest recoverable state of the trading record is thirty days back.
Whether that is right is a product judgement about how far back the owner would
ever want to reconstruct — a tax question, a broker dispute, a strategy
post-mortem — traded against disk on a 2 GB VPS. Either (a) thirty days of
dailies and nothing older, as today; or (b) thirty days of dailies plus a
retained weekly or monthly copy, which requires `prune` to select by something
other than mtime and is therefore a contract change, not a change of constant.
**Until it is decided, (a) is binding** and `prune`'s contract above is
unchanged.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

18. **Backup failure** → ERROR, alert, trading continues. A missing backup is not
    worth stopping trading over; it is worth knowing about.

    **A backup failure is `sqlite3.Error` or `OSError`, and only those
    (v1.75).** `ops.backup` runs `sqlite3.Connection.backup` in a worker thread
    over a live database file, so those two classes are the whole surface it can
    legitimately fail on: the database refusing the copy, and the filesystem
    refusing the destination — a full disk, a missing mount, a permission. Note
    it is `sqlite3`, not `aiosqlite`: this path deliberately opens its own
    synchronous connections rather than the shared one, and rule 30 does not
    apply to it. **Every other exception propagates** under rule 21 rather than
    being reported to the owner as "backup failed", which is a message that sends
    the reader to look at the disk when the fault is in the code.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A backup produces a file that opens as a valid database containing the same
  rows (proves the copy is consistent, not a torn file).
- Backups older than the retention window are removed and newer ones are kept
  (boundary).
- A failing backup alerts, emits `backup_failed`, and does not stop trading
  (v1.61).
- A successful backup emits `backup_ok` with `path` and `bytes`.
- A destination whose `PRAGMA integrity_check` does not return `ok` is
  **deleted**, emits `backup_failed` with `error` = `verification`, alerts, and
  does not stop trading (v1.86). Asserting the file is *gone* is the point of
  the case: `prune` keeps by mtime, so a surviving unreadable destination is the
  newest thing in `backup_dir` for the whole retention window.
- A destination that passes `integrity_check` but whose `schema_version` does
  not match the source is deleted and reported the same way; so is one in which
  `positions` cannot be read (v1.86). Two tests, one shape — they prove the
  check is against *this* database at *this* schema, not against any file that
  happens to be valid SQLite.
- A destination that verifies is byte-identical before and after verification
  and is still on disk when `run` returns (v1.86) — proves the check opens
  read-only, and that a passing check deletes nothing.
- A copy that raises part-way leaves **no** file behind (v1.86).
  `sqlite3.connect` creates the destination before `Connection.backup` writes a
  page, so the failure path always has a partial file to remove.
- The second consecutive failing run emits `backup_failed` and sends **no**
  alert; a run that verifies clears the latch, and the next failure alerts again
  (v1.86) — proves `_backup_alerted` and its reset. The job is nightly, so an
  alert on every failing night is equivalent to no alert.

## Expected output

- `zarabot/ops/backup.py` implementing the contract exactly
- `tests/test_ops_backup.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_ops_backup.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/ops/backup.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
