"""Nightly SQLite backup. Failure alerts; trading continues."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zarabot.clock import now
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_PREFIX = "zarabot-"
_SUFFIX = ".db"

# The alert latch (spec §4 `ops.backup`, v1.86). `app.loops` runs the backup at
# most once per Moscow day, so an unlatched failure would alert every night for
# as long as it stays broken — into a channel whose whole premise is that
# silence means healthy, which is an alert equivalent to no alert. `run` alerts
# only while this is False and sets it True immediately after sending; the ONLY
# reset is the success path in `run`, a destination written *and* verified.
# Process-local by contract: a restart re-arms it.
_backup_alerted: bool = False


class _VerificationFailed(Exception):
    """The destination landed and could not be read back (spec §4, v1.86).

    Internal to this module. `run` turns it into the existing `backup_failed`
    record with `error` = `verification` — never a new §7.1 event name, which
    would oblige `scripts/ci/check_events.py` and the hardcoded `KNOWN_EVENTS`
    frozenset in `scripts/deploy/export_health.py` in the same change.
    """


def _dest_path(backup_dir: Path) -> Path:
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    return backup_dir / f"{_PREFIX}{stamp}{_SUFFIX}"


def _copy(db_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _max_schema_version(conn: sqlite3.Connection) -> object:
    """`MAX(version)`, or `None` when `schema_version` holds no rows.

    An aggregate always answers with exactly one row, so there is no "no row"
    case to guard: a missing table raises `sqlite3.OperationalError`, and an
    empty table answers `(None,)`. A guard for a row that cannot be absent
    would be a branch no test can reach.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    value: object = row[0]
    return value


def _verify(db_path: Path, dest: Path) -> None:
    """Prove the destination is a readable copy of this database (v1.86).

    Copying a file and asserting the copy exists verifies the filesystem, not
    the backup. Three checks, all on a **separate, read-only** connection —
    `mode=ro`, in this same worker thread — so that verifying cannot journal
    into the very file an operator would later restore:

    1. `PRAGMA integrity_check` returns exactly one row whose value is `ok`;
    2. `SELECT MAX(version) FROM schema_version` is not null and equals the
       same read on the source;
    3. `SELECT COUNT(*) FROM positions` completes.

    (3) is compared against nothing. The source is live and may legitimately
    have moved on between the copy and the check, so an equality assertion
    there would fail on a working system; what it proves is that the
    destination carries this project's schema and that its pages are reachable
    through it.

    These are reads, licensed here exactly as `Connection.backup` is: this
    module deliberately opens its own synchronous connections rather than
    `db.connection.shared()`, so rule 30 does not apply to it. `db.migrations`
    remains the sole writer of `schema_version` and `db.positions` of
    `positions`. The source is opened read-write like `_copy` opens it, because
    the live database is in WAL mode and a `mode=ro` connection to a WAL file
    needs to write its shared-memory index.

    Raises `_VerificationFailed` when any one of the three is false, or when a
    `sqlite3.Error` or `OSError` is raised while checking.
    """
    try:
        uri = f"{dest.absolute().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as copy:
            rows = copy.execute("PRAGMA integrity_check").fetchall()
            if rows != [("ok",)]:
                raise _VerificationFailed(f"integrity_check returned {rows!r}")
            version = _max_schema_version(copy)
            if version is None:
                raise _VerificationFailed("destination has no schema_version row")
            with closing(sqlite3.connect(str(db_path))) as source:
                expected = _max_schema_version(source)
            if version != expected:
                raise _VerificationFailed(
                    f"destination schema_version {version!r} is not the "
                    f"source's {expected!r}"
                )
            copy.execute("SELECT COUNT(*) FROM positions").fetchone()
    except (sqlite3.Error, OSError) as exc:
        raise _VerificationFailed(f"{type(exc).__name__}: {exc}") from exc


def _copy_and_verify(db_path: Path, dest: Path) -> None:
    """The copy and its proof, in one worker thread (spec §4, v1.86).

    The contract puts the read-only connection "in the same worker thread" as
    the copy, and `sqlite3` connections are thread-affine, so the two run under
    one `asyncio.to_thread` rather than two.
    """
    _copy(db_path, dest)
    _verify(db_path, dest)


def _discard(dest: Path) -> bool:
    """Remove a destination that must never be restored from (v1.86).

    Not tidiness. `prune` selects by mtime alone, so an unreadable file written
    tonight is the newest thing in `backup_dir` and survives the entire
    retention window — and a restore would prefer it precisely for being
    newest. This also closes the pre-existing defect that `_copy` opens the
    destination *before* the copy runs, so an `OSError` part-way through leaves
    a partial file with a fresh mtime.

    Returns False when the delete itself raised `OSError`. That is not a
    swallow: it is the one case an unusable file stays in `backup_dir`, and the
    caller makes it greppable by putting `path` on the `backup_failed` record
    and names the file in the alert so the operator can remove it by hand.
    """
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        return False
    return True


async def _report_failure(dest: Path, error: str, exc: BaseException) -> None:
    """Delete the destination, emit `backup_failed`, alert if not latched."""
    global _backup_alerted
    removed = await asyncio.to_thread(_discard, dest)
    extra: dict[str, object] = {"event": "backup_failed", "error": error}
    if not removed:
        extra["path"] = dest.name
    # Emitted on EVERY failing run, latched or not: the log stays complete and
    # only the Telegram alert is rationed.
    _LOG.error("backup_failed", exc_info=exc, extra=extra)
    if _backup_alerted:
        return
    if removed:
        text = f"Database backup failed for {dest.name} ({error}). Trading continues."
    else:
        text = (
            f"Database backup failed for {dest.name} ({error}), and the unusable "
            f"file could not be removed from {dest.parent} — delete it by hand "
            "before any restore. Trading continues."
        )
    await alert(text)
    _backup_alerted = True


def _prune_sync(backup_dir: Path, retention_days: int) -> int:
    if not backup_dir.is_dir():
        return 0
    cutoff = now() - timedelta(days=retention_days)
    removed = 0
    for path in backup_dir.glob(f"{_PREFIX}*{_SUFFIX}"):
        if not path.is_file():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


async def run(db_path: Path, backup_dir: Path) -> Path:
    """Consistent SQLite backup, verified before it counts. Never raises.

    Never a raw copy of a live file, and success means written *and* verified
    (v1.86): a copy that lands and cannot be read back is a failure, not a
    success with a caveat. A destination that fails the proof is deleted.
    """
    global _backup_alerted
    dest = _dest_path(backup_dir)
    try:
        await asyncio.to_thread(_copy_and_verify, db_path, dest)
    except _VerificationFailed as exc:
        # A backup that cannot be read back did fail, so this is the existing
        # `backup_failed` row with `error` = `verification` (v1.86), never a
        # new §7.1 event name. `error` therefore takes two kinds of value: the
        # exception class name when something raised during the copy, and this
        # literal when the destination answered wrongly or raised while being
        # read back.
        await _report_failure(dest, "verification", exc)
        return dest
    except (sqlite3.Error, OSError) as exc:
        # Rule 18 (v1.75): "A backup failure is `sqlite3.Error` or `OSError`,
        # and only those" — the database refusing the copy, and the filesystem
        # refusing the destination. Every other exception propagates under
        # rule 21 rather than being reported as "backup failed", which sends
        # the reader to look at the disk when the fault is in the code. Only
        # the copy reaches this clause: `_verify` converts both classes into
        # `_VerificationFailed` above.
        await _report_failure(dest, type(exc).__name__, exc)
        return dest
    # The only reset: a destination written AND verified. A copy that succeeds
    # and then fails verification is not a success and does not clear it.
    _backup_alerted = False
    try:
        size = dest.stat().st_size
    except OSError:
        size = 0
    _LOG.info(
        "backup_ok",
        extra={
            "event": "backup_ok",
            "path": dest.name,
            "bytes": size,
        },
    )
    return dest


async def prune(backup_dir: Path, retention_days: int) -> int:
    """Remove backups strictly older than retention_days. Returns how many."""
    return await asyncio.to_thread(_prune_sync, backup_dir, retention_days)
