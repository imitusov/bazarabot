"""Late commission backfill. Never places, cancels, or modifies an order."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    OrderNotFound,
    get_order_state,
)
from zarabot.clock import now
from zarabot.db.orders import list_missing_commission, record_commission
from zarabot.db.positions import list_closed, recompute_realised
from zarabot.telegram.notifier import alert

_STALE = timedelta(hours=24)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def backfill(since: datetime, until: datetime) -> int:
    """Re-query unknown commissions and rewrite affected closed P&L."""
    _reject_naive(since)
    _reject_naive(until)
    missing = await list_missing_commission(since, until)
    updated: list[str] = []
    moment = now()
    for order in missing:
        commission: Decimal | None = None
        try:
            state = await get_order_state(order.key)
        except (OrderNotFound, BrokerUnavailable, BrokerRateLimited):
            state = None
        if state is not None:
            commission = state.commission
        if commission is not None:
            await record_commission(order.key, commission)
            updated.append(order.key)
            continue
        fill_at = order.settled_at if order.settled_at is not None else order.created_at
        if moment - fill_at > _STALE:
            await alert(
                f"commission still unknown for order {order.key} "
                "more than 24h after fill"
            )
    if updated:
        keys = set(updated)
        for position in await list_closed():
            if position.open_order_key in keys or position.close_order_key in keys:
                await recompute_realised(position.id)
    return len(updated)
