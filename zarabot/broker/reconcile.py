"""Compare broker holdings to local positions. Observes and records; never trades."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    get_instrument,
    get_operations,
    get_portfolio,
    list_stop_orders,
)
from zarabot.db.connection import transaction
from zarabot.db.cooldowns import start as start_cooldown
from zarabot.db.orders import list_unresolved
from zarabot.db.positions import adopt, close, list_open, update_lots
from zarabot.models import (
    ExitTrigger,
    OperationRecord,
    Position,
    ReconciliationReport,
    StopOrderRecord,
    StopProtection,
)
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)

# Every way the broker names a sale. A sale is identified by what the broker
# called it, never by the sign of a payment (#11).
_SALE_TYPES = frozenset(
    {
        "OPERATION_TYPE_SELL",
        "OPERATION_TYPE_DELIVERY_SELL",
        "OPERATION_TYPE_SELL_MARGIN",
    }
)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _by_ticker(positions: list[Position] | tuple[Position, ...]) -> dict[str, Position]:
    found: dict[str, Position] = {}
    for position in positions:
        if position.ticker:
            found[position.ticker] = position
    return found


@dataclass(frozen=True)
class _ResolvedSale:
    """What the broker says happened, and nothing this module worked out."""

    price: Decimal
    occurred_at: datetime
    commission: Decimal


def _resolve_sale(
    position: Position, operations: list[OperationRecord]
) -> _ResolvedSale | None:
    """The real sale of this position, from the account's own operations feed.

    Every executed sale of the instrument inside the window belongs to this
    position, because at most one position per ticker is open at a time. The
    price is their quantity-weighted average — the one weighting in the system
    that is legitimate, because every input is a number the broker reported
    about a trade that occurred.

    Deliberately no "take sales until they cover the position" cutoff: whether
    the broker reports `quantity` in lots or in instrument units has never been
    checked against a live account, and that is the class of assumption that
    produced #39 and #43. A weighted average is right under either reading
    because the units cancel; a cutoff is not.
    """
    sales = [
        item
        for item in operations
        if item.figi == position.figi
        and item.operation_type in _SALE_TYPES
        and item.price is not None
        and item.quantity
    ]
    if not sales:
        return None
    units = sum(abs(item.quantity or 0) for item in sales)
    if units <= 0:
        return None
    gross = sum(
        ((item.price or Decimal("0")) * abs(item.quantity or 0) for item in sales),
        Decimal("0"),
    )
    sale_ids = {item.id for item in sales}
    commission = sum(
        (
            item.commission
            for item in operations
            if item.parent_operation_id in sale_ids
        ),
        Decimal("0"),
    )
    return _ResolvedSale(
        price=gross / units,
        occurred_at=max(item.occurred_at for item in sales),
        commission=commission,
    )


async def _close_externally(position: Position, moment: datetime) -> dict[str, object]:
    """Book the sale the broker recorded, or report that it could not be found.

    This used to book the exit at `get_last_price` as of the moment of
    detection — hours or days after the sale, and on a different day entirely
    if the bot was down — and fell back to the position's own `entry_price`
    when the broker was unreachable, recording an exit of exactly zero P&L.
    Both were numbers this module made up (#11, rule 33).
    """
    try:
        operations = await get_operations(position.entry_at, moment)
    except (BrokerUnavailable, BrokerRateLimited) as exc:
        return await _report_unresolved_exit(position, f"operations feed: {exc}")
    sale = _resolve_sale(position, operations)
    if sale is None:
        return await _report_unresolved_exit(
            position, "no executed sale for this instrument in the window"
        )
    await close(
        position.id,
        ExitTrigger.EXTERNAL,
        sale.price,
        sale.occurred_at,
        None,
        sale.commission,
    )
    await start_cooldown(position.ticker, sale.occurred_at)
    await alert(
        f"position {position.ticker} closed externally at {sale.price} "
        f"on {sale.occurred_at.isoformat()} (id={position.id})"
    )
    return {
        "type": "CLOSED_EXTERNALLY",
        "ticker": position.ticker,
        "position_id": position.id,
        "exit_price": str(sale.price),
        "exit_at": sale.occurred_at.isoformat(),
        "exit_commission": str(sale.commission),
    }


async def _report_unresolved_exit(position: Position, reason: str) -> dict[str, object]:
    """Leave the position open rather than close it at a substituted number.

    A position closed a cycle late is recoverable; one closed at a number this
    module invented is not, because nothing downstream can tell the invented
    one from a real one (rule 33). The shares having left the account by some
    route that was not a trade is the owner's to explain.

    Unlike rule 32's foreign holding this does not stop the bot: that refusal
    exists for shares the bot might trade, and this row describes shares the
    account no longer has.
    """
    await alert(
        f"position {position.ticker} is absent at the broker and its sale "
        f"could not be resolved ({reason}); left open (id={position.id})"
    )
    return {
        "type": "EXIT_UNRESOLVED",
        "ticker": position.ticker,
        "position_id": position.id,
        "reason": reason,
    }


async def _adopt_holding(holding: Position, moment: datetime) -> dict[str, object]:
    instrument = await get_instrument(holding.ticker)
    await adopt(instrument, holding.lots, holding.entry_price, moment)
    await alert(
        f"adopted {holding.ticker} lots={holding.lots} "
        f"average_price={holding.entry_price}"
    )
    return {
        "type": "ADOPTED",
        "ticker": holding.ticker,
        "lots": holding.lots,
        "average_price": str(holding.entry_price),
    }


async def _report_foreign(holding: Position) -> dict[str, object]:
    """Name a holding the bot does not recognise. Nothing is written.

    Adoption derived the stop and target from the holding's average cost, so a
    holding bought by hand and already above its cost basis was adopted past its
    take-profit and sold on the next cycle. The account is the bot's alone
    (brief v1.8): an unrecognised holding is a condition to report, and
    `app.startup` refuses to start on it (rule 32).
    """
    await alert(
        f"foreign holding {holding.ticker} lots={holding.lots} "
        f"average_price={holding.entry_price}: not adopted, not traded"
    )
    return {
        "type": "FOREIGN_HOLDING",
        "ticker": holding.ticker,
        "lots": holding.lots,
        "average_price": str(holding.entry_price),
    }


async def _recognised_tickers() -> set[str]:
    """Tickers the bot has an unfinished entry of its own for.

    The one case `db.positions.adopt` survives for: the bot submitted the buy,
    the broker filled it, and the crash landed before the position row was
    written. The order row is the bot's own record of the holding, so the
    holding is recognised and the *local row* is what is missing. Anything else
    at the broker is foreign.
    """
    return {
        order.ticker for order in await list_unresolved() if order.intent == "ENTRY"
    }


async def _adjust_lots(local: Position, broker_lots: int) -> dict[str, object]:
    previous = local.lots
    await update_lots(local.id, broker_lots)
    await alert(
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


def _identifier(stop: StopOrderRecord) -> str:
    """How a stop is named in an adjustment: the broker's id, else our key."""
    return stop.stop_order_id or stop.key


def _keep_stop(
    position: Position, ticker_stops: list[StopOrderRecord]
) -> StopOrderRecord:
    key = position.stop_order_key
    if key:
        for stop in ticker_stops:
            if stop.key == key or stop.stop_order_id == key:
                return stop
    return min(ticker_stops, key=lambda stop: stop.created_at)


def _stop_adjustments(
    opened: list[Position], stops: list[StopOrderRecord]
) -> list[dict[str, object]]:
    adjustments: list[dict[str, object]] = []
    claimed: set[str] = set()
    for position in opened:
        ticker_stops = [stop for stop in stops if stop.ticker == position.ticker]
        if ticker_stops:
            kept = _keep_stop(position, ticker_stops)
            if len(ticker_stops) > 1:
                keeper = _identifier(kept)
                adjustments.append(
                    {
                        "type": "STOP_DUPLICATE",
                        "ticker": position.ticker,
                        "position_id": position.id,
                        "keep": keeper,
                        "cancel": [
                            _identifier(stop)
                            for stop in ticker_stops
                            if stop is not kept
                        ],
                    }
                )
            for stop in ticker_stops:
                claimed.add(_identifier(stop))
            if kept.stop_price != position.stop_price:
                adjustments.append(
                    {
                        "type": "STOP_MISPRICED",
                        "ticker": position.ticker,
                        "position_id": position.id,
                        "expected": str(position.stop_price),
                        "actual": str(kept.stop_price),
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
                        "stop_order_id": kept.stop_order_id,
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
        marker = _identifier(stop)
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
    async with transaction() as conn:
        await conn.execute(
            "INSERT INTO reconciliations (ran_at, adjustments) VALUES (?, ?)",
            (moment.isoformat(), json.dumps(adjustments)),
        )


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

    unknown = [
        holding
        for ticker, holding in broker_by_ticker.items()
        if ticker not in local_by_ticker
    ]
    recognised = await _recognised_tickers() if unknown else set()
    for holding in unknown:
        if holding.ticker in recognised:
            adjustments.append(await _adopt_holding(holding, now))
        else:
            adjustments.append(await _report_foreign(holding))

    remaining_open = await list_open()
    broker_stops = await list_stop_orders()
    adjustments.extend(_stop_adjustments(remaining_open, broker_stops))

    await _persist(now, adjustments)
    return ReconciliationReport(ran_at=now, adjustments=tuple(adjustments))
