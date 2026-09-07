"""Exchange session guard. Closed when the calendar is unknown."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import aiosqlite

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_trading_schedule,
)
from zarabot.clock import moscow_date
from zarabot.db.trading_days import earliest, list_since, record_many
from zarabot.models import SessionInfo, TradingCalendar
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_cache: list[SessionInfo] | None = None
# Days already observed and written down. The broker serves no schedule before
# today (§2.1), so the past is remembered rather than fetched (#45). Reloaded
# on each refresh so `calendar()` stays synchronous and costs nothing a cycle.
_history: list[SessionInfo] | None = None
_earliest: date | None = None
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
    _emit_session_state(fetched[0])
    await _remember(fetched)


def _emit_session_state(first: SessionInfo) -> None:
    """Log whether the first day of a successful fetch is a trading session.

    A log, not a Telegram alert — the brief does not alert session open/close.
    `trade_date` is the Moscow calendar date of `start` when the day is dated;
    a closed day from the broker carries no timestamps, so the date may be null.
    """
    event = "session_open" if first.is_trading_day else "session_closed"
    _LOG.info(
        event,
        extra={
            "event": event,
            "trade_date": moscow_date(first.start) if first.start is not None else None,
            "opens_at": first.start,
            "closes_at": first.end,
        },
    )


async def _remember(fetched: list[SessionInfo]) -> None:
    """Write the observed window down, then reload what is known.

    The window is fourteen days wide, not one, so a single run records the next
    fortnight and a bot that ran at any point in the last fortnight has every
    day since on disk — including days it was switched off for.

    A write failure degrades age counting, which `covers` then reports, and must
    not stop the bot trading: the schedule itself is already cached by now.
    """
    global _history, _earliest
    try:
        await record_many(fetched)
        _earliest = await earliest()
        _history = await list_since(_earliest) if _earliest is not None else []
    except aiosqlite.Error:
        _LOG.exception("could not record the observed trading calendar")


def _sort_key(session: SessionInfo) -> datetime:
    if session.start is None:
        return datetime.min.replace(tzinfo=UTC)
    return session.start


def calendar() -> TradingCalendar:
    """Recorded history plus the live window, oldest first.

    `app.loops` fetched a fourteen-day schedule every cycle — once a minute,
    for data that changes at most daily and that this module already holds,
    refreshed daily by `run`'s schedule task (#19). It spans the past because
    the question it serves is asked about the past, and the broker will not
    serve a schedule for any date before today (#45). Empty when nothing is
    known; never `None`.
    """
    merged = list(_history or ()) + list(_cache or ())
    by_day: dict[datetime, SessionInfo] = {}
    for session in sorted(merged, key=_sort_key):
        by_day[_sort_key(session)] = session
    return TradingCalendar(sessions=tuple(by_day.values()))


def covers(day: date) -> bool:
    """Whether the recorded calendar reaches back to `day`.

    Lets a caller tell a count it can stand behind from one it cannot. The
    failure this guards is silent by nature: an uncovered day is simply not
    counted, `trading_days_open` comes back short, and MAX_AGE never fires with
    nothing raised. #45 lived on exactly that.
    """
    return _earliest is not None and day >= _earliest


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
