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
    InstrumentNotFound,
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


async def _adopt_holding(
    holding: Position, moment: datetime, open_order_key: str
) -> dict[str, object]:
    instrument = await get_instrument(holding.ticker)
    await adopt(
        instrument, holding.lots, holding.entry_price, moment, open_order_key
    )
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


async def _recognising_orders() -> dict[str, str]:
    """Ticker → the key of the bot's unfinished entry of its own for it.

    The one case `db.positions.adopt` survives for: the bot submitted the buy,
    the broker filled it, and the crash landed before the position row was
    written. The order row is the bot's own record of the holding, so the
    holding is recognised and the *local row* is what is missing. Anything else
    at the broker is foreign.

    The key is carried out of here rather than just the ticker, because the
    adopted position must point at that order — the recognition rule has already
    identified exactly one, so the answer is in hand at the moment the decision
    is made (#42). `list_unresolved` is oldest-first, so keeping the first match
    per ticker is the documented tie-break for the state the per-ticker
    submission lock is supposed to make impossible.
    """
    found: dict[str, str] = {}
    for order in await list_unresolved():
        if order.intent == "ENTRY":
            found.setdefault(order.ticker, order.key)
    return found


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


def _is_mispriced(
    broker_price: Decimal, local_price: Decimal, increment: Decimal
) -> bool:
    """Whether a live stop sits at a different price, not merely a snapped one.

    The broker rounds every posted stop to the instrument's
    `min_price_increment`, so the price it holds is almost never the price the
    bot computed: on 2026-09-07 it held GMKN at 125.44 against a stored 125.457.
    An exact inequality called that mispriced on every startup, and the caller's
    remedy — cancel then re-post — unprotected a live position each time for a
    stop the broker had placed exactly as asked.

    Nothing is rounded here or anywhere else: which way the broker rounds has
    not been checked against a live account, and writing a guessed rounded
    price into the record would be a number this project invented (rule 33). A
    tolerance costs nothing because the bot never moves a stop after entry — a
    genuinely wrong stop is wrong by the distance between two prices, not by
    less than one tick.

    Only called with an increment the broker actually reported: a stop whose
    increment is unknown is not judged at all, here or in the branch beneath
    this one (rule 37).
    """
    return abs(broker_price - local_price) >= increment


async def _price_increments(tickers: set[str]) -> dict[str, Decimal]:
    """Tick sizes for the tickers whose stops are about to be judged.

    A ticker whose metadata cannot be read is absent from the result, and its
    stop's price is then not judged at all — neither mispriced nor adoptable
    (rule 37). Both remedies act on the price: one cancels and re-posts, the
    other makes the exchange the sole protection at that price. Neither is spent
    on a number this module could not check.

    An increment of zero or less is treated as unread rather than as a licence
    to compare exactly, and takes the same alert: one entrance to the blind
    path, not one alerted and one silent (v1.48).
    """
    increments: dict[str, Decimal] = {}
    for ticker in sorted(tickers):
        try:
            instrument = await get_instrument(ticker)
        except (InstrumentNotFound, BrokerUnavailable, BrokerRateLimited) as exc:
            _LOG.warning("price increment unavailable for %s: %s", ticker, exc)
            await alert(
                f"price increment unavailable for {ticker} ({exc}); "
                f"its stop price was not compared"
            )
            continue
        if instrument.min_price_increment <= 0:
            _LOG.warning(
                "price increment for %s is %s", ticker, instrument.min_price_increment
            )
            await alert(
                f"price increment for {ticker} is "
                f"{instrument.min_price_increment}; its stop price was not compared"
            )
            continue
        increments[ticker] = instrument.min_price_increment
    return increments


def _stop_adjustments(
    opened: list[Position],
    stops: list[StopOrderRecord],
    increments: dict[str, Decimal],
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
            increment = increments.get(position.ticker)
            if increment is None:
                # Neither price-based finding is reported: the price comparison
                # is the guard on adoption as much as on replacement, and it is
                # the price that could not be read (rule 37). The position stays
                # LOCAL and lifecycle.exits keeps watching its own stop.
                pass
            elif _is_mispriced(kept.stop_price, position.stop_price, increment):
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
    recognised = await _recognising_orders() if unknown else {}
    for holding in unknown:
        open_order_key = recognised.get(holding.ticker)
        if open_order_key is not None:
            adjustments.append(await _adopt_holding(holding, now, open_order_key))
        else:
            adjustments.append(await _report_foreign(holding))

    remaining_open = await list_open()
    broker_stops = await list_stop_orders()
    stopped_tickers = {stop.ticker for stop in broker_stops}
    increments = await _price_increments(
        {
            position.ticker
            for position in remaining_open
            if position.ticker in stopped_tickers
        }
    )
    adjustments.extend(_stop_adjustments(remaining_open, broker_stops, increments))

    await _persist(now, adjustments)
    return ReconciliationReport(ran_at=now, adjustments=tuple(adjustments))
