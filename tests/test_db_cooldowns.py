"""Tests for zarabot.db.cooldowns — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

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
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    return path


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
