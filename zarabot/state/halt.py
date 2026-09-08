"""Sole owner of the persisted halt flag. Entries only; exits keep running."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import aiosqlite

from zarabot.db.connection import shared, transaction
from zarabot.models import HaltReason, HaltState
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)

# Severity order: DAILY_LOSS_LIMIT > RECONCILIATION_MISMATCH > MANUAL. A halt
# for a strictly more severe reason replaces a weaker one; the same reason or a
# weaker one is a no-op, so re-halting adds no alert noise (#9).
_SEVERITY: dict[HaltReason, int] = {
    HaltReason.MANUAL: 1,
    HaltReason.RECONCILIATION_MISMATCH: 2,
    HaltReason.DAILY_LOSS_LIMIT: 3,
}


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


async def halt(
    reason: HaltReason,
    detail: str,
    at: datetime,
    daily_loss_pct: Decimal | None = None,
) -> None:
    _reject_naive(at)
    conn = _conn()
    cursor = await conn.execute("SELECT halted, reason FROM halt_state WHERE id = 1")
    row = await cursor.fetchone()
    standing: HaltReason | None = None
    replacing = False
    if row is not None and row["halted"]:
        standing = HaltReason(row["reason"]) if row["reason"] else None
        if standing is not None and _SEVERITY[reason] <= _SEVERITY[standing]:
            return
        replacing = True
    async with transaction() as conn:
        if replacing:
            # The halt has been in force since its original `halted_at`; only
            # the reason and its detail are superseded.
            await conn.execute(
                "UPDATE halt_state SET reason = ?, detail = ? WHERE id = 1",
                (reason.value, detail),
            )
        else:
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
    extra: dict[str, object] = {
        "event": "halt_triggered",
        "reason": reason.value,
        "detail": detail,
    }
    if daily_loss_pct is not None:
        extra["daily_loss_pct"] = daily_loss_pct
    _LOG.critical("halt_triggered", extra=extra)
    if replacing:
        previous = standing.value if standing is not None else "an unrecorded reason"
        await alert(f"Halt escalated from {previous} to {reason.value}: {detail}")


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
    _LOG.info("halt_cleared", extra={"event": "halt_cleared", "actor": actor})
    return True
