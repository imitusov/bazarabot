"""Tests for zarabot.db.cooldowns — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect
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
