"""Sole owner of the `trading_days` table. What the broker said, when it said it."""

from __future__ import annotations

import logging
from datetime import date, datetime

import aiosqlite

from zarabot.clock import moscow_date
from zarabot.clock import now as clock_now
from zarabot.db.connection import shared, transaction
from zarabot.models import SessionInfo

_LOG = logging.getLogger(__name__)


def _conn() -> aiosqlite.Connection:
    return shared()


def _dt_opt(raw: object) -> datetime | None:
    return datetime.fromisoformat(str(raw)) if raw is not None else None


def _row_to_session(row: aiosqlite.Row) -> SessionInfo:
    return SessionInfo(
        start=_dt_opt(row["session_start"]),
        end=_dt_opt(row["session_end"]),
        is_trading_day=bool(row["is_trading_day"]),
    )


def _key(session: SessionInfo) -> date | None:
    """The Moscow calendar date a session belongs to, or None when undated.

    A non-trading day carries no timestamps by contract, so it cannot be keyed
    and is skipped rather than stored under a null date. Nothing is lost:
    `clock.trading_days_between` counts only trading days, and coverage is
    defined by the oldest recorded *trading* day, which is exactly the boundary
    below which a count would be short.
    """
    return moscow_date(session.start) if session.start is not None else None


async def record_many(sessions: list[SessionInfo]) -> int:
    """Upsert one row per day, newer observation winning. One transaction."""
    observed = clock_now().isoformat()
    written = 0
    async with transaction() as conn:
        for session in sessions:
            day = _key(session)
            if day is None:
                continue
            await conn.execute(
                """
                INSERT INTO trading_days (
                    trade_date, is_trading_day, session_start, session_end,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (trade_date) DO UPDATE SET
                    is_trading_day = excluded.is_trading_day,
                    session_start  = excluded.session_start,
                    session_end    = excluded.session_end,
                    observed_at    = excluded.observed_at
                """,
                (
                    day.isoformat(),
                    1 if session.is_trading_day else 0,
                    session.start.isoformat() if session.start else None,
                    session.end.isoformat() if session.end else None,
                    observed,
                ),
            )
            written += 1
    return written


async def list_since(start: date) -> list[SessionInfo]:
    """Recorded days from `start` onwards, oldest first. Empty list when none."""
    cursor = await _conn().execute(
        "SELECT * FROM trading_days WHERE trade_date >= ? ORDER BY trade_date ASC",
        (start.isoformat(),),
    )
    rows = await cursor.fetchall()
    return [_row_to_session(row) for row in rows]


async def earliest() -> date | None:
    """The oldest recorded date, or None when nothing has been recorded."""
    cursor = await _conn().execute("SELECT MIN(trade_date) AS first FROM trading_days")
    row = await cursor.fetchone()
    if row is None or row["first"] is None:
        return None
    return date.fromisoformat(str(row["first"]))
