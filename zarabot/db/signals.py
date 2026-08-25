"""Records every signal with its risk decision. Sole owner of `signals`."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

import aiosqlite

from zarabot.clock import moscow_date
from zarabot.db.connection import shared
from zarabot.models import RejectionReason, RiskDecision, Side, Signal

_LOG = logging.getLogger(__name__)


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


def _row_to_pair(row: aiosqlite.Row) -> tuple[Signal, RiskDecision]:
    signal = Signal(
        ticker=row["ticker"],
        strategy=row["strategy"],
        side=Side.BUY,
        generated_at=datetime.fromisoformat(row["generated_at"]),
        reference_price=Decimal(str(row["reference_price"])),
    )
    if row["decision"] == "APPROVED":
        decision = RiskDecision(approved=True, lots=int(row["lots"]), reason=None)
    else:
        decision = RiskDecision(
            approved=False,
            lots=None,
            reason=RejectionReason(row["rejection_reason"]),
        )
    return signal, decision


async def record(signal: Signal, decision: RiskDecision) -> None:
    """Store every signal, approved or rejected, with its reason."""
    if decision.approved:
        values: tuple[str | int | None, ...] = (
            signal.ticker,
            signal.strategy,
            signal.generated_at.isoformat(),
            str(signal.reference_price),
            "APPROVED",
            None,
            decision.lots,
        )
    else:
        reason = decision.reason.value if decision.reason is not None else None
        values = (
            signal.ticker,
            signal.strategy,
            signal.generated_at.isoformat(),
            str(signal.reference_price),
            "REJECTED",
            reason,
            None,
        )
    conn = _conn()
    try:
        await conn.execute(
            """
            INSERT INTO signals (
                ticker, strategy, generated_at, reference_price,
                decision, rejection_reason, lots, order_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            values,
        )
        await conn.commit()
    except aiosqlite.Error:
        _LOG.exception("signal write failed for %s", signal.ticker)


async def list_for_period(start: date, end: date) -> list[tuple[Signal, RiskDecision]]:
    """Signals whose Moscow date falls in [start, end], oldest first."""
    cursor = await _conn().execute(
        "SELECT * FROM signals ORDER BY generated_at ASC, id ASC"
    )
    rows = await cursor.fetchall()
    found: list[tuple[Signal, RiskDecision]] = []
    for row in rows:
        generated = datetime.fromisoformat(row["generated_at"])
        day = moscow_date(generated)
        if start <= day <= end:
            found.append(_row_to_pair(row))
    return found
