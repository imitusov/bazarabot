"""Late commission backfill. Never places, cancels, or modifies an order."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    OrderNotFound,
    get_operations,
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
from zarabot.models import OperationRecord, OrderRecord
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)

_STALE = timedelta(hours=24)

# How long after a trade a fee must be absent before that absence is a
# measurement. The trade-to-fee lag on the live account is min 1s, median 1s,
# max 1s over the full history (§2.1, #246); five minutes is 300x the worst
# observed. It guards exactly one hazard: a trade that filled seconds before
# `until`, whose fee has not posted yet, must not be read as commission-free and
# written off as zero. It is not a settlement window, and it is not a reason to
# widen `app.loops._BACKFILL_LOOKBACK`, which at 7 days is already five orders
# of magnitude wider than the lag.
_FEE_SETTLE = timedelta(minutes=5)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _resolve(
    order: OrderRecord,
    operations: Sequence[OperationRecord],
    cutoff: datetime,
) -> Decimal | None:
    """One order's commission: a number, a measured zero, or `None` for unknown.

    Re-queries by whichever identifier can find the order. A row describing an
    execution the exchange performed is filed under a key the bot invented, so
    `get_order_state` on it can only ever return `OrderNotFound` — which is why
    the commission on every stop exit was permanently unrecoverable (#8). Either
    way the lookup is by an identifier, never by matching on instrument, time
    and quantity, which is ambiguous exactly when two similar orders are close
    together.

    **The order state is not the last word (spec v1.91, #246).** It reports a
    zero commission on every fill, which `broker.client` now records as `None` —
    that is what makes the row visible to `list_missing_commission` at all — and
    re-querying it returns the same zero. When it carries a number that number
    wins and nothing else is read; when it does not, the operations feed is the
    arbiter, joined on the executions the broker attributes to this order.

    Three outcomes, and the second is what stops the alert repeating forever:

    * a fee child of one of this order's trades -> the sum of those fees;
    * the trades found, no fee child, and the latest of them settled before
      `cutoff` -> `Decimal(0)`, a measured zero. The row leaves
      `list_missing_commission` and is never asked about again;
    * anything else -> `None`, unknown, asked about again tomorrow.
    """
    try:
        if order.broker_order_id:
            state = await get_order_state_by_broker_id(order.broker_order_id)
        else:
            state = await get_order_state(order.key)
    except (OrderNotFound, BrokerUnavailable, BrokerRateLimited):
        return None
    if state.commission is not None:
        return state.commission
    executions = set(state.trade_ids)
    if not executions:
        return None
    trades = [item for item in operations if executions & set(item.trade_ids)]
    if not trades:
        return None
    parents = {item.id for item in trades}
    fees = [
        item.commission
        for item in operations
        if item.parent_operation_id in parents
    ]
    if fees:
        return sum(fees, Decimal(0))
    if max(item.occurred_at for item in trades) <= cutoff:
        # No fee for a trade that settled long enough ago that one would have
        # posted. That is a commission-free trade, and it is terminal.
        return Decimal(0)
    return None


async def _feed(since: datetime, until: datetime) -> list[OperationRecord]:
    """The operations for the window, or an empty feed if it cannot be read.

    Catches `BrokerUnavailable` and `BrokerRateLimited` and nothing wider — the
    same two classes `broker.reconcile` catches on the same feed. Every row is
    then unknown and is retried on the next run, which is what this job is for.
    Any other exception propagates under rule 21 rather than becoming a quiet
    day of no commissions (failure class 5).
    """
    try:
        return await get_operations(since, until)
    except (BrokerUnavailable, BrokerRateLimited) as exc:
        # Deliberately NOT a §7.1 structured event: this is a transient
        # condition with no catalogue row, and an `event` name absent from
        # `scripts/deploy/export_health.py`'s KNOWN_EVENTS makes the health
        # export exit non-zero. `broker.client._cached_instrument` logs the
        # same way for the same reason.
        _LOG.warning("operations feed unavailable, commissions stay unknown: %s", exc)
        return []


async def backfill(since: datetime, until: datetime) -> int:
    """Re-query unknown commissions and rewrite affected closed P&L."""
    _reject_naive(since)
    _reject_naive(until)
    missing = await list_missing_commission(since, until)
    if not missing:
        return 0
    # One feed read per run, not one per order. The window is the caller's own
    # and needs no margin: the trade is inside it by construction, because that
    # is the window the order was selected in, and the fee posts a measured one
    # second later (§2.1, #246).
    operations = await _feed(since, until)
    updated: list[str] = []
    moment = now()
    cutoff = moment - _FEE_SETTLE
    for order in missing:
        commission = await _resolve(order, operations, cutoff)
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
