"""Tests for zarabot.ops.backup — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
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
    return tmp_path


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if getattr(record, "event", None) == event
    ]


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
