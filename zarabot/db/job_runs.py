"""Sole owner of `job_runs`. When each periodic job last completed, per period."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from zarabot.db.connection import shared, transaction


def _conn() -> aiosqlite.Connection:
    return shared()


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def has_run(job: str, period_key: str) -> bool:
    """Whether `job` has completed for that period."""
    cursor = await _conn().execute(
        "SELECT 1 FROM job_runs WHERE job = ? AND period_key = ?",
        (job, period_key),
    )
    return await cursor.fetchone() is not None


async def mark_run(job: str, period_key: str, ran_at: datetime) -> None:
    """Record completion. Idempotent, keeping the first `ran_at`.

    When the job *first* completed is the fact worth having: it is what makes a
    run that was late distinguishable from one that repeated (#27).
    """
    _reject_naive(ran_at)
    async with transaction() as conn:
        await conn.execute(
            """
            INSERT INTO job_runs (job, period_key, ran_at) VALUES (?, ?, ?)
            ON CONFLICT (job, period_key) DO NOTHING
            """,
            (job, period_key, ran_at.isoformat()),
        )


async def last_run(job: str) -> datetime | None:
    """The most recent completion of `job`, or None when it has never run."""
    cursor = await _conn().execute(
        "SELECT MAX(ran_at) AS latest FROM job_runs WHERE job = ?", (job,)
    )
    row = await cursor.fetchone()
    if row is None or row["latest"] is None:
        return None
    return datetime.fromisoformat(str(row["latest"]))
