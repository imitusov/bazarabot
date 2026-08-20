"""Compare broker holdings to local positions. Observes and records; never trades."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import aiosqlite

from zarabot.broker.client import (
    BrokerUnavailable,
    get_instrument,
    get_last_price,
    get_portfolio,
    list_stop_orders,
)
from zarabot.config import load
from zarabot.db.cooldowns import start as start_cooldown
from zarabot.db.orders import record_submitting
from zarabot.db.orders import settle as settle_order
from zarabot.db.positions import adopt, close, list_open, update_lots
from zarabot.models import (
    ExitTrigger,
    OrderStatus,
    Position,
    ReconciliationReport,
    Side,
    StopOrderRecord,
    StopProtection,
)

_LOG = logging.getLogger(__name__)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _alert(message: str) -> None:
    _LOG.error(message)


def _by_ticker(positions: list[Position] | tuple[Position, ...]) -> dict[str, Position]:
    found: dict[str, Position] = {}
    for position in positions:
        if position.ticker:
            found[position.ticker] = position
    return found


async def _last_price(position: Position) -> Decimal:
    try:
        return await get_last_price(position.figi)
    except BrokerUnavailable:
        return position.entry_price


async def _close_externally(position: Position, moment: datetime) -> dict[str, object]:
    price = await _last_price(position)
    key = str(uuid4())
    await record_submitting(key, position.ticker, Side.SELL, position.lots, "EXIT")
    order = await settle_order(
        key, OrderStatus.FILLED, position.lots, price, "closed externally"
    )
    await close(position.id, ExitTrigger.EXTERNAL, price, moment, order)
    await start_cooldown(position.ticker, moment)
    _alert(
        f"position {position.ticker} closed externally at {price} (id={position.id})"
    )
    return {
        "type": "CLOSED_EXTERNALLY",
        "ticker": position.ticker,
        "position_id": position.id,
        "last_price": str(price),
    }


async def _adopt_holding(holding: Position, moment: datetime) -> dict[str, object]:
    instrument = await get_instrument(holding.ticker)
    await adopt(instrument, holding.lots, holding.entry_price, moment)
    _alert(
        f"adopted {holding.ticker} lots={holding.lots} "
        f"average_price={holding.entry_price}"
    )
    return {
        "type": "ADOPTED",
        "ticker": holding.ticker,
        "lots": holding.lots,
        "average_price": str(holding.entry_price),
    }


async def _adjust_lots(local: Position, broker_lots: int) -> dict[str, object]:
    previous = local.lots
    await update_lots(local.id, broker_lots)
    _alert(
        f"lots adjusted for {local.ticker} id={local.id} "
        f"from {previous} to {broker_lots}"
    )
    return {
        "type": "LOTS_ADJUSTED",
        "ticker": local.ticker,
        "position_id": local.id,
        "from": previous,
        "to": broker_lots,
    }


def _stop_adjustments(
    opened: list[Position], stops: list[StopOrderRecord]
) -> list[dict[str, object]]:
    adjustments: list[dict[str, object]] = []
    claimed: set[str] = set()
    for position in opened:
        ticker_stops = [stop for stop in stops if stop.ticker == position.ticker]
        if ticker_stops:
            stop = ticker_stops[0]
            claimed.add(stop.stop_order_id or stop.key)
            if stop.stop_price != position.stop_price:
                adjustments.append(
                    {
                        "type": "STOP_MISPRICED",
                        "ticker": position.ticker,
                        "position_id": position.id,
                        "expected": str(position.stop_price),
                        "actual": str(stop.stop_price),
                    }
                )
            elif (
                position.stop_protection is StopProtection.LOCAL
                or position.stop_order_key is None
            ):
                adjustments.append(
                    {
                        "type": "STOP_ADOPTABLE",
                        "ticker": position.ticker,
                        "position_id": position.id,
                        "stop_order_id": stop.stop_order_id,
                    }
                )
        elif position.stop_protection is StopProtection.EXCHANGE:
            adjustments.append(
                {
                    "type": "STOP_MISSING",
                    "ticker": position.ticker,
                    "position_id": position.id,
                }
            )
    open_tickers = {position.ticker for position in opened}
    for stop in stops:
        marker = stop.stop_order_id or stop.key
        if marker in claimed:
            continue
        if stop.ticker in open_tickers:
            continue
        adjustments.append(
            {
                "type": "STOP_ORPHAN",
                "ticker": stop.ticker,
                "stop_order_id": stop.stop_order_id,
                "key": stop.key,
            }
        )
    return adjustments


async def _persist(moment: datetime, adjustments: list[dict[str, object]]) -> None:
    conn = await aiosqlite.connect(load().db_path, timeout=30)
    try:
        await conn.execute(
            "INSERT INTO reconciliations (ran_at, adjustments) VALUES (?, ?)",
            (moment.isoformat(), json.dumps(adjustments)),
        )
        await conn.commit()
    finally:
        await conn.close()


async def reconcile(now: datetime) -> ReconciliationReport:
    """Correct local records to match the broker. Never places or cancels orders."""
    _reject_naive(now)
    portfolio = await get_portfolio()
    local_open = await list_open()
    broker_by_ticker = _by_ticker(portfolio.positions)
    local_by_ticker = _by_ticker(local_open)
    adjustments: list[dict[str, object]] = []

    for ticker, local in local_by_ticker.items():
        holding = broker_by_ticker.get(ticker)
        if holding is None:
            adjustments.append(await _close_externally(local, now))
        elif holding.lots != local.lots:
            adjustments.append(await _adjust_lots(local, holding.lots))

    for ticker, holding in broker_by_ticker.items():
        if ticker not in local_by_ticker:
            adjustments.append(await _adopt_holding(holding, now))

    remaining_open = await list_open()
    broker_stops = await list_stop_orders()
    adjustments.extend(_stop_adjustments(remaining_open, broker_stops))

    await _persist(now, adjustments)
    return ReconciliationReport(ran_at=now, adjustments=tuple(adjustments))
