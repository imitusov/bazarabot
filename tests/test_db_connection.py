"""Tests for zarabot.db.connection — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import (
    DatabaseAlreadyOpenError,
    DatabaseNotOpenError,
    connect,
    disconnect,
    shared,
    transaction,
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
    assert tuple(await cursor.fetchone()) == (1,)


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
    assert tuple(foreign_keys) == (1,)
    assert tuple(busy_timeout) == (30000,)


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
    assert tuple(await cursor.fetchone()) == (1,)


async def test_second_sequential_test_sees_none_of_the_first_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "second.db"
    conn = await connect(str(path))
    await apply(conn)
    cursor = await conn.execute("SELECT COUNT(*) FROM cooldowns")
    assert tuple(await cursor.fetchone()) == (0,)


async def _seed(conn: aiosqlite.Connection) -> None:
    await apply(conn)


async def test_two_writers_in_different_modules_run_concurrently(
    tmp_path: Path,
) -> None:
    """Spec §3.2: the transaction is serialised process-wide, not per module.

    Before v1.24 these collided with `cannot start a transaction within a
    transaction` on the first attempt (#40).
    """
    from zarabot.db import orders, stop_orders
    from zarabot.models import Side

    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await _seed(conn)
    await conn.execute(
        "INSERT INTO orders (key,ticker,figi,side,intent,lots,status,created_at)"
        " VALUES ('seed','SBER','BBG1','BUY','ENTRY',1,'FILLED',?)",
        ("2026-03-16T12:00:00+00:00",),
    )
    for pid in range(1, 6):
        await conn.execute(
            "INSERT INTO positions (id,ticker,figi,strategy,lots,lot_size,"
            "entry_price,entry_at,stop_price,target_price,status,adopted,"
            "open_order_key,stop_protection) VALUES (?,?,'BBG1','ma',1,10,'100',"
            "'2026-03-16T12:00:00+00:00','95','110','OPEN',0,'seed','LOCAL')",
            (pid, f"TICK{pid}"),
        )
    await conn.commit()

    async def writer_orders(i: int) -> None:
        await orders.record_submitting(f"k-a{i}", "SBER", Side.BUY, 1, "ENTRY")

    async def writer_stops(i: int) -> None:
        await stop_orders.record_placing(
            f"k-b{i}", i + 1, f"TICK{i + 1}", 1, Decimal("95")
        )

    for i in range(5):
        await asyncio.gather(writer_orders(i), writer_stops(i))

    cursor = await shared().execute("SELECT COUNT(*) FROM orders")
    assert (await cursor.fetchone())[0] == 6
    cursor = await shared().execute("SELECT COUNT(*) FROM stop_orders")
    assert (await cursor.fetchone())[0] == 5


async def test_nested_transaction_joins_the_outer_one(tmp_path: Path) -> None:
    """A nested acquisition must not commit early, and must not deadlock."""
    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await _seed(conn)

    with pytest.raises(RuntimeError, match="outer failure"):
        async with transaction() as outer:
            await outer.execute(
                "INSERT INTO cooldowns (ticker, started_at) VALUES ('SBER', 'x')"
            )
            async with transaction() as inner:
                await inner.execute(
                    "INSERT INTO cooldowns (ticker, started_at) VALUES ('GAZP', 'y')"
                )
            # the inner block exiting must NOT have committed
            raise RuntimeError("outer failure")

    cursor = await shared().execute("SELECT COUNT(*) FROM cooldowns")
    assert (await cursor.fetchone())[0] == 0


async def test_write_does_not_survive_rollback_despite_another_module_writing(
    tmp_path: Path,
) -> None:
    """Spec §3.2: a foreign write can no longer make half-written rows durable.

    This is #40's second reproduction: `state.halt` committing the shared
    connection made another module's in-flight rows permanent, so its own
    `rollback()` undid nothing.
    """
    from datetime import UTC, datetime

    from zarabot.models import HaltReason
    from zarabot.state.halt import halt

    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await _seed(conn)
    moment = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="injected"):
        async with transaction() as txn:
            await txn.execute(
                "INSERT INTO orders (key,ticker,figi,side,intent,lots,status,"
                "created_at) VALUES ('half-written','SBER','BBG1','BUY','ENTRY',1,"
                "'SUBMITTING','2026-03-16T12:00:00+00:00')"
            )
            await halt(HaltReason.MANUAL, "unrelated", moment)
            raise RuntimeError("injected")

    cursor = await shared().execute(
        "SELECT COUNT(*) FROM orders WHERE key = 'half-written'"
    )
    assert (await cursor.fetchone())[0] == 0


async def test_reads_do_not_need_a_transaction(tmp_path: Path) -> None:
    """A read path takes no transaction and is not blocked by one."""
    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await _seed(conn)

    async with transaction() as txn:
        await txn.execute(
            "INSERT INTO cooldowns (ticker, started_at) VALUES ('SBER', 'x')"
        )
        cursor = await shared().execute("SELECT COUNT(*) FROM cooldowns")
        assert (await cursor.fetchone())[0] == 1
