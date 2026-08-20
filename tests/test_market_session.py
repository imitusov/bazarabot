"""Tests for zarabot.market.session — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zarabot.market.session import (
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
