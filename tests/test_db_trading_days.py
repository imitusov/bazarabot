"""Tests for zarabot.db.trading_days — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from zarabot.db.connection import connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.db.trading_days import earliest, list_since, record_many
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
        trade_date=date(2026, 3, day),
        start=base.replace(hour=7),
        end=base.replace(hour=15, minute=54),
        is_trading_day=True,
    )


def _closed(day: int) -> SessionInfo:
    """A non-trading day: no timestamps, but a date of its own (v1.81, #51).

    This is the shape `broker.client.get_trading_schedule` returns — the 1970
    sentinel is normalised away in `start`/`end`, and `TradingDay.date` is
    populated and correct on a closed day (§2.1).
    """
    return SessionInfo(
        trade_date=date(2026, 3, day), start=None, end=None, is_trading_day=False
    )


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
    assert [day.trade_date for day in days] == [
        date(2026, 3, 16),
        date(2026, 3, 17),
        date(2026, 3, 18),
    ]
    starts = [s.start for s in days if s.start is not None]
    assert starts == sorted(starts)
    assert len(days) == 3


async def test_a_later_observation_replaces_an_earlier_one(db: Path) -> None:
    """This is the one table here that is not append-only: it records what is
    true about a date, and the broker's latest answer about a date wins."""
    await record_many([_trading(16)])
    moved = SessionInfo(
        trade_date=date(2026, 3, 16),
        start=datetime(2026, 3, 16, 7, 0, tzinfo=UTC),
        end=datetime(2026, 3, 16, 12, 0, tzinfo=UTC),  # a short session
        is_trading_day=True,
    )
    await record_many([moved])
    days = await list_since(date(2026, 3, 16))
    assert len(days) == 1, "one row per date, not one per observation"
    assert days[0].end == datetime(2026, 3, 16, 12, 0, tzinfo=UTC)


async def test_a_day_that_becomes_a_holiday_is_overwritten(db: Path) -> None:
    """A holiday announced after the fact replaces the earlier answer. Before
    v1.81 the closed observation could not be recorded at all, so the stale
    trading row stood."""
    await record_many([_trading(16)])
    await record_many([_closed(16)])
    days = await list_since(date(2026, 3, 16))
    assert len(days) == 1
    assert days[0].is_trading_day is False
    assert days[0].start is None
    assert days[0].end is None
    assert days[0].trade_date == date(2026, 3, 16)


async def test_earliest_is_none_on_an_empty_table(db: Path) -> None:
    assert await earliest() is None


async def test_earliest_returns_the_oldest_recorded_date(db: Path) -> None:
    await record_many([_trading(18), _trading(16), _trading(17)])
    assert await earliest() == date(2026, 3, 16)


async def test_every_day_of_the_window_is_recorded_closed_ones_included(
    db: Path,
) -> None:
    """v1.81 withdraws "a day with no date is skipped": `trade_date` is a `date`
    and never `None`, so the input that case described can no longer be
    constructed — `models` refuses it. The skip was the mechanism by which this
    table held trading days only, and it is replaced by a lossless round trip.

    `006_trading_days.sql` has declared `session_start` and `session_end`
    `TEXT NULL` since it was written, so no migration is required.
    """
    written = await record_many([_trading(16), _closed(21), _closed(22), _trading(23)])
    assert written == 4
    days = await list_since(date(2026, 3, 1))
    assert [day.trade_date for day in days] == [
        date(2026, 3, 16),
        date(2026, 3, 21),
        date(2026, 3, 22),
        date(2026, 3, 23),
    ]
    closed = [day for day in days if not day.is_trading_day]
    assert len(closed) == 2
    for day in closed:
        assert day.start is None
        assert day.end is None

    cursor = await shared().execute(
        "SELECT is_trading_day, session_start, session_end FROM trading_days "
        "WHERE trade_date = ?",
        ("2026-03-21",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["is_trading_day"] == 0
    assert row["session_start"] is None
    assert row["session_end"] is None


async def test_a_closed_day_reports_its_date_from_the_trade_date_column(
    db: Path,
) -> None:
    """Not reconstructed from `session_start`, which is null on exactly the rows
    that made #51 invisible."""
    await record_many([_closed(21)])
    days = await list_since(date(2026, 3, 1))
    assert len(days) == 1
    assert days[0].trade_date == date(2026, 3, 21)
    assert days[0].start is None


async def test_earliest_is_the_oldest_calendar_day_not_the_oldest_trading_day(
    db: Path,
) -> None:
    """A Saturday at the head of an observed window is a day the bot was told
    about and wrote down. Reporting it as uncovered understated the calendar,
    which is what `market.session.covers` was built on (v1.81)."""
    # 21 March 2026 is a Saturday.
    assert date(2026, 3, 21).weekday() == 5
    await record_many([_closed(21), _closed(22), _trading(23)])
    assert await earliest() == date(2026, 3, 21)


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
