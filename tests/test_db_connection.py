"""Tests for zarabot.db.connection — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import (
    DatabaseAlreadyOpenError,
    DatabaseNotOpenError,
    connect,
    disconnect,
    shared,
)
from zarabot.db.migrations import apply

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
async def _close_process_connection() -> None:
    yield
    await disconnect()


def test_importing_the_module_opens_no_file(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from zarabot.db.connection import DatabaseNotOpenError, shared\n"
            "raised = False\n"
            "try:\n"
            "    shared()\n"
            "except DatabaseNotOpenError:\n"
            "    raised = True\n"
            "assert raised\n"
            "from pathlib import Path\n"
            f"assert list(Path({str(tmp_path)!r}).glob('*.db')) == []\n",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_connect_then_shared_returns_live_connection(tmp_path: Path) -> None:
    path = tmp_path / "zarabot.db"
    live = await connect(str(path))
    via_shared = shared()
    assert via_shared is live
    cursor = await live.execute("SELECT 1")
    assert await cursor.fetchone() == (1,)


async def test_second_connect_without_disconnect_raises(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zarabot.db"
    await connect(str(path))
    with pytest.raises(DatabaseAlreadyOpenError):
        await connect(str(tmp_path / "other.db"))


async def test_shared_before_connect_and_after_disconnect_raises_and_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)

    with pytest.raises(DatabaseNotOpenError):
        shared()
    assert calls == []

    path = tmp_path / "zarabot.db"
    await connect(str(path))
    await disconnect()
    calls.clear()
    with pytest.raises(DatabaseNotOpenError):
        shared()
    assert calls == []
    assert not (tmp_path / "fallback.db").exists()


async def test_disconnect_is_idempotent(tmp_path: Path) -> None:
    await disconnect()
    await disconnect()
    path = tmp_path / "zarabot.db"
    await connect(str(path))
    await disconnect()
    await disconnect()
    with pytest.raises(DatabaseNotOpenError):
        shared()


async def test_connect_sets_wal_foreign_keys_and_busy_timeout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    journal = await (await conn.execute("PRAGMA journal_mode")).fetchone()
    foreign_keys = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
    busy_timeout = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
    assert journal is not None
    assert journal[0].lower() == "wal"
    assert foreign_keys == (1,)
    assert busy_timeout == (30000,)


async def test_wal_writer_does_not_block_reader_on_another_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await apply(conn)
    await conn.execute("BEGIN IMMEDIATE")
    await conn.execute(
        "INSERT INTO cooldowns (ticker, started_at) VALUES (?, ?)",
        ("SBER", "2026-03-16T10:00:00+00:00"),
    )

    async def read_other_table() -> int:
        async with aiosqlite.connect(path) as other:
            cursor = await other.execute("SELECT COUNT(*) FROM schema_version")
            row = await cursor.fetchone()
            assert row is not None
            return int(row[0])

    count = await asyncio.wait_for(read_other_table(), timeout=5)
    await conn.commit()
    assert count >= 1


async def test_first_of_two_sequential_tests_uses_its_own_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "first.db"
    conn = await connect(str(path))
    await apply(conn)
    await conn.execute(
        "INSERT INTO cooldowns (ticker, started_at) VALUES (?, ?)",
        ("SBER", "2026-03-16T10:00:00+00:00"),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT COUNT(*) FROM cooldowns")
    assert await cursor.fetchone() == (1,)


async def test_second_sequential_test_sees_none_of_the_first_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "second.db"
    conn = await connect(str(path))
    await apply(conn)
    cursor = await conn.execute("SELECT COUNT(*) FROM cooldowns")
    assert await cursor.fetchone() == (0,)
