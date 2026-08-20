"""Sole owner of the persisted halt flag. Entries only; exits keep running."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from zarabot.config import load
from zarabot.models import HaltReason, HaltState


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(load().db_path, timeout=30)
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
    conn = await _connect()
    try:
        cursor = await conn.execute("SELECT * FROM halt_state WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            return None
        return _from_row(row)
    finally:
        await conn.close()


async def is_halted() -> bool:
    state = await current()
    return state is not None and state.halted


async def halt(reason: HaltReason, detail: str, at: datetime) -> None:
    _reject_naive(at)
    conn = await _connect()
    try:
        cursor = await conn.execute("SELECT halted FROM halt_state WHERE id = 1")
        row = await cursor.fetchone()
        if row is not None and row["halted"]:
            return
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
        await conn.commit()
    finally:
        await conn.close()


async def resume(actor: str, at: datetime) -> bool:
    _reject_naive(at)
    conn = await _connect()
    try:
        cursor = await conn.execute("SELECT halted FROM halt_state WHERE id = 1")
        row = await cursor.fetchone()
        if row is None or not row["halted"]:
            return False
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
        await conn.commit()
        return True
    finally:
        await conn.close()
