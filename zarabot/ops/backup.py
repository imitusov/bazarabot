"""Nightly SQLite backup. Failure alerts; trading continues."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zarabot.clock import now
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_PREFIX = "zarabot-"
_SUFFIX = ".db"


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
    """Consistent SQLite backup. Never a raw copy of a live file. Never raises."""
    dest = _dest_path(backup_dir)
    try:
        await asyncio.to_thread(_copy, db_path, dest)
        return dest
    except Exception:
        _LOG.exception("backup failed")
        await alert(f"Database backup failed for {dest.name}. Trading continues.")
        return dest


async def prune(backup_dir: Path, retention_days: int) -> int:
    """Remove backups strictly older than retention_days. Returns how many."""
    return await asyncio.to_thread(_prune_sync, backup_dir, retention_days)
