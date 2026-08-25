"""Sole owner of order rows and of order status transitions."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import aiosqlite

from zarabot.clock import now
from zarabot.db.connection import shared
from zarabot.models import ExitTrigger, OrderRecord, OrderStatus, Side

_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED})
_write_lock = asyncio.Lock()


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


class DuplicateOrderError(Exception):
    """Idempotency key already exists."""


class OrderStateError(Exception):
    """Illegal order status transition."""


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


def _dec_opt(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


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


def _row_to_order(row: aiosqlite.Row) -> OrderRecord:
    trigger_raw = row["exit_trigger"]
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
        exit_trigger=ExitTrigger(trigger_raw) if trigger_raw else None,
    )


async def _load(conn: aiosqlite.Connection, key: str) -> OrderRecord | None:
    cursor = await conn.execute("SELECT * FROM orders WHERE key = ?", (key,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_order(row)


async def record_submitting(
    key: str,
    ticker: str,
    side: Side,
    lots: int,
    intent: str,
    exit_trigger: ExitTrigger | None = None,
) -> OrderRecord:
    """Persist intent before the broker is called. Must complete before post_order."""
    if intent == "EXIT":
        if exit_trigger is None:
            raise ValueError("EXIT requires exit_trigger")
        if exit_trigger is ExitTrigger.EXTERNAL:
            raise ValueError("EXIT exit_trigger cannot be EXTERNAL")
    elif exit_trigger is not None:
        raise ValueError("ENTRY forbids exit_trigger")
    async with _write_lock:
        conn = _conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                await conn.execute(
                    """
                    INSERT INTO orders (
                        key, ticker, figi, side, intent, lots, status,
                        filled_lots, filled_price, commission, broker_reason,
                        created_at, settled_at, exit_trigger
                    ) VALUES (
                        ?, ?, '', ?, ?, ?, 'SUBMITTING',
                        NULL, NULL, NULL, NULL, ?, NULL, ?
                    )
                    """,
                    (
                        key,
                        ticker,
                        side.value,
                        intent,
                        lots,
                        now().isoformat(),
                        exit_trigger.value if exit_trigger is not None else None,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise DuplicateOrderError(f"order key already exists: {key}") from exc
            loaded = await _load(conn, key)
            if loaded is None:
                raise OrderStateError(f"order {key} could not be read back")
            await conn.commit()
            return loaded
        except Exception:
            await conn.rollback()
            raise


async def settle(
    key: str,
    status: OrderStatus,
    filled_lots: int,
    filled_price: Decimal | None,
    commission: Decimal | None,
    broker_reason: str | None,
) -> OrderRecord:
    """Record a terminal outcome. Raises if the row is already terminal."""
    if status not in _TERMINAL:
        raise OrderStateError(f"{status} is not a terminal status")
    async with _write_lock:
        conn = _conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current = await _load(conn, key)
            if current is None:
                raise OrderStateError(f"order {key} is absent")
            if current.status in _TERMINAL:
                raise OrderStateError(f"order {key} is already {current.status.value}")
            await conn.execute(
                """
                UPDATE orders
                SET status = ?, filled_lots = ?, filled_price = ?,
                    commission = ?, broker_reason = ?, settled_at = ?
                WHERE key = ?
                """,
                (
                    status.value,
                    filled_lots,
                    str(filled_price) if filled_price is not None else None,
                    str(commission) if commission is not None else None,
                    broker_reason,
                    now().isoformat(),
                    key,
                ),
            )
            loaded = await _load(conn, key)
            if loaded is None:
                raise OrderStateError(f"order {key} is absent")
            await conn.commit()
            return loaded
        except Exception:
            await conn.rollback()
            raise


async def get(key: str) -> OrderRecord | None:
    """Return the order or None when absent. Does not commit."""
    return await _load(_conn(), key)


async def record_commission(key: str, commission: Decimal) -> OrderRecord:
    """Write commission onto an already-terminal order."""
    async with _write_lock:
        conn = _conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            current = await _load(conn, key)
            if current is None:
                raise OrderStateError(f"order {key} is absent")
            if current.status not in _TERMINAL:
                raise OrderStateError(f"order {key} is {current.status.value}")
            await conn.execute(
                "UPDATE orders SET commission = ? WHERE key = ?",
                (str(commission), key),
            )
            loaded = await _load(conn, key)
            if loaded is None:
                raise OrderStateError(f"order {key} is absent")
            await conn.commit()
            return loaded
        except Exception:
            await conn.rollback()
            raise


async def list_missing_commission(
    since: datetime, until: datetime
) -> list[OrderRecord]:
    """FILLED orders in the period whose commission is still unknown."""
    _reject_naive(since)
    _reject_naive(until)
    cursor = await _conn().execute(
        """
        SELECT * FROM orders
        WHERE status = 'FILLED'
          AND commission IS NULL
          AND settled_at >= ?
          AND settled_at <= ?
        ORDER BY settled_at ASC
        """,
        (since.isoformat(), until.isoformat()),
    )
    rows = await cursor.fetchall()
    return [_row_to_order(row) for row in rows]


async def list_unresolved() -> list[OrderRecord]:
    """Orders in SUBMITTING or SUBMITTED, oldest first."""
    cursor = await _conn().execute(
        """
        SELECT * FROM orders
        WHERE status IN ('SUBMITTING', 'SUBMITTED')
        ORDER BY created_at ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_order(row) for row in rows]
