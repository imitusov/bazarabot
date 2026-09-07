"""Tests for zarabot.market.session — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import aiosqlite
import pytest

import zarabot.market.session as session_mod
from zarabot.market.session import (
    cache_exhausted,
    calendar,
    covers,
    current_session,
    in_closing_window,
    is_open,
    next_open,
    refresh,
)
from zarabot.models import SessionInfo, TradingCalendar

REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}

OPEN = datetime(2026, 3, 16, 6, 50, tzinfo=UTC)
CLOSE = datetime(2026, 3, 16, 15, 50, tzinfo=UTC)
INSIDE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
HOLIDAY = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)  # Monday 9 Mar 2026 as a holiday


def _weekday() -> SessionInfo:
    return SessionInfo(start=OPEN, end=CLOSE, is_trading_day=True)


def _saturday() -> SessionInfo:
    start = datetime(2026, 3, 14, 6, 50, tzinfo=UTC)
    end = datetime(2026, 3, 14, 15, 50, tzinfo=UTC)
    return SessionInfo(start=start, end=end, is_trading_day=False)


def _holiday() -> SessionInfo:
    start = datetime(2026, 3, 9, 6, 50, tzinfo=UTC)
    end = datetime(2026, 3, 9, 15, 50, tzinfo=UTC)
    return SessionInfo(start=start, end=end, is_trading_day=False)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    session_mod._cache = None
    session_mod._history = None
    session_mod._earliest = None
    session_mod._alerted = False
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(session_mod, "alert", _alert, raising=False)
    return alerts


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[SessionInfo]]:
    """A real db.trading_days, on a temporary file database."""
    import tempfile

    from zarabot.db.connection import connect, disconnect
    from zarabot.db.migrations import apply

    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/zarabot.db"
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DB_PATH", path)
        conn = await connect(path)
        await apply(conn)
        try:
            yield []
        finally:
            await disconnect()


def _day(n: int) -> SessionInfo:
    base = datetime(2026, 3, n, tzinfo=UTC)
    trading = base.weekday() < 5
    return SessionInfo(
        start=base.replace(hour=6, minute=50) if trading else None,
        end=base.replace(hour=15, minute=50) if trading else None,
        is_trading_day=trading,
    )


async def test_refresh_records_the_whole_window_not_only_today(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourteen-day width is the property the design rests on: one run
    covers the next fortnight, so an outage shorter than that leaves no gap."""
    from zarabot.db.trading_days import list_since

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 30)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    await refresh(14)

    recorded = await list_since(date(2026, 3, 1))
    assert len(recorded) == 10, "ten weekdays in the fortnight, all recorded"


async def test_calendar_remembers_a_day_that_has_since_passed(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The past cannot be fetched (§2.1), so it has to have been written down."""

    async def _first(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(2, 7)]

    async def _later(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 21)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _first)
    await refresh(5)
    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _later)
    await refresh(5)

    dates = [s.start.date() for s in calendar().sessions if s.start is not None]
    assert date(2026, 3, 2) in dates, "the earlier window is remembered"
    assert date(2026, 3, 20) in dates
    assert dates == sorted(dates)


async def test_a_weeks_old_position_counts_its_trading_days(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against the calendar `refresh` actually builds — the seam #45 lived in
    while both sides passed their own tests."""
    from zarabot.clock import trading_days_between

    async def _early(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(2, 16)]

    async def _now(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 21)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _early)
    await refresh(14)
    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _now)
    await refresh(14)

    entry = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    now = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    # 6, 9, 10, 11, 12, 13, 16 March are weekdays -> 7 trading days.
    assert trading_days_between(entry, now, calendar()) == 7


async def test_covers_reports_what_the_history_reaches(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmeasurable age must be detectable; that is the whole difference
    between this and the silent undercount #45 was."""

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 21)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    await refresh(5)

    assert covers(date(2026, 3, 17)) is True
    assert covers(date(2026, 3, 16)) is True
    assert covers(date(2026, 3, 10)) is False


async def test_covers_is_false_with_no_history_at_all() -> None:
    assert covers(date(2026, 3, 16)) is False


async def test_a_failed_history_write_leaves_the_schedule_cached(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degraded age counting must not stop the market session working."""

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_weekday()]

    async def _boom(sessions: list[SessionInfo]) -> int:
        raise aiosqlite.Error("disk full")

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    monkeypatch.setattr("zarabot.market.session.record_many", _boom)
    await refresh(7)

    assert is_open(INSIDE) is True


@pytest.fixture
async def schedule(store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_holiday(), _saturday(), _weekday()]

    async def _fake(days: int) -> list[SessionInfo]:
        return sessions[:days] if days < len(sessions) else sessions

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fake)
    await refresh(7)


async def test_timestamp_inside_main_session_is_open(schedule: None) -> None:
    assert is_open(INSIDE) is True


async def test_open_instant_is_open_close_instant_is_closed(schedule: None) -> None:
    assert is_open(OPEN) is True
    assert is_open(CLOSE) is False


async def test_saturday_and_holiday_are_closed(schedule: None) -> None:
    assert is_open(SATURDAY) is False
    assert is_open(HOLIDAY) is False


async def test_unavailable_schedule_is_closed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.broker.client import BrokerUnavailable

    async def _fail(days: int) -> list[SessionInfo]:
        raise BrokerUnavailable("broker unavailable")

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fail)
    await refresh(7)
    assert is_open(INSIDE) is False
    assert current_session(INSIDE) is None


async def test_never_refreshed_is_closed() -> None:
    assert is_open(INSIDE) is False


async def test_empty_cache_is_exhausted() -> None:
    assert cache_exhausted(INSIDE) is True


async def test_current_session_and_closing_window(schedule: None) -> None:
    session = current_session(INSIDE)
    assert session is not None
    assert session.start == OPEN
    window_start = CLOSE - timedelta(minutes=15)
    assert in_closing_window(window_start, 15) is True
    assert in_closing_window(INSIDE, 15) is False


async def test_next_open_returns_upcoming_session_start(schedule: None) -> None:
    before = datetime(2026, 3, 16, 5, 0, tzinfo=UTC)
    assert next_open(before) == OPEN
    assert next_open(SATURDAY) == OPEN


async def test_past_last_cached_session_is_closed_and_cache_exhausted(
    schedule: None,
) -> None:
    past_last = CLOSE + timedelta(seconds=1)
    assert is_open(past_last) is False
    assert cache_exhausted(past_last) is True
    assert is_open(SATURDAY) is False
    assert cache_exhausted(SATURDAY) is False


async def test_rollover_refresh_clears_cache_exhausted(
    store: list[SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _first(days: int) -> list[SessionInfo]:
        return [_weekday()]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _first)
    await refresh(7)
    past_last = CLOSE + timedelta(hours=1)
    assert cache_exhausted(past_last) is True

    next_open_start = datetime(2026, 3, 17, 6, 50, tzinfo=UTC)
    next_open_end = datetime(2026, 3, 17, 15, 50, tzinfo=UTC)

    async def _rollover(days: int) -> list[SessionInfo]:
        return [
            _weekday(),
            SessionInfo(start=next_open_start, end=next_open_end, is_trading_day=True),
        ]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _rollover)
    await refresh(7)
    assert cache_exhausted(past_last) is False


async def test_empty_schedule_leaves_populated_cache_intact(
    schedule: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty(days: int) -> list[SessionInfo]:
        return []

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _empty)
    await refresh(7)
    assert is_open(INSIDE) is True
    assert current_session(INSIDE) is not None


async def test_empty_schedule_alerts_once(
    _reset_cache: list[str],
    schedule: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty(days: int) -> list[SessionInfo]:
        return []

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _empty)
    await refresh(7)
    await refresh(7)
    assert len(_reset_cache) == 1


async def test_a_second_outage_alerts_again(
    store: list[SessionInfo],
    _reset_cache: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 10's "alert once" is per incident, not per process. The latch was
    set on the first failure and never cleared, so every outage after the first
    was silent from this module for the life of the process (#32)."""
    from zarabot.broker.client import BrokerUnavailable

    sessions = [_weekday()]
    failing = True

    async def _fetch(days: int) -> list[SessionInfo]:
        if failing:
            raise BrokerUnavailable("broker unavailable")
        return sessions

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    await refresh(7)
    assert len(_reset_cache) == 1
    failing = False
    await refresh(7)
    assert len(_reset_cache) == 1
    failing = True
    await refresh(7)
    assert len(_reset_cache) == 2


async def test_calendar_returns_the_cached_sessions(schedule: None) -> None:
    """The caller counts trading days from this instead of fetching a
    fourteen-day schedule once a minute (#19)."""
    result = calendar()
    assert isinstance(result, TradingCalendar)
    assert result.sessions == (_holiday(), _saturday(), _weekday())


async def test_calendar_is_empty_not_none_before_any_refresh() -> None:
    result = calendar()
    assert isinstance(result, TradingCalendar)
    assert result.sessions == ()
