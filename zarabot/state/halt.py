"""Sole owner of the persisted halt flag. Entries only; exits keep running."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from zarabot.db.connection import shared, transaction
from zarabot.models import HaltReason, HaltState


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


def _from_row(row: aiosqlite.Row) -> HaltState:
    reason_raw = row["reason"]
    halted_at = row["halted_at"]
    resumed_at = row["resumed_at"]
    return HaltState(
        halted=bool(row["halted"]),
        reason=HaltReason(reason_raw) if reason_raw else None,
        detail=row["detail"],
        halted_at=datetime.fromisoformat(halted_at) if halted_at else None,
        resumed_at=datetime.fromisoformat(resumed_at) if resumed_at else None,
        resumed_by=row["resumed_by"],
    )


async def current() -> HaltState | None:
    cursor = await _conn().execute("SELECT * FROM halt_state WHERE id = 1")
    row = await cursor.fetchone()
    if row is None:
        return None
    return _from_row(row)


async def is_halted() -> bool:
    state = await current()
    return state is not None and state.halted


async def halt(reason: HaltReason, detail: str, at: datetime) -> None:
    _reject_naive(at)
    conn = _conn()
    cursor = await conn.execute("SELECT halted FROM halt_state WHERE id = 1")
    row = await cursor.fetchone()
    if row is not None and row["halted"]:
        return
    async with transaction() as conn:
        await conn.execute(
            """
            UPDATE halt_state
            SET halted = 1,
                reason = ?,
                detail = ?,
                halted_at = ?,
                resumed_at = NULL,
                resumed_by = NULL
            WHERE id = 1
            """,
            (reason.value, detail, at.isoformat()),
        )


async def resume(actor: str, at: datetime) -> bool:
    _reject_naive(at)
    conn = _conn()
    cursor = await conn.execute("SELECT halted FROM halt_state WHERE id = 1")
    row = await cursor.fetchone()
    if row is None or not row["halted"]:
        return False
    async with transaction() as conn:
        await conn.execute(
            """
            UPDATE halt_state
            SET halted = 0,
                resumed_at = ?,
                resumed_by = ?
            WHERE id = 1
            """,
            (at.isoformat(), actor),
        )
    return True
