"""Tests for zarabot.db.cooldowns — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect, shared
from zarabot.db.cooldowns import active_until, is_active, start
from zarabot.db.migrations import apply

MINUTES = 120
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
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


async def test_ticker_with_no_cooldown_is_not_active(db: Path) -> None:
    assert await is_active("SBER", NOW, MINUTES) is False
    assert await active_until("SBER", MINUTES) is None


async def test_cooldown_started_one_minute_before_expiry_is_active(db: Path) -> None:
    started = NOW - timedelta(minutes=MINUTES - 1)
    await start("SBER", started)
    assert await is_active("SBER", NOW, MINUTES) is True


async def test_cooldown_started_exactly_minutes_ago_is_not_active(db: Path) -> None:
    started = NOW - timedelta(minutes=MINUTES)
    await start("SBER", started)
    assert await is_active("SBER", NOW, MINUTES) is False


async def test_starting_again_extends_from_the_newer_timestamp(db: Path) -> None:
    first = NOW - timedelta(minutes=90)
    newer = NOW - timedelta(minutes=10)
    await start("SBER", first)
    await start("SBER", newer)
    assert await is_active("SBER", NOW, MINUTES) is True
    until = await active_until("SBER", MINUTES)
    assert until == newer + timedelta(minutes=MINUTES)
    older = NOW - timedelta(minutes=100)
    await start("SBER", older)
    until_after = await active_until("SBER", MINUTES)
    assert until_after == newer + timedelta(minutes=MINUTES)


async def test_naive_datetimes_are_rejected(db: Path) -> None:
    naive = datetime(2026, 3, 16, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await start("SBER", naive)
    with pytest.raises(ValueError):
        await is_active("SBER", naive, MINUTES)


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    with pytest.raises(DatabaseNotOpenError):
        await is_active("SBER", NOW, MINUTES)
    with pytest.raises(DatabaseNotOpenError):
        await active_until("SBER", MINUTES)
    with pytest.raises(DatabaseNotOpenError):
        await start("SBER", NOW)


# --- Rule 11: a write failure propagates (issue #210). -----------------------
# §4, `zarabot/db/cooldowns.py`: "**A write failure propagates (v1.63).** This
# module catches no `aiosqlite.Error` and logs no failure of its own: cooldowns
# are rule 11, and `db.connection` already emits `db_write_failed` with
# `critical` true before re-raising."
#
# A test that only asserts a successful write passes just as well against a
# swallowed error (failure class 3), which is why the swallow survived from
# v1.63 to #210 with this file fully green.


async def _break_the_cooldowns_table() -> None:
    """Make the next `start` fail the way a damaged database would.

    A dropped table is the cheapest real `aiosqlite.Error` at the real write
    site: the SQL, the transaction and the re-raise are all production code,
    so nothing here is a stand-in for the path under test.
    """
    conn = shared()
    await conn.execute("DROP TABLE cooldowns")
    await conn.commit()


async def test_a_write_failure_propagates_out_of_start(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _break_the_cooldowns_table()
    with caplog.at_level(logging.DEBUG), pytest.raises(aiosqlite.Error):
        await start("SBER", NOW)
    # "logs no failure of its own": the only record is `db.connection`'s.
    assert [r.name for r in caplog.records if r.levelno >= logging.ERROR] == [
        "zarabot.db.connection"
    ]


async def test_the_propagated_failure_is_reported_as_trading_critical(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 11, v1.63: `cooldowns` is critical, not analytics."""
    await _break_the_cooldowns_table()
    with caplog.at_level(logging.DEBUG), pytest.raises(aiosqlite.Error):
        await start("SBER", NOW)
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "db_write_failed"
    ]
    assert len(events) == 1
    assert events[0].critical is True
    assert events[0].table == "cooldowns"


async def test_the_module_catches_no_database_error(db: Path) -> None:
    """Structural guard against the swallow being written back.

    The behavioural tests above only cover `start`; the contract's "propagates
    everything" is about the module. `ruff` cannot catch this — `BLE001` sees a
    named exception class, not a narrow catch that contradicts its contract.
    """
    import zarabot.db.cooldowns as module

    source = inspect.getsource(module)
    assert "except aiosqlite.Error" not in source
    assert "except Exception" not in source
    assert "_LOG.exception" not in source


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.db.cooldowns as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    assert "_connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    await start("SBER", NOW)
    assert await is_active("SBER", NOW + timedelta(minutes=1), MINUTES) is True
    assert await active_until("SBER", MINUTES) is not None
    assert calls == []
