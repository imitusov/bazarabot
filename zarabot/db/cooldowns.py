"""Sole owner of per-instrument re-entry cooldown timestamps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiosqlite

from zarabot.db.connection import shared

_LOG = logging.getLogger(__name__)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


async def _started_at(ticker: str) -> datetime | None:
    cursor = await _conn().execute(
        "SELECT started_at FROM cooldowns WHERE ticker = ?", (ticker,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["started_at"])


async def start(ticker: str, at: datetime) -> None:
    """Record the cooldown start, keeping the newer instant if one exists."""
    _reject_naive(at)
    conn = _conn()
    try:
        cursor = await conn.execute(
            "SELECT started_at FROM cooldowns WHERE ticker = ?", (ticker,)
        )
        row = await cursor.fetchone()
        if row is not None:
            existing = datetime.fromisoformat(row["started_at"])
            if existing >= at:
                return
            await conn.execute(
                "UPDATE cooldowns SET started_at = ? WHERE ticker = ?",
                (at.isoformat(), ticker),
            )
        else:
            await conn.execute(
                "INSERT INTO cooldowns (ticker, started_at) VALUES (?, ?)",
                (ticker, at.isoformat()),
            )
        await conn.commit()
    except aiosqlite.Error:
        _LOG.exception("cooldown write failed for %s", ticker)


async def is_active(ticker: str, now: datetime, minutes: int) -> bool:
    """True while now - started_at is strictly less than minutes."""
    _reject_naive(now)
    started = await _started_at(ticker)
    if started is None:
        return False
    return now - started < timedelta(minutes=minutes)


async def active_until(ticker: str, minutes: int) -> datetime | None:
    """End of the cooldown window, or None when no cooldown is recorded."""
    started = await _started_at(ticker)
    if started is None:
        return None
    return started + timedelta(minutes=minutes)
