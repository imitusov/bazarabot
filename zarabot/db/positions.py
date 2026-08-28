"""Sole owner of position rows and of ``position_events``. Rows are never deleted."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import NoReturn

import aiosqlite

from zarabot.clock import now
from zarabot.config import load
from zarabot.db.connection import shared, transaction
from zarabot.db.orders import get as get_order
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    Position,
    Signal,
    StopProtection,
)

_HUNDRED = Decimal("100")


class PositionStateError(Exception):
    """Illegal position state transition or invariant violation."""


@dataclass(frozen=True)
class PositionEvent:
    """One append-only mutation of a position, oldest-first via ``list_events``."""

    position_id: int
    occurred_at: datetime
    event: str
    detail: str


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _conn() -> aiosqlite.Connection:
    conn = shared()
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
    parsed = datetime.fromisoformat(value)
    _reject_naive(parsed)
    return parsed


def _dt_opt(value: object) -> datetime | None:
    if value is None:
        return None
    return _dt(value)


def _detail(payload: dict[str, object]) -> str:
    return json.dumps(payload, default=str)


def _reraise_unless_duplicate_position(
    exc: aiosqlite.IntegrityError, ticker: str
) -> NoReturn:
    """Only the partial unique index on open positions is a duplicate.

    Every integrity failure used to become "open position already exists",
    which turned a foreign-key violation into a plausible-sounding lie naming a
    position that did not exist, and made #42 take a reproduction to diagnose
    rather than a stack trace. Anything else propagates as itself.
    """
    if "UNIQUE" in str(exc).upper():
        raise PositionStateError(f"open position already exists for {ticker}") from exc
    raise exc


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


def _row_to_event(row: aiosqlite.Row) -> PositionEvent:
    return PositionEvent(
        position_id=int(row["position_id"]),
        occurred_at=_dt(row["occurred_at"]),
        event=str(row["event"]),
        detail=str(row["detail"]),
    )


async def _load(conn: aiosqlite.Connection, position_id: int) -> Position | None:
    cursor = await conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_position(row)


async def _insert_event(
    conn: aiosqlite.Connection,
    position_id: int,
    event: str,
    detail: dict[str, object],
) -> None:
    await conn.execute(
        """
        INSERT INTO position_events (position_id, occurred_at, event, detail)
        VALUES (?, ?, ?, ?)
        """,
        (position_id, now().isoformat(), event, _detail(detail)),
    )


async def _order_commission(key: str | None) -> Decimal:
    if not key:
        return Decimal("0")
    order = await get_order(key)
    if order is None or order.commission is None:
        return Decimal("0")
    return order.commission


def _realised(
    entry_price: Decimal,
    exit_price: Decimal,
    lots: int,
    lot_size: int,
    entry_commission: Decimal,
    exit_commission: Decimal,
) -> Decimal:
    units = Decimal(lots * lot_size)
    return (exit_price - entry_price) * units - entry_commission - exit_commission


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
    async with transaction() as conn:
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
        except aiosqlite.IntegrityError as exc:
            _reraise_unless_duplicate_position(exc, signal.ticker)
        position_id = cursor.lastrowid
        if position_id is None:
            raise PositionStateError("insert did not assign an id")
        await _insert_event(
            conn,
            int(position_id),
            "OPENED",
            {
                "ticker": signal.ticker,
                "lots": lots,
                "entry_price": str(order.filled_price),
                "open_order_key": order.key,
                "stop_price": str(stop),
                "target_price": str(target),
            },
        )
        loaded = await _load(conn, int(position_id))
        if loaded is None:
            raise PositionStateError("inserted position could not be read back")
        return loaded


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
    async with transaction() as conn:
        existing = await _load(conn, position_id)
        if existing is None:
            raise PositionStateError(f"position {position_id} is absent")
        cursor = await conn.execute(
            """
            UPDATE positions
            SET stop_protection = ?, stop_order_key = ?
            WHERE id = ?
            """,
            (protection.value, stop_order_key, position_id),
        )
        if cursor.rowcount != 1:
            raise PositionStateError(f"position {position_id} is absent")
        await _insert_event(
            conn,
            position_id,
            "STOP_PROTECTION_CHANGED",
            {
                "previous_protection": existing.stop_protection.value,
                "new_protection": protection.value,
                "previous_stop_order_key": existing.stop_order_key,
                "new_stop_order_key": stop_order_key,
            },
        )
        loaded = await _load(conn, position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded


async def close(
    position_id: int,
    trigger: ExitTrigger,
    exit_price: Decimal,
    closed_at: datetime,
    order: OrderRecord | None,
    exit_commission: Decimal | None = None,
) -> Position:
    """Atomically close an open position. Never deletes the row.

    `exit_commission` is the closing leg's fee where there is no closing order
    to read it from, which is exactly and only the EXTERNAL case (#11). It is
    stored as well as netted: a realised figure whose inputs are not all
    recorded cannot be checked afterwards, and this is the only commission in
    the system with no order row of its own to live on.
    """
    _reject_naive(closed_at)
    if trigger is ExitTrigger.EXTERNAL:
        if order is not None:
            raise ValueError("EXTERNAL close forbids an order")
    else:
        if order is None:
            raise ValueError("non-EXTERNAL close requires an order")
        if exit_commission is not None:
            raise ValueError("exit_commission belongs to an EXTERNAL close only")
    async with transaction() as conn:
        existing = await _load(conn, position_id)
        if existing is None:
            raise PositionStateError(f"position {position_id} is absent")
        if existing.status != "OPEN":
            raise PositionStateError(f"position {position_id} is already closed")
        if order is not None:
            closing_commission = (
                order.commission if order.commission is not None else Decimal("0")
            )
        else:
            closing_commission = (
                exit_commission if exit_commission is not None else Decimal("0")
            )
        entry_commission = await _order_commission(existing.open_order_key)
        realised = _realised(
            existing.entry_price,
            exit_price,
            existing.lots,
            existing.lot_size,
            entry_commission,
            closing_commission,
        )
        close_key = order.key if order is not None else None
        cursor = await conn.execute(
            """
            UPDATE positions
            SET status = 'CLOSED',
                exit_trigger = ?,
                exit_price = ?,
                exit_at = ?,
                realised_pnl = ?,
                close_order_key = ?,
                exit_commission = ?,
                stop_protection = 'LOCAL',
                stop_order_key = NULL
            WHERE id = ? AND status = 'OPEN'
            """,
            (
                trigger.value,
                str(exit_price),
                closed_at.isoformat(),
                str(realised),
                close_key,
                str(exit_commission) if exit_commission is not None else None,
                position_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PositionStateError(
                f"position {position_id} is already closed or absent"
            )
        await _insert_event(
            conn,
            position_id,
            "CLOSED",
            {
                "exit_trigger": trigger.value,
                "exit_price": str(exit_price),
                "realised_pnl": str(realised),
                "close_order_key": close_key,
                "exit_commission": (
                    str(exit_commission) if exit_commission is not None else None
                ),
                "previous_stop_protection": existing.stop_protection.value,
                "new_stop_protection": StopProtection.LOCAL.value,
            },
        )
        loaded = await _load(conn, position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded


async def list_open() -> list[Position]:
    """All open positions. Empty list when none; never None."""
    conn = _conn()
    cursor = await conn.execute(
        "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [_row_to_position(row) for row in rows]


async def list_closed() -> list[Position]:
    """Closed positions, newest exit first. Empty list when none; never None."""
    conn = _conn()
    cursor = await conn.execute(
        """
        SELECT * FROM positions
        WHERE status = 'CLOSED'
        ORDER BY exit_at DESC, id DESC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_position(row) for row in rows]


async def get(position_id: int) -> Position | None:
    """Return the position or None when absent."""
    return await _load(_conn(), position_id)


async def _stored_exit_commission(
    conn: aiosqlite.Connection, position_id: int
) -> Decimal:
    cursor = await conn.execute(
        "SELECT exit_commission FROM positions WHERE id = ?", (position_id,)
    )
    row = await cursor.fetchone()
    if row is None or row["exit_commission"] is None:
        return Decimal("0")
    return _dec(row["exit_commission"])


async def recompute_realised(position_id: int) -> Position:
    """Rewrite realised_pnl for a closed position from current order commissions."""
    async with transaction() as conn:
        existing = await _load(conn, position_id)
        if existing is None:
            raise PositionStateError(f"position {position_id} is absent")
        if existing.status != "CLOSED" or existing.exit_price is None:
            raise PositionStateError(f"position {position_id} is not closed")
        if existing.exit_trigger is ExitTrigger.EXTERNAL:
            # No closing order to re-read. Dropping the stored fee to zero would
            # turn a figure the operations feed resolved back into the
            # overstated one — a recomputation that makes a number worse is the
            # failure this function exists to prevent (#11).
            closing_commission = await _stored_exit_commission(conn, position_id)
        else:
            closing_commission = await _order_commission(existing.close_order_key)
        realised = _realised(
            existing.entry_price,
            existing.exit_price,
            existing.lots,
            existing.lot_size,
            await _order_commission(existing.open_order_key),
            closing_commission,
        )
        cursor = await conn.execute(
            """
            UPDATE positions
            SET realised_pnl = ?
            WHERE id = ? AND status = 'CLOSED'
            """,
            (str(realised), position_id),
        )
        if cursor.rowcount != 1:
            raise PositionStateError(f"position {position_id} is not closed")
        await _insert_event(
            conn,
            position_id,
            "REALISED_RECOMPUTED",
            {
                "previous_realised_pnl": str(existing.realised_pnl),
                "new_realised_pnl": str(realised),
            },
        )
        loaded = await _load(conn, position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded


async def adopt(
    instrument: Instrument,
    lots: int,
    average_price: Decimal,
    adopted_at: datetime,
    open_order_key: str,
) -> Position:
    """Open a LOCAL position for a broker holding whose local row is missing.

    `open_order_key` is the bot's own unresolved ENTRY order for the ticker.
    This used to synthesise `ADOPTED-{figi}`, a key with no order row, while the
    schema has required `open_order_key REFERENCES orders (key)` since 001 —
    so once foreign keys were actually enforced this function could not insert
    at all (#42). The real key was available the whole time: since v1.25 this is
    reached only for a holding recognised by such an order, so one always exists,
    and pointing at it also makes the entry commission recoverable through
    `db.orders.get`.
    """
    _reject_naive(adopted_at)
    if lots <= 0:
        raise PositionStateError("lots must be positive")
    cfg = load()
    stop = average_price * (_HUNDRED - cfg.stop_loss_pct) / _HUNDRED
    target = average_price * (_HUNDRED + cfg.take_profit_pct) / _HUNDRED
    open_key = open_order_key
    async with transaction() as conn:
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
                    open_key,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            _reraise_unless_duplicate_position(exc, instrument.ticker)
        position_id = cursor.lastrowid
        if position_id is None:
            raise PositionStateError("insert did not assign an id")
        await _insert_event(
            conn,
            int(position_id),
            "ADOPTED",
            {
                "ticker": instrument.ticker,
                "lots": lots,
                "average_price": str(average_price),
                "open_order_key": open_key,
                "stop_price": str(stop),
                "target_price": str(target),
            },
        )
        loaded = await _load(conn, int(position_id))
        if loaded is None:
            raise PositionStateError("inserted position could not be read back")
        return loaded


async def update_lots(position_id: int, lots: int) -> Position:
    """Write the broker's lot count onto an open position."""
    if lots <= 0:
        raise PositionStateError("lots must be positive")
    async with transaction() as conn:
        existing = await _load(conn, position_id)
        if existing is None:
            raise PositionStateError(f"position {position_id} is absent")
        if existing.status != "OPEN":
            raise PositionStateError(f"position {position_id} is already closed")
        cursor = await conn.execute(
            "UPDATE positions SET lots = ? WHERE id = ? AND status = 'OPEN'",
            (lots, position_id),
        )
        if cursor.rowcount != 1:
            raise PositionStateError(
                f"position {position_id} is already closed or absent"
            )
        await _insert_event(
            conn,
            position_id,
            "LOTS_ADJUSTED",
            {
                "previous_lots": existing.lots,
                "new_lots": lots,
            },
        )
        loaded = await _load(conn, position_id)
        if loaded is None:
            raise PositionStateError(f"position {position_id} is absent")
        return loaded


async def list_events(position_id: int) -> list[PositionEvent]:
    """Events for a position, oldest first. Empty list when none; never None."""
    conn = _conn()
    cursor = await conn.execute(
        """
        SELECT position_id, occurred_at, event, detail
        FROM position_events
        WHERE position_id = ?
        ORDER BY id ASC
        """,
        (position_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_event(row) for row in rows]
