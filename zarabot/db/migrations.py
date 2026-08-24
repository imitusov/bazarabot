"""Schema creation and version tracking. Forward-only."""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite

from zarabot.clock import now

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILE = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(Exception):
    """Schema version is ahead of the code, or a migration cannot be applied."""


def _migration_files() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return found
    for path in MIGRATIONS_DIR.iterdir():
        match = _FILE.match(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


async def _recorded_version(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
    current = await cursor.fetchone()
    if current is None or current[0] is None:
        return 0
    return int(current[0])


async def apply(conn: aiosqlite.Connection) -> int:
    """Apply pending migrations in ascending order. Each file is one transaction."""
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 30000")
    await conn.execute("PRAGMA journal_mode = WAL")
    files = _migration_files()
    if not files:
        raise MigrationError("no migration files found")
    highest = files[-1][0]
    recorded = await _recorded_version(conn)
    if recorded > highest:
        raise MigrationError(
            f"schema version {recorded} is ahead of code version {highest}"
        )
    for version, path in files:
        if version <= recorded:
            continue
        sql = path.read_text(encoding="utf-8")
        await conn.execute("BEGIN")
        try:
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, now().isoformat()),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        recorded = version
    return recorded
