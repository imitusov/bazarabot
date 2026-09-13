"""Tests for zarabot.ops.backup — written from technical-spec.md §3.2."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
from zarabot.ops import backup as backup_module
from zarabot.ops.backup import prune, run

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    monkeypatch.setattr("zarabot.ops.backup.now", lambda: NOW)
    # `_backup_alerted` is process-local by contract (v1.86), so it leaks
    # between tests unless each one starts armed. `raising=False` only so the
    # red commit fails on behaviour rather than on a missing attribute.
    monkeypatch.setattr(backup_module, "_backup_alerted", False, raising=False)
    return tmp_path


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Telegram alert `run` actually sent, in order."""
    sent: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        sent.append(text)

    monkeypatch.setattr("zarabot.ops.backup.alert", _alert)
    return sent


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if getattr(record, "event", None) == event
    ]


async def _seed(db_path: Path) -> None:
    """A source shaped like production: WAL, migrations applied.

    `db.connection.connect` sets `PRAGMA journal_mode = WAL`, and a WAL source
    produces a WAL destination — which is the file verification has to open
    read-only. A delete-mode fixture would prove the check against a database
    the bot never writes (failure class 9).
    """
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await apply(conn)
        await conn.commit()


def _after_copy(monkeypatch: pytest.MonkeyPatch, hook: Callable[[Path], None]) -> None:
    """Let the real copy run, then reach the destination before verification.

    This is the seam the contract cares about: a destination that landed and
    cannot be read back. Nothing about `_copy` is asserted — the hook stands in
    for a copy interrupted by a full disk that returned without raising, or a
    filesystem that silently discarded writes.
    """
    real = backup_module._copy

    def _copy_then_hook(db_path: Path, dest: Path) -> None:
        real(db_path, dest)
        hook(dest)

    monkeypatch.setattr(backup_module, "_copy", _copy_then_hook)


def _break_freelist(dest: Path) -> None:
    """Header claims a five-page freelist; the file holds none.

    `PRAGMA integrity_check` answers with one row that is not `ok` rather than
    raising, which is the branch the first §3.2 verification case names.
    """
    with dest.open("r+b") as handle:
        handle.seek(36)
        handle.write((5).to_bytes(4, "big"))


def _scramble_header(dest: Path) -> None:
    """Not a database any more: `integrity_check` raises `sqlite3.DatabaseError`."""
    with dest.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"NotSQLite")


def _bump_schema_version(dest: Path) -> None:
    with closing(sqlite3.connect(str(dest))) as conn:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (99, ?)",
            (NOW.isoformat(),),
        )
        conn.commit()


def _empty_schema_version(dest: Path) -> None:
    with closing(sqlite3.connect(str(dest))) as conn:
        conn.execute("DELETE FROM schema_version")
        conn.commit()


def _drop_positions(dest: Path) -> None:
    with closing(sqlite3.connect(str(dest))) as conn:
        conn.execute("DROP TABLE positions")
        conn.commit()


def _write_torn_database(path: Path) -> None:
    """A source that opens and then fails part-way through the page copy.

    `sqlite3.connect(dest)` creates the destination before `Connection.backup`
    writes a page, so this failure always has a partial file to remove.
    """
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE torn (a TEXT)")
        conn.executemany("INSERT INTO torn VALUES (?)", [("x" * 200,)] * 2000)
        conn.commit()
    with path.open("r+b") as handle:
        handle.truncate(path.stat().st_size // 2)


async def test_backup_opens_as_valid_database_with_same_rows(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    async with aiosqlite.connect(db_path) as conn:
        await apply(conn)
        await conn.execute(
            "UPDATE halt_state SET detail = 'backup-marker' WHERE id = 1"
        )
        await conn.commit()
    dest = await run(db_path, backup_dir)
    assert dest.is_file()
    async with aiosqlite.connect(dest) as copy:
        cursor = await copy.execute("SELECT detail FROM halt_state WHERE id = 1")
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "backup-marker"


async def test_prune_removes_older_than_retention_and_keeps_newer(
    env: Path,
) -> None:
    backup_dir = env / "backups"
    backup_dir.mkdir()
    old = backup_dir / "zarabot-old.db"
    boundary = backup_dir / "zarabot-boundary.db"
    recent = backup_dir / "zarabot-recent.db"
    for path in (old, boundary, recent):
        path.write_bytes(b"sqlite")
    old_ts = (NOW - timedelta(days=8)).timestamp()
    boundary_ts = (NOW - timedelta(days=7)).timestamp()
    recent_ts = (NOW - timedelta(days=1)).timestamp()
    os.utime(old, (old_ts, old_ts))
    os.utime(boundary, (boundary_ts, boundary_ts))
    os.utime(recent, (recent_ts, recent_ts))
    removed = await prune(backup_dir, 7)
    assert removed == 1
    assert not old.exists()
    assert boundary.exists()
    assert recent.exists()


async def test_failing_backup_alerts_emits_backup_failed_and_does_not_stop_trading(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr("zarabot.ops.backup.alert", _alert)
    missing = env / "missing-parent" / "zarabot.db"
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(missing, env / "backups")
    assert alerts
    assert dest.name.startswith("zarabot-")
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.ERROR
    assert record.error
    assert record.exc_info is not None
    assert "unable to open database file" in caplog.text
    assert "token" not in caplog.text
    assert "tg" not in caplog.text


async def test_successful_backup_emits_backup_ok_with_path_and_bytes(
    env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    async with aiosqlite.connect(db_path) as conn:
        await apply(conn)
        await conn.commit()
    with caplog.at_level(logging.INFO, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    events = _events(caplog, "backup_ok")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.INFO
    assert record.path == dest.name
    assert record.bytes == dest.stat().st_size
    assert dest.stat().st_size > 0


async def test_stat_failure_after_copy_returns_backup_ok_not_backup_failed(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Copy succeeded; a later stat must not raise or look like a failed copy."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    async with aiosqlite.connect(db_path) as conn:
        await apply(conn)
        await conn.commit()
    original = Path.stat

    def _stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.parent == backup_dir and self.name.startswith("zarabot-"):
            raise OSError("stat failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)
    with caplog.at_level(logging.INFO, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert dest.name.startswith("zarabot-")
    assert original(dest).st_size > 0
    assert _events(caplog, "backup_failed") == []
    events = _events(caplog, "backup_ok")
    assert len(events) == 1
    assert events[0].bytes == 0


# --- Rule 18 (v1.75): the catch is narrow. ------------------------------------
# "A backup failure is `sqlite3.Error` or `OSError`, and only those ... Every
# other exception propagates under rule 21 rather than being reported to the
# owner as "backup failed", which is a message that sends the reader to look at
# the disk when the fault is in the code."


async def test_sqlite_error_during_copy_is_reported_as_backup_failed(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    def _copy(src: Path, dest: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("zarabot.ops.backup.alert", _alert)
    monkeypatch.setattr("zarabot.ops.backup._copy", _copy)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        await run(env / "zarabot.db", env / "backups")
    assert alerts
    assert len(_events(caplog, "backup_failed")) == 1


async def test_non_backup_exception_propagates(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: red against `except Exception`."""
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    def _copy(src: Path, dest: Path) -> None:
        raise AttributeError("Connection.backup renamed")

    monkeypatch.setattr("zarabot.ops.backup.alert", _alert)
    monkeypatch.setattr("zarabot.ops.backup._copy", _copy)
    with pytest.raises(AttributeError):
        await run(env / "zarabot.db", env / "backups")
    assert alerts == []


# --- Verification (v1.86). ----------------------------------------------------
# "Before `run` may emit `backup_ok` it opens the destination as a separate,
# read-only `sqlite3` connection — `mode=ro`, in the same worker thread — and
# proves three things ... Any one of the three false, or a `sqlite3.Error` or
# `OSError` raised while checking, is a verification failure."
#
# Each case damages the destination AFTER the copy and BEFORE the check. A
# verification that cannot go red is decoration.


async def test_integrity_check_failure_deletes_destination_and_reports_verification(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _break_freelist)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    # Asserting the file is *gone* is the point: `prune` keeps by mtime, so a
    # surviving unreadable destination is the newest thing in `backup_dir` for
    # the whole retention window and a restore would prefer it.
    assert not dest.exists()
    assert list(backup_dir.glob(f"{dest.name}*")) == []
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR
    assert events[0].error == "verification"
    assert not hasattr(events[0], "path")
    assert len(alerts) == 1
    assert _events(caplog, "backup_ok") == []


async def test_integrity_check_that_raises_is_reported_as_verification(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `sqlite3.Error` raised while checking is a verification failure too."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _scramble_header)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert not dest.exists()
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "verification"
    assert len(alerts) == 1


async def test_schema_version_mismatch_deletes_destination_and_reports_verification(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Valid SQLite is not enough: it must be *this* database at *this* schema."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _bump_schema_version)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert not dest.exists()
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "verification"
    assert len(alerts) == 1
    assert _events(caplog, "backup_ok") == []


async def test_null_schema_version_deletes_destination_and_reports_verification(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`MAX(version)` must be *not null* as well as equal to the source's."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _empty_schema_version)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert not dest.exists()
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "verification"
    assert len(alerts) == 1


async def test_unreadable_positions_deletes_destination_and_reports_verification(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Check (3): the destination carries this project's schema."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _drop_positions)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert not dest.exists()
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "verification"
    assert len(alerts) == 1
    assert _events(caplog, "backup_ok") == []


async def test_verified_destination_is_byte_identical_and_still_on_disk(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The check opens read-only, and a passing check deletes nothing."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    digests: list[str] = []

    def _record(dest: Path) -> None:
        digests.append(hashlib.sha256(dest.read_bytes()).hexdigest())

    _after_copy(monkeypatch, _record)
    with caplog.at_level(logging.INFO, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert dest.is_file()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == digests[0]
    assert len(_events(caplog, "backup_ok")) == 1
    assert _events(caplog, "backup_failed") == []
    assert alerts == []


async def test_copy_that_raises_part_way_leaves_no_file_behind(
    env: Path, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The pre-existing defect: a partial destination with a fresh mtime."""
    torn = env / "torn.db"
    backup_dir = env / "backups"
    _write_torn_database(torn)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(torn, backup_dir)
    assert not dest.exists()
    assert list(backup_dir.glob("zarabot-*")) == []
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "DatabaseError"
    assert not hasattr(events[0], "path")
    assert len(alerts) == 1


async def test_delete_failure_keeps_path_on_the_record_and_in_the_alert(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one case an unusable file stays: it must be greppable and named."""
    db_path = env / "zarabot.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _after_copy(monkeypatch, _break_freelist)
    real_unlink = Path.unlink

    def _unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent == backup_dir and self.name.startswith("zarabot-"):
            raise OSError("read-only file system")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        dest = await run(db_path, backup_dir)
    assert dest.is_file()
    events = _events(caplog, "backup_failed")
    assert len(events) == 1
    assert events[0].error == "verification"
    assert events[0].path == dest.name
    assert len(alerts) == 1
    assert dest.name in alerts[0]


# --- The alert latch `_backup_alerted` (v1.86). -------------------------------
# "run alerts on a failure only while it is False ... sets it back to False on
# the success path — a destination written AND verified. That is the only
# reset ... The `backup_failed` ERROR record is emitted on every failing run,
# latched or not."


async def test_second_failure_is_logged_but_not_alerted_and_a_verified_run_rearms(
    env: Path, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    db_path = env / "zarabot.db"
    torn = env / "torn.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _write_torn_database(torn)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        await run(torn, backup_dir)
        await run(torn, backup_dir)
    assert len(_events(caplog, "backup_failed")) == 2
    assert len(alerts) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="zarabot.ops.backup"):
        await run(db_path, backup_dir)
    assert len(_events(caplog, "backup_ok")) == 1
    assert len(alerts) == 1

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        await run(torn, backup_dir)
    assert len(_events(caplog, "backup_failed")) == 1
    assert len(alerts) == 2


async def test_a_copy_that_fails_verification_does_not_clear_the_latch(
    env: Path,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reset is written *and* verified; a landed-unreadable copy is neither."""
    db_path = env / "zarabot.db"
    torn = env / "torn.db"
    backup_dir = env / "backups"
    await _seed(db_path)
    _write_torn_database(torn)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        await run(torn, backup_dir)
    assert len(alerts) == 1

    _after_copy(monkeypatch, _break_freelist)
    with caplog.at_level(logging.ERROR, logger="zarabot.ops.backup"):
        await run(db_path, backup_dir)
    assert len(_events(caplog, "backup_failed")) == 2
    assert len(alerts) == 1
