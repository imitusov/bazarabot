"""Sole owner of the current time and of trading-day arithmetic."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from zarabot.models import TradingCalendar

_MOSCOW = ZoneInfo("Europe/Moscow")


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def now() -> datetime:
    """The current instant, timezone-aware, in UTC. Sole reader of the system clock."""
    return datetime.now(UTC)


def to_moscow(moment: datetime) -> datetime:
    """Convert a timezone-aware instant to Europe/Moscow. Never a fixed offset."""
    _reject_naive(moment)
    return moment.astimezone(_MOSCOW)


def moscow_date(moment: datetime) -> date:
    """The Moscow calendar date of an instant."""
    return to_moscow(moment).date()


def trading_days_between(
    start: datetime, end: datetime, calendar: TradingCalendar
) -> int:
    """Trading days elapsed between start and end, excluding weekends and holidays.

    Returns 0 when both instants fall on the same trading day.
    """
    _reject_naive(start)
    _reject_naive(end)
    if end < start:
        raise ValueError("end precedes start")

    trading_dates = {
        moscow_date(session.start)
        for session in calendar.sessions
        if session.is_trading_day and session.start is not None
    }
    start_d = moscow_date(start)
    end_d = moscow_date(end)
    elapsed = 0
    cursor = start_d + timedelta(days=1)
    while cursor <= end_d:
        if cursor in trading_dates:
            elapsed += 1
        cursor += timedelta(days=1)
    return elapsed
