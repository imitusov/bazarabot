"""Tests for zarabot.market.session — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
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
    return SessionInfo(
        trade_date=date(2026, 3, 16), start=OPEN, end=CLOSE, is_trading_day=True
    )


def _closed(day: date) -> SessionInfo:
    """The only shape `get_trading_schedule` can return for a closed day.

    `trade_date` set, `start` and `end` `None` (v1.81). Until then `_saturday`
    and `_holiday` here gave closed days session times — a shape the producer
    cannot emit — so every closed entry had a distinct sort key in the tests and
    collapsed only in production. That fixture is the reason #51 survived; the
    contract on closed-day fixtures in this file is pinned by
    `test_no_closed_day_fixture_in_this_file_carries_session_times` below.
    """
    return SessionInfo(trade_date=day, start=None, end=None, is_trading_day=False)


def _saturday() -> SessionInfo:
    return _closed(date(2026, 3, 14))


def _holiday() -> SessionInfo:
    return _closed(date(2026, 3, 9))


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
    if not trading:
        return _closed(date(2026, 3, n))
    return SessionInfo(
        trade_date=date(2026, 3, n),
        start=base.replace(hour=6, minute=50),
        end=base.replace(hour=15, minute=50),
        is_trading_day=True,
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
    assert len(recorded) == 14, "every day of the fortnight, closed ones included"
    trading = [day for day in recorded if day.is_trading_day]
    assert len(trading) == 10, "ten weekdays in the fortnight"


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

    dates = [s.trade_date for s in calendar().sessions]
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


async def test_a_programming_error_in_history_write_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename or type error must not look like an unmeasurable calendar.

    `_remember` used to catch `Exception`, so AttributeError from `record_many`
    was logged and dropped; `covers()` then returned False and MAX_AGE stayed
    suppressed (F-52).
    """

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_weekday()]

    async def _rename(sessions: list[SessionInfo]) -> int:
        raise AttributeError("record_many has no attribute 'foo'")

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    monkeypatch.setattr("zarabot.market.session.record_many", _rename)

    with pytest.raises(AttributeError, match="record_many has no attribute"):
        await refresh(7)


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
            SessionInfo(
                trade_date=date(2026, 3, 17),
                start=next_open_start,
                end=next_open_end,
                is_trading_day=True,
            ),
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


def _session_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) in ("session_open", "session_closed")
    ]


async def test_successful_refresh_of_a_trading_day_emits_session_open(
    store: list[SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v1.61: first day of the window is a trading session → session_open."""

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_weekday()]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    with caplog.at_level(logging.INFO, logger="zarabot.market.session"):
        await refresh(7)

    events = _session_events(caplog)
    assert len(events) == 1
    record = events[0]
    assert record.event == "session_open"
    assert record.levelno == logging.INFO
    assert record.trade_date == date(2026, 3, 16)
    assert record.opens_at == OPEN
    assert record.closes_at == CLOSE


async def test_successful_refresh_of_a_holiday_emits_session_closed(
    store: list[SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The holiday case is built from the shape the broker actually returns: a
    closed day with a `trade_date` and no session times (v1.81). The record still
    names the day it is talking about — a `session_closed` whose `trade_date` is
    also null says only that some unspecified day was shut."""

    closed = _closed(date(2026, 3, 9))

    async def _fetch(days: int) -> list[SessionInfo]:
        return [closed, _weekday()]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    with caplog.at_level(logging.INFO, logger="zarabot.market.session"):
        await refresh(7)

    events = _session_events(caplog)
    assert len(events) == 1
    record = events[0]
    assert record.event == "session_closed"
    assert record.levelno == logging.INFO
    assert record.trade_date == date(2026, 3, 9)
    assert record.opens_at is None
    assert record.closes_at is None


async def test_session_open_trade_date_is_moscow_not_utc(
    store: list[SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """21:30 UTC is the next calendar date in Moscow (v1.67). As of v1.81 the
    conversion under test is `broker.client`'s; this module reports
    `first.trade_date`, and the fixture supplies the Moscow date the broker
    would have sent."""

    start = datetime(2026, 3, 16, 21, 30, tzinfo=UTC)
    end = datetime(2026, 3, 17, 6, 40, tzinfo=UTC)

    async def _fetch(days: int) -> list[SessionInfo]:
        return [
            SessionInfo(
                trade_date=date(2026, 3, 17),
                start=start,
                end=end,
                is_trading_day=True,
            )
        ]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    with caplog.at_level(logging.INFO, logger="zarabot.market.session"):
        await refresh(7)

    events = _session_events(caplog)
    assert len(events) == 1
    record = events[0]
    assert record.event == "session_open"
    assert record.trade_date == date(2026, 3, 17)
    assert record.opens_at == start
    assert record.closes_at == end


async def test_unavailable_refresh_does_not_emit_session_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from zarabot.broker.client import BrokerUnavailable

    async def _fail(days: int) -> list[SessionInfo]:
        raise BrokerUnavailable("broker unavailable")

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fail)
    with caplog.at_level(logging.INFO, logger="zarabot.market.session"):
        await refresh(7)
    assert _session_events(caplog) == []


# --------------------------------------------------------------------------
# #51 — the calendar is the calendar, and the events name the day they describe.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,expected_event",
    [(_day(16), "session_open"), (_closed(date(2026, 3, 21)), "session_closed")],
)
async def test_both_events_report_first_trade_date_verbatim(
    store: list[SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    first: SessionInfo,
    expected_event: str,
) -> None:
    """`clock.now` is patched to a Moscow date nowhere near the window, so a
    record derived from the clock cannot pass. v1.67 left two producers of this
    one field standing — `first.trade_date` and `clock.moscow_date(clock.now())`
    — and they separate whenever a refresh straddles Moscow midnight. The
    surviving source is the entry being described (v1.81)."""
    elsewhere = datetime(2025, 12, 31, 22, 30, tzinfo=UTC)  # 1 Jan 2026 in Moscow

    async def _fetch(days: int) -> list[SessionInfo]:
        # A window with no trading session at all is "unavailable" under rule 10
        # and emits nothing, so the closed day leads a window that has one.
        return [first, _day(23)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    monkeypatch.setattr("zarabot.clock.now", lambda: elsewhere)
    with caplog.at_level(logging.INFO, logger="zarabot.market.session"):
        await refresh(7)

    events = _session_events(caplog)
    assert len(events) == 1
    assert events[0].event == expected_event
    assert events[0].trade_date == first.trade_date
    assert events[0].trade_date != date(2026, 1, 1)


async def test_a_fortnight_with_four_weekend_days_yields_fourteen_entries(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#51 returned one. Every non-trading day shared the `datetime.min` sort
    key and collapsed onto a single entry, so `calendar()` returned something
    that was not the calendar."""

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 30)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    await refresh(14)

    sessions = calendar().sessions
    assert len(sessions) == 14
    closed = [s for s in sessions if not s.is_trading_day]
    assert len(closed) == 4
    assert [s.trade_date for s in closed] == [
        date(2026, 3, 21),
        date(2026, 3, 22),
        date(2026, 3, 28),
        date(2026, 3, 29),
    ]
    dates = [s.trade_date for s in sessions]
    assert dates == sorted(dates)
    assert len(set(dates)) == 14


async def test_a_date_in_both_history_and_the_live_window_appears_once(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedupe the collapse was hiding inside still works, and the newer
    answer about a date wins — as `db.trading_days` requires.

    History and cache diverge on the production path the contract already
    describes: the second window's write fails with `aiosqlite.Error`, which is
    logged and does not propagate, so `_history` keeps the older observation
    while `_cache` holds the newer one.
    """
    short_end = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
    revised = SessionInfo(
        trade_date=date(2026, 3, 18),
        start=datetime(2026, 3, 18, 6, 50, tzinfo=UTC),
        end=short_end,
        is_trading_day=True,
    )

    async def _first(days: int) -> list[SessionInfo]:
        return [_day(17), _day(18)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _first)
    await refresh(2)

    async def _second(days: int) -> list[SessionInfo]:
        return [revised, _day(19)]

    async def _boom(sessions: list[SessionInfo]) -> int:
        raise aiosqlite.Error("disk full")

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _second)
    monkeypatch.setattr("zarabot.market.session.record_many", _boom)
    await refresh(2)

    sessions = calendar().sessions
    dates = [s.trade_date for s in sessions]
    assert dates == [date(2026, 3, 17), date(2026, 3, 18), date(2026, 3, 19)]
    assert dates.count(date(2026, 3, 18)) == 1
    revised_entry = next(s for s in sessions if s.trade_date == date(2026, 3, 18))
    assert revised_entry.end == short_end, "the live observation wins"


async def test_trading_days_between_is_unchanged_by_carrying_closed_days(
    store: list[SessionInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A requirement, not a hope: the entries this fix stops discarding have
    `is_trading_day` false and a null `start`, and the count reads neither, so
    the same window must yield the same count as it did before. `MAX_AGE` is
    what this measures, so a quiet change here would be a financial defect."""
    from zarabot.clock import trading_days_between

    async def _fetch(days: int) -> list[SessionInfo]:
        return [_day(n) for n in range(16, 30)]

    monkeypatch.setattr("zarabot.market.session.get_trading_schedule", _fetch)
    await refresh(14)

    full = calendar()
    assert len(full.sessions) == 14
    trading_only = TradingCalendar(
        sessions=tuple(s for s in full.sessions if s.is_trading_day)
    )
    assert len(trading_only.sessions) == 10, "what the collapsed calendar counted"

    entry = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
    now = datetime(2026, 3, 27, 12, 0, tzinfo=UTC)
    assert trading_days_between(entry, now, full) == trading_days_between(
        entry, now, trading_only
    )
    assert trading_days_between(entry, now, full) == 9


def test_no_closed_day_fixture_in_this_file_carries_session_times() -> None:
    """A contract on the fixtures, not a behaviour (v1.81).

    The impossible fixture is the whole reason #51 survived: `_saturday` and
    `_holiday` gave closed days a start and an end, a shape
    `get_trading_schedule` can never return, so every closed entry had a
    distinct sort key here and collapsed only in production (failure class 9).
    A promise would rot; this reads the file.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "SessionInfo"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        flag = kwargs.get("is_trading_day")
        if not (isinstance(flag, ast.Constant) and flag.value is False):
            continue
        checked += 1
        for field in ("start", "end"):
            value = kwargs.get(field)
            assert isinstance(value, ast.Constant) and value.value is None, (
                f"a non-trading SessionInfo in this file sets {field}; "
                "get_trading_schedule cannot return that shape"
            )
        assert "trade_date" in kwargs, "a closed day must still say which day"
    assert checked >= 1, "no closed-day fixture found — has the guard gone stale?"
