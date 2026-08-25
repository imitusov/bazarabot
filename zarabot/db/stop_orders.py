"""Sole owner of stop-order rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import aiosqlite

from zarabot.clock import now
from zarabot.db.connection import shared, transaction
from zarabot.db.orders import DuplicateOrderError, OrderStateError
from zarabot.models import StopOrderRecord, StopOrderStatus

_TERMINAL = frozenset(
    {
        StopOrderStatus.CANCELLED,
        StopOrderStatus.EXECUTED,
        StopOrderStatus.ORPHANED,
        StopOrderStatus.FAILED,
    }
)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


def _dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise OrderStateError("expected a timestamp")
    parsed = datetime.fromisoformat(value)
    _reject_naive(parsed)
    return parsed


def _dt_opt(value: object) -> datetime | None:
    if value is None:
        return None
    return _dt(value)


def _row_to_stop(row: aiosqlite.Row) -> StopOrderRecord:
    return StopOrderRecord(
        key=row["key"],
        stop_order_id=row["stop_order_id"],
        position_id=int(row["position_id"]),
        ticker=row["ticker"],
        lots=int(row["lots"]),
        stop_price=Decimal(str(row["stop_price"])),
        status=StopOrderStatus(row["status"]),
        created_at=_dt(row["created_at"]),
        settled_at=_dt_opt(row["settled_at"]),
    )


async def _load(conn: aiosqlite.Connection, key: str) -> StopOrderRecord | None:
    cursor = await conn.execute("SELECT * FROM stop_orders WHERE key = ?", (key,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_stop(row)


async def record_placing(
    key: str, position_id: int, ticker: str, lots: int, stop_price: Decimal
) -> StopOrderRecord:
    """Persist intent before the broker is called."""
    async with transaction() as conn:
        existing = await _load(conn, key)
        if existing is not None:
            raise DuplicateOrderError(f"stop order key already exists: {key}")
        try:
            await conn.execute(
                """
                INSERT INTO stop_orders (
                    key, stop_order_id, position_id, ticker, lots, stop_price,
                    status, created_at, settled_at
                ) VALUES (?, NULL, ?, ?, ?, ?, 'PLACING', ?, NULL)
                """,
                (
                    key,
                    position_id,
                    ticker,
                    lots,
                    str(stop_price),
                    now().isoformat(),
                ),
            )
        except aiosqlite.IntegrityError as exc:
            if "stop_orders.key" in str(exc):
                raise DuplicateOrderError(
                    f"stop order key already exists: {key}"
                ) from exc
            raise
        loaded = await _load(conn, key)
        if loaded is None:
            raise OrderStateError(f"stop order {key} could not be read back")
        return loaded


async def activate(key: str, stop_order_id: str) -> StopOrderRecord:
    """Record the broker identifier once the stop is standing."""
    async with transaction() as conn:
        current = await _load(conn, key)
        if current is None:
            raise OrderStateError(f"stop order {key} is absent")
        if current.status in _TERMINAL:
            raise OrderStateError(f"stop order {key} is already {current.status.value}")
        await conn.execute(
            """
            UPDATE stop_orders
            SET status = 'ACTIVE', stop_order_id = ?
            WHERE key = ?
            """,
            (stop_order_id, key),
        )
        loaded = await _load(conn, key)
        if loaded is None:
            raise OrderStateError(f"stop order {key} is absent")
        return loaded


async def settle(
    key: str, status: StopOrderStatus, settled_at: datetime
) -> StopOrderRecord:
    """Record a terminal stop-order outcome."""
    _reject_naive(settled_at)
    if status not in _TERMINAL:
        raise OrderStateError(f"{status} is not a terminal stop-order status")
    async with transaction() as conn:
        current = await _load(conn, key)
        if current is None:
            raise OrderStateError(f"stop order {key} is absent")
        if current.status in _TERMINAL:
            raise OrderStateError(f"stop order {key} is already {current.status.value}")
        await conn.execute(
            """
            UPDATE stop_orders
            SET status = ?, settled_at = ?
            WHERE key = ?
            """,
            (status.value, settled_at.isoformat(), key),
        )
        loaded = await _load(conn, key)
        if loaded is None:
            raise OrderStateError(f"stop order {key} is absent")
        return loaded


async def active_for_position(position_id: int) -> StopOrderRecord | None:
    """The single standing stop for a position, or None."""
    cursor = await _conn().execute(
        """
        SELECT * FROM stop_orders
        WHERE position_id = ? AND status IN ('PLACING', 'ACTIVE')
        """,
        (position_id,),
    )
    rows = list(await cursor.fetchall())
    if len(rows) > 1:
        raise OrderStateError(f"multiple standing stops for position {position_id}")
    if not rows:
        return None
    return _row_to_stop(rows[0])


async def list_active() -> list[StopOrderRecord]:
    """Every stop believed standing (ACTIVE)."""
    cursor = await _conn().execute(
        """
        SELECT * FROM stop_orders
        WHERE status = 'ACTIVE'
        ORDER BY created_at ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_stop(row) for row in rows]
