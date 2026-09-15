"""Tests for zarabot.db.migrations — written from technical-spec.md §3.2."""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import MIGRATIONS_DIR, MigrationError, apply

_FILE = re.compile(r"^(\d+)_.*\.sql$")

EXPECTED_TABLES = {
    "schema_version",
    "positions",
    "position_events",
    "orders",
    "stop_orders",
    "signals",
    "cooldowns",
    "daily_snapshots",
    "halt_state",
    "instruments",
    "reconciliations",
}


def _highest_on_disk() -> int:
    versions: list[int] = []
    for path in MIGRATIONS_DIR.iterdir():
        match = _FILE.match(path.name)
        if match and path.is_file():
            versions.append(int(match.group(1)))
    assert versions
    return max(versions)


async def _tables(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


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
        order_cols = await _columns(conn, "orders")
        recorded = await _version(conn)
    assert tables >= EXPECTED_TABLES
    assert "exit_trigger" in order_cols
    assert version == _highest_on_disk()
    assert recorded == version


async def test_apply_sets_wal_and_foreign_keys_on_given_connection(
    db_path: Path,
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await apply(conn)
        journal = await conn.execute("PRAGMA journal_mode")
        journal_row = await journal.fetchone()
        foreign_keys = await conn.execute("PRAGMA foreign_keys")
        foreign_keys_row = await foreign_keys.fetchone()
    assert journal_row is not None
    assert journal_row[0].lower() == "wal"
    assert foreign_keys_row == (1,)


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


async def test_008_rewrites_a_zero_commission_on_a_filled_order_to_null(
    tmp_path: Path,
) -> None:
    """#246: the history half. Every zero in that column was written by
    `_executed_commission` reading a zero off the order state, which spec v1.91
    rules an absence — and `list_missing_commission` selects `IS NULL`, so those
    rows are invisible to the backfill built to correct them.

    Applied to every spelling a serialised `Decimal` zero may have, and to
    nothing else.
    """
    path = tmp_path / "m.db"
    script = (MIGRATIONS_DIR / "008_commission_zero_is_unknown.sql").read_text(
        encoding="utf-8"
    )
    rows = [
        ("k-plain", "FILLED", "0"),
        ("k-scaled", "FILLED", "0E-9"),
        ("k-decimals", "FILLED", "0.00"),
        ("k-real", "FILLED", "0.53"),
        ("k-unknown", "FILLED", None),
        ("k-cancelled", "CANCELLED", "0"),
    ]
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
        for key, status, commission in rows:
            await conn.execute(
                """
                INSERT INTO orders (
                    key, ticker, figi, side, intent, lots, status,
                    commission, created_at
                ) VALUES (?, 'SBER', 'BBG1', 'BUY', 'ENTRY', 1, ?, ?,
                          '2026-03-16T10:00:00+00:00')
                """,
                (key, status, commission),
            )
        await conn.commit()
        # The migration is already applied; running its text again proves the
        # predicate, which is the part that can be wrong.
        await conn.executescript(script)
        cursor = await conn.execute("SELECT key, commission FROM orders")
        found = dict(await cursor.fetchall())
    assert found["k-plain"] is None
    assert found["k-scaled"] is None
    assert found["k-decimals"] is None
    assert found["k-real"] == "0.53"
    assert found["k-unknown"] is None
    # A cancelled order's zero is not a missing fill commission.
    assert found["k-cancelled"] == "0"
