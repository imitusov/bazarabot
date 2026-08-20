"""Sole owner of position row mutation. Rows are never deleted."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import aiosqlite

from zarabot.config import load
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    Position,
    Signal,
    StopProtection,
)

_ADOPT_STOP_PCT = Decimal("5")
_ADOPT_TARGET_PCT = Decimal("10")
_HUNDRED = Decimal("100")


class PositionStateError(Exception):
    """Illegal position state transition or invariant violation."""


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(load().db_path, timeout=30)
    conn.row_factory = aiosqlite.Row
    return conn


def _dec(value: object) -> Decimal:
    if value is None:
        raise PositionStateError("expected a monetary value")
    return Decimal(str(value))


def _dec_opt(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise PositionStateError("expected a timestamp")
    return datetime.fromisoformat(value)


def _dt_opt(value: object) -> datetime | None:
    if value is None:
        return None
    return _dt(value)


def _row_to_position(row: aiosqlite.Row) -> Position:
    trigger = row["exit_trigger"]
    return Position(
        id=int(row["id"]),
        ticker=row["ticker"],
        figi=row["figi"],
        strategy=row["strategy"],
        lots=int(row["lots"]),
        lot_size=int(row["lot_size"]),
        entry_price=_dec(row["entry_price"]),
        entry_at=_dt(row["entry_at"]),
        stop_price=_dec(row["stop_price"]),
        target_price=_dec(row["target_price"]),
        status=row["status"],
        adopted=bool(row["adopted"]),
        open_order_key=row["open_order_key"],
        close_order_key=row["close_order_key"],
        exit_trigger=ExitTrigger(trigger) if trigger is not None else None,
        exit_price=_dec_opt(row["exit_price"]),
        exit_at=_dt_opt(row["exit_at"]),
        realised_pnl=_dec_opt(row["realised_pnl"]),
        stop_protection=StopProtection(row["stop_protection"]),
        stop_order_key=row["stop_order_key"],
    )


async def open(
    signal: Signal,
    order: OrderRecord,
    instrument: Instrument,
    stop: Decimal,
    target: Decimal,
    opened_at: datetime,
) -> Position:
    """Insert an open LOCAL-protected position. Raises if the ticker is already open."""
    _reject_naive(opened_at)
    _reject_naive(signal.generated_at)
    lots = order.filled_lots if order.filled_lots is not None else order.lots
    if order.filled_price is None:
        raise PositionStateError("cannot open a position without a fill price")
    conn = await _connect()
    try:
        try:
            cursor = await conn.execute(
                """
                INSERT INTO positions (
                    ticker, figi, strategy, lots, lot_size, entry_price, entry_at,
                    stop_price, target_price, status, adopted, open_order_key,
                    close_order_key, exit_trigger, exit_price, exit_at,
                    realised_pnl, stop_protection, stop_order_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0, ?, NULL, NULL,
                          NULL, NULL, NULL, 'LOCAL', NULL)
                """,
                (
                    signal.ticker,
                    instrument.figi,
                    signal.strategy,
                    lots,
                    instrument.lot,
                    str(order.filled_price),
                    opened_at.isoformat(),
                    str(stop),
                    str(target),
                    order.key,
                ),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            raise PositionStateError(
                f"open position already exists for {signal.ticker}"
            ) from exc
        position_id = cursor.lastrowid
        if position_id is None:
            raise PositionStateError("insert did not assign an id")
        loaded = await get(position_id)
        if loaded is None:
            raise PositionStateError("inserted position could not be read back")
        return loaded
    finally:
        await conn.close()


async def set_stop_protection(
    position_id: int,
    protection: StopProtection,
    stop_order_key: str | None,
) -> Position:
    """Promote to EXCHANGE with a key, or return to LOCAL without one."""
    if protection is StopProtection.EXCHANGE and not stop_order_key:
        raise PositionStateError("EXCHANGE stop protection requires a stop order key")
    if protection is StopProtection.LOCAL and stop_order_key:
        raise PositionStateError("LOCAL stop protection forbids a stop order key")
    conn = await _connect()
    try:
        cursor = await conn.execute(
            """
            UPDATE positions
            SET stop_protection = ?, stop_order_key = ?
            WHERE id = ?
            """,
            (protection.value, stop_order_key, position_id),
        )
        await conn.commit()
        if cursor.rowcount != 1:
            raise PositionStateError(f"position {position_id} is absent")
        loaded = await get(position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded
    finally:
        await conn.close()


async def close(
    position_id: int,
    trigger: ExitTrigger,
    exit_price: Decimal,
    closed_at: datetime,
    order: OrderRecord,
) -> Position:
    """Atomically close an open position. Never deletes the row."""
    _reject_naive(closed_at)
    existing = await get(position_id)
    if existing is None:
        raise PositionStateError(f"position {position_id} is absent")
    if existing.status != "OPEN":
        raise PositionStateError(f"position {position_id} is already closed")
    units = Decimal(existing.lots * existing.lot_size)
    commission = order.commission if order.commission is not None else Decimal("0")
    realised = (exit_price - existing.entry_price) * units - commission
    conn = await _connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            """
            UPDATE positions
            SET status = 'CLOSED',
                exit_trigger = ?,
                exit_price = ?,
                exit_at = ?,
                realised_pnl = ?,
                close_order_key = ?,
                stop_protection = 'LOCAL',
                stop_order_key = NULL
            WHERE id = ? AND status = 'OPEN'
            """,
            (
                trigger.value,
                str(exit_price),
                closed_at.isoformat(),
                str(realised),
                order.key,
                position_id,
            ),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            raise PositionStateError(
                f"position {position_id} is already closed or absent"
            )
        await conn.commit()
        loaded = await get(position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded
    except PositionStateError:
        raise
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def list_open() -> list[Position]:
    """All open positions. Empty list when none; never None."""
    conn = await _connect()
    try:
        cursor = await conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [_row_to_position(row) for row in rows]
    finally:
        await conn.close()


async def get(position_id: int) -> Position | None:
    """Return the position or None when absent."""
    conn = await _connect()
    try:
        cursor = await conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_position(row)
    finally:
        await conn.close()


async def adopt(
    instrument: Instrument,
    lots: int,
    average_price: Decimal,
    adopted_at: datetime,
) -> Position:
    """Open a LOCAL position for a broker holding unknown locally."""
    _reject_naive(adopted_at)
    stop = average_price * (_HUNDRED - _ADOPT_STOP_PCT) / _HUNDRED
    target = average_price * (_HUNDRED + _ADOPT_TARGET_PCT) / _HUNDRED
    conn = await _connect()
    try:
        try:
            cursor = await conn.execute(
                """
                INSERT INTO positions (
                    ticker, figi, strategy, lots, lot_size, entry_price, entry_at,
                    stop_price, target_price, status, adopted, open_order_key,
                    close_order_key, exit_trigger, exit_price, exit_at,
                    realised_pnl, stop_protection, stop_order_key
                ) VALUES (?, ?, 'ADOPTED', ?, ?, ?, ?, ?, ?, 'OPEN', 1, ?, NULL,
                          NULL, NULL, NULL, NULL, 'LOCAL', NULL)
                """,
                (
                    instrument.ticker,
                    instrument.figi,
                    lots,
                    instrument.lot,
                    str(average_price),
                    adopted_at.isoformat(),
                    str(stop),
                    str(target),
                    f"ADOPTED-{instrument.figi}",
                ),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            raise PositionStateError(
                f"open position already exists for {instrument.ticker}"
            ) from exc
        position_id = cursor.lastrowid
        if position_id is None:
            raise PositionStateError("insert did not assign an id")
        loaded = await get(position_id)
        if loaded is None:
            raise PositionStateError("inserted position could not be read back")
        return loaded
    finally:
        await conn.close()


async def update_lots(position_id: int, lots: int) -> Position:
    """Write the broker's lot count onto an open position."""
    if lots <= 0:
        raise PositionStateError("lots must be positive")
    existing = await get(position_id)
    if existing is None:
        raise PositionStateError(f"position {position_id} is absent")
    if existing.status != "OPEN":
        raise PositionStateError(f"position {position_id} is already closed")
    conn = await _connect()
    try:
        cursor = await conn.execute(
            "UPDATE positions SET lots = ? WHERE id = ? AND status = 'OPEN'",
            (lots, position_id),
        )
        await conn.commit()
        if cursor.rowcount != 1:
            raise PositionStateError(
                f"position {position_id} is already closed or absent"
            )
        loaded = await get(position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded
    finally:
        await conn.close()
