"""Sole owner of order rows and of order status transitions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import aiosqlite

from zarabot.clock import now
from zarabot.config import load
from zarabot.models import OrderRecord, OrderStatus, Side

_TERMINAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
)


class DuplicateOrderError(Exception):
    """Idempotency key already exists."""


class OrderStateError(Exception):
    """Illegal order status transition."""


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(load().db_path, timeout=30)
    conn.row_factory = aiosqlite.Row
    return conn


def _dec_opt(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise OrderStateError("expected a timestamp")
    return datetime.fromisoformat(value)


def _dt_opt(value: object) -> datetime | None:
    if value is None:
        return None
    return _dt(value)


def _row_to_order(row: aiosqlite.Row) -> OrderRecord:
    return OrderRecord(
        key=row["key"],
        ticker=row["ticker"],
        figi=row["figi"],
        side=Side(row["side"]),
        intent=row["intent"],
        lots=int(row["lots"]),
        status=OrderStatus(row["status"]),
        filled_lots=int(row["filled_lots"]) if row["filled_lots"] is not None else None,
        filled_price=_dec_opt(row["filled_price"]),
        commission=_dec_opt(row["commission"]),
        broker_reason=row["broker_reason"],
        created_at=_dt(row["created_at"]),
        settled_at=_dt_opt(row["settled_at"]),
    )


async def record_submitting(
    key: str, ticker: str, side: Side, lots: int, intent: str
) -> OrderRecord:
    """Persist intent before the broker is called. Must complete before post_order."""
    conn = await _connect()
    try:
        try:
            await conn.execute(
                """
                INSERT INTO orders (
                    key, ticker, figi, side, intent, lots, status,
                    filled_lots, filled_price, commission, broker_reason,
                    created_at, settled_at
                ) VALUES (
                    ?, ?, '', ?, ?, ?, 'SUBMITTING',
                    NULL, NULL, NULL, NULL, ?, NULL
                )
                """,
                (key, ticker, side.value, intent, lots, now().isoformat()),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            raise DuplicateOrderError(f"order key already exists: {key}") from exc
        cursor = await conn.execute("SELECT * FROM orders WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            raise OrderStateError(f"order {key} could not be read back")
        return _row_to_order(row)
    finally:
        await conn.close()


async def settle(
    key: str,
    status: OrderStatus,
    filled_lots: int,
    filled_price: Decimal | None,
    broker_reason: str | None,
) -> OrderRecord:
    """Record a terminal outcome. Raises if the row is already terminal."""
    if status not in _TERMINAL:
        raise OrderStateError(f"{status} is not a terminal status")
    conn = await _connect()
    try:
        cursor = await conn.execute("SELECT * FROM orders WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            raise OrderStateError(f"order {key} is absent")
        current = OrderStatus(row["status"])
        if current in _TERMINAL:
            raise OrderStateError(f"order {key} is already {current.value}")
        await conn.execute(
            """
            UPDATE orders
            SET status = ?, filled_lots = ?, filled_price = ?,
                broker_reason = ?, settled_at = ?
            WHERE key = ?
            """,
            (
                status.value,
                filled_lots,
                str(filled_price) if filled_price is not None else None,
                broker_reason,
                now().isoformat(),
                key,
            ),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM orders WHERE key = ?", (key,))
        updated = await cursor.fetchone()
        if updated is None:
            raise OrderStateError(f"order {key} is absent")
        return _row_to_order(updated)
    finally:
        await conn.close()


async def list_unresolved() -> list[OrderRecord]:
    """Orders in SUBMITTING or SUBMITTED, oldest first."""
    conn = await _connect()
    try:
        cursor = await conn.execute(
            """
            SELECT * FROM orders
            WHERE status IN ('SUBMITTING', 'SUBMITTED')
            ORDER BY created_at ASC
            """
        )
        rows = await cursor.fetchall()
        return [_row_to_order(row) for row in rows]
    finally:
        await conn.close()
