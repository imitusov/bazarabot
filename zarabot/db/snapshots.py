"""Daily equity snapshots. Sole owner of `daily_snapshots`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import aiosqlite

from zarabot.db.connection import shared, transaction

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailySnapshot:
    trade_date: date
    opening_equity: Decimal
    closing_equity: Decimal | None
    cash: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    open_positions: int
    orders_placed: int
    benchmark_value: Decimal | None


def _conn() -> aiosqlite.Connection:
    conn = shared()
    conn.row_factory = aiosqlite.Row
    return conn


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _dec_opt(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _row_to_snapshot(row: aiosqlite.Row) -> DailySnapshot:
    return DailySnapshot(
        trade_date=date.fromisoformat(row["trade_date"]),
        opening_equity=_dec(row["opening_equity"]),
        closing_equity=_dec_opt(row["closing_equity"]),
        cash=_dec(row["cash"]),
        realised_pnl=_dec(row["realised_pnl"]),
        unrealised_pnl=_dec(row["unrealised_pnl"]),
        open_positions=int(row["open_positions"]),
        orders_placed=int(row["orders_placed"]),
        benchmark_value=_dec_opt(row["benchmark_value"]),
    )


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


async def write_daily(snapshot: DailySnapshot) -> None:
    """Upsert on the Moscow trade date. Write failures are not propagated."""
    try:
        async with transaction(critical=False) as conn:
            await conn.execute(
                """
                INSERT INTO daily_snapshots (
                    trade_date, opening_equity, closing_equity, cash,
                    realised_pnl, unrealised_pnl, open_positions, orders_placed,
                    benchmark_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    opening_equity = excluded.opening_equity,
                    closing_equity = excluded.closing_equity,
                    cash = excluded.cash,
                    realised_pnl = excluded.realised_pnl,
                    unrealised_pnl = excluded.unrealised_pnl,
                    open_positions = excluded.open_positions,
                    orders_placed = excluded.orders_placed,
                    benchmark_value = excluded.benchmark_value
                """,
                (
                    snapshot.trade_date.isoformat(),
                    str(snapshot.opening_equity),
                    _money(snapshot.closing_equity),
                    str(snapshot.cash),
                    str(snapshot.realised_pnl),
                    str(snapshot.unrealised_pnl),
                    snapshot.open_positions,
                    snapshot.orders_placed,
                    _money(snapshot.benchmark_value),
                ),
            )
    except aiosqlite.Error:
        _LOG.exception("snapshot write failed for %s", snapshot.trade_date)


async def list_for_period(start: date, end: date) -> list[DailySnapshot]:
    """Snapshots with trade_date in [start, end], oldest first."""
    cursor = await _conn().execute(
        """
        SELECT * FROM daily_snapshots
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (start.isoformat(), end.isoformat()),
    )
    rows = await cursor.fetchall()
    return [_row_to_snapshot(row) for row in rows]
