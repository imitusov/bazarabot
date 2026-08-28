"""Exchange session guard. Closed when the calendar is unknown."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_past_trading_schedule,
    get_trading_schedule,
)
from zarabot.models import SessionInfo, TradingCalendar
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_cache: list[SessionInfo] | None = None
# The days BEFORE today. `get_trading_schedule` looks forward, which is right
# for the session questions below and wrong for counting how long a position
# has been held — every day between an entry and yesterday fell outside it, so
# `trading_days_open` was capped at 1 and MAX_AGE could never fire (#45).
_past_cache: list[SessionInfo] | None = None
_alerted = False
_UNAVAILABLE = "trading schedule unavailable; treating market as closed"


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


def _has_trading_sessions(sessions: list[SessionInfo]) -> bool:
    return any(session.is_trading_day for session in sessions)


async def _report_unavailable() -> None:
    global _alerted
    if _alerted:
        return
    _LOG.warning(_UNAVAILABLE)
    await alert(_UNAVAILABLE)
    _alerted = True


async def refresh(days: int) -> None:
    """Cache both windows. Two broker calls a day, not two a cycle."""
    global _cache, _alerted, _past_cache
    try:
        fetched = await get_trading_schedule(days)
    except (BrokerUnavailable, BrokerRateLimited):
        await _report_unavailable()
        return
    if not _has_trading_sessions(fetched):
        await _report_unavailable()
        return
    _cache = fetched
    # A failed backward fetch leaves its cache as it was and alerts, but must
    # not discard a forward window that did arrive: whether the market is open
    # is the more urgent of the two questions, and one answer beats none.
    try:
        past = await get_past_trading_schedule(days)
    except (BrokerUnavailable, BrokerRateLimited):
        await _report_unavailable()
        return
    if _has_trading_sessions(past):
        _past_cache = past
    # Rule 10's "alert once" is per incident, not per process. Without this a
    # schedule that went unavailable, recovered, and went unavailable again was
    # silent from here for the rest of the process lifetime (#32).
    _alerted = False


def _sort_key(session: SessionInfo) -> datetime:
    return (
        session.start if session.start is not None else datetime.min.replace(tzinfo=UTC)
    )


def calendar() -> TradingCalendar:
    """Both cached windows, oldest first, for callers counting trading days.

    `app.loops` fetched a fourteen-day schedule every cycle — once a minute,
    for data that changes at most daily and that this module already holds,
    refreshed daily by `run`'s schedule task (#19). It spans the past as well,
    because the question it serves is asked about the past and the forward
    window contains none of it (#45). Empty when both caches are; never
    `None`.
    """
    merged = list(_past_cache or ()) + list(_cache or ())
    return TradingCalendar(sessions=tuple(sorted(merged, key=_sort_key)))


def current_session(now: datetime) -> SessionInfo | None:
    _reject_naive(now)
    if not _cache:
        return None
    for session in _cache:
        if not session.is_trading_day or session.start is None or session.end is None:
            continue
        if session.start <= now < session.end:
            return session
    return None


def is_open(now: datetime) -> bool:
    _reject_naive(now)
    return current_session(now) is not None


def cache_exhausted(now: datetime) -> bool:
    _reject_naive(now)
    if not _cache:
        return True
    ends = [
        session.end
        for session in _cache
        if session.is_trading_day and session.end is not None
    ]
    if not ends:
        return True
    return now >= max(ends)


def in_closing_window(now: datetime, minutes: int) -> bool:
    session = current_session(now)
    if session is None:
        return False
    return session.in_closing_window(now, minutes)


def next_open(now: datetime) -> datetime:
    _reject_naive(now)
    if not _cache:
        return now
    upcoming = [
        session.start
        for session in _cache
        if session.is_trading_day and session.start is not None and session.start > now
    ]
    if not upcoming:
        return now
    return min(upcoming)
