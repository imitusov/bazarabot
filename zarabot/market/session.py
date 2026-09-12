"""Exchange session guard. Closed when the calendar is unknown."""

from __future__ import annotations

import logging
from datetime import date, datetime

import aiosqlite

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_trading_schedule,
)
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

    `trade_date` is `first.trade_date` on **both** branches, and nothing else
    (v1.81, superseding v1.67). v1.67 derived it from `clock.now()` because the
    type had no date; it has one now, so the workaround is withdrawn. One field,
    one source, and the source is the entry being described: `first.trade_date`
    is the broker's own answer for the day the record is *about*, whereas the
    clock answers for the day the fetch happened, and a refresh straddling
    Moscow midnight separates them. `opens_at` / `closes_at` are null on close —
    a closed day has no open and no close; what the record must still answer is
    *which day*.
    """
    trade_date = first.trade_date
    if first.is_trading_day:
        event = "session_open"
        opens_at, closes_at = first.start, first.end
    else:
        event = "session_closed"
        opens_at, closes_at = None, None
    _LOG.info(
        event,
        extra={
            "event": event,
            "trade_date": trade_date,
            "opens_at": opens_at,
            "closes_at": closes_at,
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


def calendar() -> TradingCalendar:
    """Recorded history plus the live window, oldest first.

    `app.loops` fetched a fourteen-day schedule every cycle — once a minute,
    for data that changes at most daily and that this module already holds,
    refreshed daily by `run`'s schedule task (#19). It spans the past because
    the question it serves is asked about the past, and the broker will not
    serve a schedule for any date before today (#45). Empty when nothing is
    known; never `None`.

    The union is taken on `trade_date` — one entry per recorded calendar date,
    closed days included, oldest first. A date in both the history and the live
    window appears once, and the live window's entry wins, matching
    `db.trading_days`' rule that the broker's most recent answer about a date is
    the one to keep.

    Until v1.81 the key was the session's start instant with `datetime.min`
    standing in wherever there was none, so every non-trading day shared one key
    and collapsed onto one entry: a fortnight containing four weekend days came
    back with one (#51). `trade_date` is a total, unique key over exactly the set
    of days the calendar is about, which is what the dedupe needed and lacked.
    `clock.trading_days_between` is unchanged by this, and §3.2 pins that: it
    counts entries with `is_trading_day` true and a non-null `start`, and the
    entries this stops discarding satisfy neither.
    """
    by_day: dict[date, SessionInfo] = {}
    # History first, then the live window, so the newer observation overwrites.
    for session in list(_history or ()) + list(_cache or ()):
        by_day[session.trade_date] = session
    return TradingCalendar(
        sessions=tuple(by_day[day] for day in sorted(by_day)),
    )


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
