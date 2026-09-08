# Task 31/42: Implement `zarabot/ops/backup.py`

## Product context

Nightly database backup. A failure alerts but never stops trading.

## Build order position

Module **31** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/ops/backup.py`

**`async run(db_path: Path, backup_dir: Path) → Path`** — produces a consistent copy using SQLite's own backup mechanism, never a raw file copy of a live database.
- **On success, emit `backup_ok` (INFO) with `path` and `bytes` (v1.61).** On
  failure, emit `backup_failed` (ERROR) with `error`, alert, and return; it must
  never stop trading.

**`async prune(backup_dir: Path, retention_days: int) → int`** — removes backups strictly older than the window; returns the count removed.

- Failure alerts and returns; it must never stop trading.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

18. **Backup failure** → ERROR, alert, trading continues. A missing backup is not
    worth stopping trading over; it is worth knowing about.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A backup produces a file that opens as a valid database containing the same
  rows (proves the copy is consistent, not a torn file).
- Backups older than the retention window are removed and newer ones are kept
  (boundary).
- A failing backup alerts, emits `backup_failed`, and does not stop trading
  (v1.61).
- A successful backup emits `backup_ok` with `path` and `bytes`.

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
