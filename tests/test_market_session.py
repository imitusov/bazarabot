"""Tests for zarabot.market.session — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import zarabot.market.session as session_mod
from zarabot.market.session import (
    cache_exhausted,
    current_session,
    in_closing_window,
    is_open,
    next_open,
    refresh,
)
from zarabot.models import SessionInfo

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
def _reset_cache() -> None:
    session_mod._cache = None
    session_mod._alerted = False


@pytest.fixture
async def schedule(monkeypatch: pytest.MonkeyPatch) -> None:
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
