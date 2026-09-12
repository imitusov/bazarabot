"""Sole owner of the `trading_days` table. What the broker said, when it said it."""

from __future__ import annotations

import logging
from datetime import date, datetime

import aiosqlite

from zarabot.clock import now as clock_now
from zarabot.db.connection import shared, transaction
from zarabot.models import SessionInfo

_LOG = logging.getLogger(__name__)


def _conn() -> aiosqlite.Connection:
    return shared()


def _dt_opt(raw: object) -> datetime | None:
    return datetime.fromisoformat(str(raw)) if raw is not None else None


def _row_to_session(row: aiosqlite.Row) -> SessionInfo:
    """One recorded day. `trade_date` comes from the `trade_date` column.

    Never reconstructed from `session_start`: for a trading day the two agree by
    the producer obligation in §4 `models`, and for a closed day there is no
    `session_start` to derive anything from — which is exactly the case this
    table now holds (v1.81, #51).
    """
    return SessionInfo(
        trade_date=date.fromisoformat(str(row["trade_date"])),
        start=_dt_opt(row["session_start"]),
        end=_dt_opt(row["session_end"]),
        is_trading_day=bool(row["is_trading_day"]),
    )


async def record_many(sessions: list[SessionInfo]) -> int:
    """Upsert one row per day, newer observation winning. One transaction.

    Every day of the window is recorded, closed days included: the row is keyed
    on `SessionInfo.trade_date`, which is present on every entry, so a
    non-trading day is written with `is_trading_day = 0` and both timestamp
    columns null. §5 has declared them `TEXT NULL` since `006_trading_days.sql`,
    so this is what the table was built for and no migration is required.
    """
    observed = clock_now().isoformat()
    written = 0
    async with transaction() as conn:
        for session in sessions:
            day = session.trade_date
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
