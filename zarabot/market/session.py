"""Exchange session guard. Closed when the calendar is unknown."""

from __future__ import annotations

import logging
from datetime import datetime

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_trading_schedule,
)
from zarabot.models import SessionInfo, TradingCalendar
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_cache: list[SessionInfo] | None = None
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
    global _cache, _alerted
    try:
        fetched = await get_trading_schedule(days)
    except (BrokerUnavailable, BrokerRateLimited):
        await _report_unavailable()
        return
    if not _has_trading_sessions(fetched):
        await _report_unavailable()
        return
    _cache = fetched
    # Rule 10's "alert once" is per incident, not per process. Without this a
    # schedule that went unavailable, recovered, and went unavailable again was
    # silent from here for the rest of the process lifetime (#32).
    _alerted = False


def calendar() -> TradingCalendar:
    """The cached schedule, for callers counting trading days rather than
    asking whether a moment is inside a session.

    `app.loops` fetched a fourteen-day schedule every cycle — once a minute,
    for data that changes at most daily and that this module already holds,
    refreshed daily by `run`'s schedule task (#19). Empty when the cache is
    empty; never `None`.
    """
    return TradingCalendar(sessions=tuple(_cache or ()))


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
