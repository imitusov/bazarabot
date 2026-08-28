"""Tests for zarabot.db.trading_days — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from zarabot.db.trading_days import earliest, list_since, record_many

from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.models import SessionInfo

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _trading(day: int) -> SessionInfo:
    base = datetime(2026, 3, day, tzinfo=UTC)
    return SessionInfo(
        start=base.replace(hour=7),
        end=base.replace(hour=15, minute=54),
        is_trading_day=True,
    )


def _closed() -> SessionInfo:
    """A non-trading day, which by contract carries no timestamps at all."""
    return SessionInfo(start=None, end=None, is_trading_day=False)


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr("zarabot.db.trading_days.clock_now", lambda: NOW)
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


async def test_recorded_window_reads_back_oldest_first(db: Path) -> None:
    written = await record_many([_trading(18), _trading(16), _trading(17)])
    assert written == 3
    days = await list_since(date(2026, 3, 1))
    starts = [s.start for s in days if s.start is not None]
    assert starts == sorted(starts)
    assert len(days) == 3


async def test_a_later_observation_replaces_an_earlier_one(db: Path) -> None:
    """This is the one table here that is not append-only: it records what is
    true about a date, and the broker's latest answer about a date wins."""
    await record_many([_trading(16)])
    moved = SessionInfo(
        start=datetime(2026, 3, 16, 7, 0, tzinfo=UTC),
        end=datetime(2026, 3, 16, 12, 0, tzinfo=UTC),  # a short session
        is_trading_day=True,
    )
    await record_many([moved])
    days = await list_since(date(2026, 3, 16))
    assert len(days) == 1, "one row per date, not one per observation"
    assert days[0].end == datetime(2026, 3, 16, 12, 0, tzinfo=UTC)


async def test_earliest_is_none_on_an_empty_table(db: Path) -> None:
    assert await earliest() is None


async def test_earliest_returns_the_oldest_recorded_date(db: Path) -> None:
    await record_many([_trading(18), _trading(16), _trading(17)])
    assert await earliest() == date(2026, 3, 16)


async def test_an_undated_day_is_skipped_not_stored(db: Path) -> None:
    """Only trading days carry a date. A closed day cannot be keyed, and it does
    not need to be: nothing counts it, and `earliest` is defined in terms of the
    oldest recorded TRADING day, which is exactly what coverage depends on."""
    written = await record_many([_trading(16), _closed(), _trading(17)])
    assert written == 2
    assert len(await list_since(date(2026, 3, 1))) == 2


async def test_list_since_excludes_earlier_days(db: Path) -> None:
    await record_many([_trading(16), _trading(17), _trading(18)])
    days = await list_since(date(2026, 3, 17))
    assert len(days) == 2


async def test_list_since_on_empty_returns_empty_list(db: Path) -> None:
    result = await list_since(date(2026, 3, 1))
    assert result == []
    assert result is not None


async def test_module_never_opens_or_commits_its_own_connection() -> None:
    import zarabot.db.trading_days as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    for statement in ("BEGIN", "commit()", "rollback()"):
        assert statement not in source
