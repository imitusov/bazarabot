"""Late commission backfill. Never places, cancels, or modifies an order."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    OrderNotFound,
    get_order_state,
    get_order_state_by_broker_id,
)
from zarabot.clock import now
from zarabot.db.orders import (
    list_missing_commission,
    mark_commission_alerted,
    record_commission,
)
from zarabot.db.positions import list_closed, recompute_realised
from zarabot.models import OrderRecord
from zarabot.telegram.notifier import alert

_STALE = timedelta(hours=24)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _resolve(order: OrderRecord) -> Decimal | None:
    """Re-query one order's commission, by whichever identifier can find it.

    A row describing an execution the exchange performed is filed under a key
    the bot invented, so `get_order_state` on it can only ever return
    `OrderNotFound` — which is why the commission on every stop exit was
    permanently unrecoverable (#8). Either way the lookup is by an identifier,
    never by matching on instrument, time and quantity, which is ambiguous
    exactly when two similar orders are close together.
    """
    try:
        if order.broker_order_id:
            state = await get_order_state_by_broker_id(order.broker_order_id)
        else:
            state = await get_order_state(order.key)
    except (OrderNotFound, BrokerUnavailable, BrokerRateLimited):
        return None
    return state.commission


async def backfill(since: datetime, until: datetime) -> int:
    """Re-query unknown commissions and rewrite affected closed P&L."""
    _reject_naive(since)
    _reject_naive(until)
    missing = await list_missing_commission(since, until)
    updated: list[str] = []
    moment = now()
    for order in missing:
        commission = await _resolve(order)
        if commission is not None:
            await record_commission(order.key, commission)
            updated.append(order.key)
            continue
        fill_at = order.settled_at if order.settled_at is not None else order.created_at
        # Alert once per order, not once per run. This runs daily from the
        # rollover loop and again before every weekly report, so the same row
        # alerted forever — into a channel whose whole premise is that silence
        # means healthy (#8). The row is still re-queried above: the terminal
        # state is on the telling, not the trying.
        if moment - fill_at > _STALE and order.commission_alerted_at is None:
            await alert(
                f"commission still unknown for order {order.key} "
                "more than 24h after fill"
            )
            await mark_commission_alerted(order.key, moment)
    if updated:
        keys = set(updated)
        for position in await list_closed():
            if position.open_order_key in keys or position.close_order_key in keys:
                await recompute_realised(position.id)
    return len(updated)
