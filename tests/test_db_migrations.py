"""Tests for zarabot.db.migrations — written from technical-spec.md §3.2."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import MigrationError, apply

EXPECTED_TABLES = {
    "schema_version",
    "positions",
    "orders",
    "stop_orders",
    "signals",
    "cooldowns",
    "daily_snapshots",
    "halt_state",
    "instruments",
    "reconciliations",
}


async def _tables(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def _version(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    return tmp_path / "zarabot.db"


async def test_apply_to_empty_database_creates_every_table(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as conn:
        version = await apply(conn)
        tables = await _tables(conn)
    assert tables >= EXPECTED_TABLES
    assert version >= 1


async def test_apply_twice_is_idempotent(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as conn:
        first = await apply(conn)
        cursor = await conn.execute("SELECT halted FROM halt_state WHERE id = 1")
        halt = await cursor.fetchone()
        second = await apply(conn)
        cursor = await conn.execute("SELECT COUNT(*) FROM halt_state")
        count = await cursor.fetchone()
    assert first == second
    assert halt == (0,)
    assert count == (1,)


async def test_forward_migration_preserves_existing_rows(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_items.sql").write_text(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("zarabot.db.migrations.MIGRATIONS_DIR", migrations_dir)
    async with aiosqlite.connect(db_path) as conn:
        await apply(conn)
        await conn.execute("INSERT INTO items (id, name) VALUES (1, 'kept')")
        await conn.commit()
        (migrations_dir / "002_extra.sql").write_text(
            "CREATE TABLE extra (id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )
        version = await apply(conn)
        cursor = await conn.execute("SELECT name FROM items WHERE id = 1")
        row = await cursor.fetchone()
        tables = await _tables(conn)
    assert version == 2
    assert row == ("kept",)
    assert "extra" in tables


async def test_recorded_version_ahead_of_code_raises_and_changes_nothing(
    db_path: Path,
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        current = await apply(conn)
        await conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (current + 10, "2099-01-01T00:00:00+00:00"),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT COUNT(*) FROM schema_version")
        before = await cursor.fetchone()
        with pytest.raises(MigrationError):
            await apply(conn)
        cursor = await conn.execute("SELECT COUNT(*) FROM schema_version")
        after = await cursor.fetchone()
        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        highest = await cursor.fetchone()
    assert before == after
    assert highest == (current + 10,)
