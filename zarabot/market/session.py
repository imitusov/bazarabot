"""Exchange session guard. Closed when the calendar is unknown."""

from __future__ import annotations

import logging
from datetime import datetime

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_trading_schedule,
)
from zarabot.models import SessionInfo

_LOG = logging.getLogger(__name__)
_cache: list[SessionInfo] | None = None
_alerted = False


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


async def refresh(days: int) -> None:
    global _cache, _alerted
    try:
        _cache = await get_trading_schedule(days)
    except (BrokerUnavailable, BrokerRateLimited):
        _cache = None
        if not _alerted:
            _LOG.warning("trading schedule unavailable; treating market as closed")
            _alerted = True


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
        return False
    ends = [session.end for session in _cache if session.end is not None]
    if not ends:
        return False
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
