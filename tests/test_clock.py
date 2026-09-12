"""Tests for zarabot.clock — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zarabot.clock import moscow_date, now, to_moscow, trading_days_between
from zarabot.models import SessionInfo, TradingCalendar

MOSCOW = ZoneInfo("Europe/Moscow")
AWARE = datetime(2026, 3, 13, 10, 0, tzinfo=UTC)  # Friday
NAIVE = datetime(2026, 3, 13, 10, 0)  # noqa: DTZ001


def _session(day: datetime, trading: bool = True) -> SessionInfo:
    """`trade_date` is the Moscow date of `start`, the producer obligation every
    fixture owes (§4 `models`).

    The non-trading days here keep their session times on purpose, and that is a
    shape `get_trading_schedule` cannot return. It is deliberate in this file
    only: `trading_days_between` filters on `is_trading_day` **and** a non-null
    `start`, and a closed fixture with no timestamps would leave the
    `is_trading_day` half of that filter untested here. The production shape is
    contracted where it matters — `market.session`'s §3.2, whose closed-day
    fixtures are what hid #51.
    """
    start = day.replace(hour=6, minute=50, second=0, microsecond=0)
    end = day.replace(hour=15, minute=50, second=0, microsecond=0)
    return SessionInfo(
        trade_date=moscow_date(start), start=start, end=end, is_trading_day=trading
    )


def _week_calendar() -> TradingCalendar:
    friday = datetime(2026, 3, 13, tzinfo=UTC)
    days = []
    for offset in range(7):
        day = friday + timedelta(days=offset)
        trading = day.weekday() < 5
        days.append(_session(day, trading=trading))
    return TradingCalendar(sessions=tuple(days))


def test_now_returns_timezone_aware_utc() -> None:
    moment = now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_to_moscow_on_dst_shifted_month_is_not_a_fixed_offset() -> None:
    """July 2010: Moscow observed DST (UTC+4). A hardcoded +3 would be wrong."""
    utc_instant = datetime(2010, 7, 15, 10, 0, tzinfo=UTC)
    moscow = to_moscow(utc_instant)
    assert moscow.tzinfo is not None
    assert moscow.utcoffset() == timedelta(hours=4)
    assert moscow.hour == 14
    assert moscow.tzinfo == MOSCOW


def test_trading_days_between_across_weekend_excludes_saturday_and_sunday() -> None:
    friday = datetime(2026, 3, 13, 10, 0, tzinfo=UTC)
    monday = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
    calendar = _week_calendar()
    assert trading_days_between(friday, friday, calendar) == 0
    assert trading_days_between(friday, monday, calendar) == 1
    tuesday = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)
    assert trading_days_between(friday, tuesday, calendar) == 2


def test_naive_datetime_raises_value_error() -> None:
    calendar = _week_calendar()
    with pytest.raises(ValueError):
        to_moscow(NAIVE)
    with pytest.raises(ValueError):
        moscow_date(NAIVE)
    with pytest.raises(ValueError):
        trading_days_between(NAIVE, AWARE, calendar)
    with pytest.raises(ValueError):
        trading_days_between(AWARE, NAIVE, calendar)
    with pytest.raises(ValueError):
        trading_days_between(AWARE, AWARE - timedelta(seconds=1), calendar)
