"""Order submission, submission locks, and crash recovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import aiosqlite
from telegram.error import TelegramError

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    OrderNotFound,
    OrderRejected,
    StopOrderRejected,
    cancel_order,
    cancel_stop_order,
    get_instrument,
    get_max_lots,
    get_order_state,
    post_market_order,
    post_stop_loss,
)
from zarabot.clock import moscow_date
from zarabot.clock import now as clock_now
from zarabot.config import load
from zarabot.db.cooldowns import start as start_cooldown
from zarabot.db.orders import OrderStateError, record_submitting
from zarabot.db.orders import list_unresolved as list_unresolved_orders
from zarabot.db.orders import settle as settle_order
from zarabot.db.positions import (
    PositionStateError,
    list_open,
    set_stop_protection,
    update_lots,
)
from zarabot.db.positions import close as close_row
from zarabot.db.positions import get as get_position
from zarabot.db.positions import open as open_row
from zarabot.db.signals import list_for_period
from zarabot.db.stop_orders import activate, active_for_position, record_placing
from zarabot.db.stop_orders import settle as settle_stop
from zarabot.models import (
    ExitTrigger,
    HaltReason,
    Instrument,
    OrderRecord,
    OrderStatus,
    Position,
    Side,
    Signal,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
)
from zarabot.state.halt import halt
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_HUNDRED = Decimal("100")
_STOP_ATTEMPTS = 3


def _emit(level: int, event: str, **fields: object) -> None:
    _LOG.log(level, event, extra={"event": event, **fields})


def _emit_filled(order: OrderRecord) -> None:
    _emit(
        logging.INFO,
        "order_filled",
        key=order.key,
        ticker=order.ticker,
        filled_lots=order.filled_lots,
        filled_price=order.filled_price,
        commission=order.commission,
    )


# A trade the bot did not decide on is named, not credited. In the same family
# as `ADOPTED`: the schema accepts it, `telegram.commands` iterates the enabled
# strategies and so never shows it under one, and `reporter.weekly` groups by
# the stored name and so gives it a heading of its own (rule 35).
_UNATTRIBUTED = "UNATTRIBUTED"

_global_lock: asyncio.Lock | None = None
_ticker_locks: dict[str, asyncio.Lock] = {}
_registry_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _loop_locks() -> tuple[asyncio.Lock, dict[str, asyncio.Lock], asyncio.Lock]:
    """Locks for the running loop. Module-level Lock objects bind the first
    loop that contends them; a later asyncio.run then raises (#50)."""
    global _global_lock, _ticker_locks, _registry_lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop or _global_lock is None or _registry_lock is None:
        _global_lock = asyncio.Lock()
        _registry_lock = asyncio.Lock()
        _ticker_locks = {}
        _lock_loop = loop
    return _global_lock, _ticker_locks, _registry_lock


class ExitFailed(Exception):
    """An exit could not be completed. The caller retries on the next cycle."""


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _halt_on_db_failure(detail: str) -> None:
    await alert(f"trading halted: {detail}")
    try:
        await halt(HaltReason.MANUAL, detail, clock_now())
    except aiosqlite.Error:
        # Rule 11 (v1.75): "A write failure is `aiosqlite.Error`, and only
        # that ... Every other exception propagates unchanged." The halt is
        # itself a trading-critical write, so its own failure is the same
        # class; a defect here must not leave the bot believing it halted.
        _LOG.exception("halt after database failure also failed")


@asynccontextmanager
async def _locks(ticker: str) -> AsyncIterator[None]:
    global_lock, ticker_locks, registry = _loop_locks()
    async with registry:
        lock = ticker_locks.get(ticker)
        if lock is None:
            lock = asyncio.Lock()
            ticker_locks[ticker] = lock
    async with lock, global_lock:
        yield


def _stop_and_target(fill: Decimal) -> tuple[Decimal, Decimal]:
    cfg = load()
    stop = fill * (_HUNDRED - cfg.stop_loss_pct) / _HUNDRED
    target = fill * (_HUNDRED + cfg.take_profit_pct) / _HUNDRED
    return stop, target


async def _already_open(ticker: str) -> Position | None:
    for position in await list_open():
        if position.ticker == ticker:
            return position
    return None


async def _write[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except aiosqlite.Error:
        await _halt_on_db_failure("database write failed on a trading-critical path")
        raise


async def _signal_for_recovered_entry(order: OrderRecord, moment: datetime) -> Signal:
    """Recover the signal behind a discovered fill, or say plainly there is none.

    The lookup spans the order's life rather than one Moscow date: an order
    that filled at 23:58 MSK and is recovered at 00:05 is the case where
    recovery matters most, and a single-date search failed exactly there.

    With no match, the strategy is `UNATTRIBUTED`, never a real one. This used
    to name `ma_crossover` — a strategy whose weekly figures decide whether it
    stays enabled — so every recovered trade biased that evidence, in the same
    direction every time (#11, rule 35).
    """
    created = moscow_date(order.created_at)
    seen = moscow_date(moment)
    pairs = await list_for_period(min(created, seen), max(created, seen))
    for signal, _decision in reversed(pairs):
        if signal.ticker == order.ticker:
            return signal
    if order.filled_price is None:
        raise PositionStateError(f"recovered fill for {order.ticker} has no price")
    return Signal(
        ticker=order.ticker,
        strategy=_UNATTRIBUTED,
        side=Side.BUY,
        generated_at=moment,
        reference_price=order.filled_price,
    )


async def _resolve_partial_entry(key: str) -> OrderRecord | None:
    """Abandon a partial entry's remainder and settle from the broker's re-read.

    A `SUBMITTED` entry with lots filled is a live order holding shares we have
    no position row and no stop against. The remainder is abandoned — the
    strategy's entry price is stale by then — but abandoning it means cancelling
    it. What gets written down comes from the `get_order_state` that follows,
    never from the pre-cancel response, which was already stale when it arrived
    (rule 33/34).

    Returns `None` when the broker's own record is not in hand, having written
    nothing: the order stays unresolved and the next cycle repeats this.
    """
    try:
        await cancel_order(key)
        state = await get_order_state(key)
    except (OrderNotFound, BrokerUnavailable, BrokerRateLimited):
        return None
    filled = state.filled_lots if state.filled_lots is not None else 0
    if filled <= 0 or state.filled_price is None:
        return await _write(
            settle_order(
                key, OrderStatus.CANCELLED, 0, None, state.commission, "cancelled"
            )
        )
    return await _write(
        settle_order(
            key,
            OrderStatus.FILLED,
            filled,
            state.filled_price,
            state.commission,
            state.broker_reason,
        )
    )


async def _place_stop(position: Position, instrument: Instrument) -> Position:
    standing = await active_for_position(position.id)
    if standing is not None and position.stop_protection is StopProtection.EXCHANGE:
        return position
    last_error: Exception | None = None
    for _attempt in range(_STOP_ATTEMPTS):
        key = str(uuid4())
        await _write(
            record_placing(
                key, position.id, position.ticker, position.lots, position.stop_price
            )
        )
        try:
            placed = await post_stop_loss(
                key, instrument.figi, position.lots, position.stop_price
            )
            stop_id = placed.stop_order_id or ""
            if not stop_id:
                raise StopOrderRejected("rejected")
            await _write(activate(key, stop_id))
            updated = await _write(
                set_stop_protection(position.id, StopProtection.EXCHANGE, key)
            )
            _emit(
                logging.INFO,
                "stop_order_placed",
                position_id=updated.id,
                ticker=updated.ticker,
                stop_price=updated.stop_price,
                stop_order_id=stop_id,
            )
            return updated
        except (StopOrderRejected, BrokerUnavailable, BrokerRateLimited) as exc:
            last_error = exc
            try:
                await settle_stop(key, StopOrderStatus.FAILED, clock_now())
            except (OrderStateError, aiosqlite.Error):
                _LOG.exception("failed to settle unplaceable stop %s", key)
    await alert(
        f"stop-loss unplaceable for {position.ticker} after {_STOP_ATTEMPTS} attempts"
        f"{': ' + str(last_error) if last_error else ''}"
    )
    _emit(
        logging.ERROR,
        "stop_protection_degraded",
        position_id=position.id,
        ticker=position.ticker,
        attempts=_STOP_ATTEMPTS,
    )
    current = await get_position(position.id)
    return current if current is not None else position


async def place_protective_stop(position: Position, instrument: Instrument) -> Position:
    """Place a standing stop on an unprotected position. Does not unwind."""
    async with _locks(position.ticker):
        loaded = await get_position(position.id)
        if loaded is None:
            return position
        return await _place_stop(loaded, instrument)


async def adopt_existing_stop(position: Position, stop: StopOrderRecord) -> Position:
    """Bind a live broker stop to the position without placing a second one."""
    async with _locks(position.ticker):
        await _write(
            record_placing(
                stop.key, position.id, position.ticker, stop.lots, stop.stop_price
            )
        )
        stop_id = stop.stop_order_id or ""
        if not stop_id:
            raise StopOrderRejected("cannot adopt a stop without a broker identifier")
        await _write(activate(stop.key, stop_id))
        updated = await _write(
            set_stop_protection(position.id, StopProtection.EXCHANGE, stop.key)
        )
        _emit(
            logging.INFO,
            "stop_order_placed",
            position_id=updated.id,
            ticker=updated.ticker,
            stop_price=stop.stop_price,
            stop_order_id=stop_id,
        )
        return updated


async def cancel_orphaned_stop(stop: StopOrderRecord) -> None:
    """Cancel a live stop that has no matching open position."""
    async with _locks(stop.ticker):
        if stop.stop_order_id:
            await cancel_stop_order(stop.stop_order_id)
            _emit(
                logging.INFO,
                "stop_order_cancelled",
                position_id=stop.position_id,
                stop_order_id=stop.stop_order_id,
                cause="orphan",
            )
        with suppress(OrderStateError):
            await settle_stop(stop.key, StopOrderStatus.ORPHANED, clock_now())
        owner = await get_position(stop.position_id)
        if (
            owner is not None
            and owner.status == "OPEN"
            and owner.stop_order_key == stop.key
        ):
            await _write(set_stop_protection(owner.id, StopProtection.LOCAL, None))


async def replace_stop(position: Position, instrument: Instrument) -> Position:
    """Cancel the standing stop and place a replacement at the stored price."""
    async with _locks(position.ticker):
        await _cancel_stop_to_local(position, "replace")
        loaded = await get_position(position.id)
        if loaded is None:
            return position
        return await _place_stop(loaded, instrument)


async def _cancel_stop_to_local(position: Position, cause: str) -> Position:
    standing = await active_for_position(position.id)
    if standing is not None:
        if standing.stop_order_id:
            await cancel_stop_order(standing.stop_order_id)
            _emit(
                logging.INFO,
                "stop_order_cancelled",
                position_id=position.id,
                stop_order_id=standing.stop_order_id,
                cause=cause,
            )
        with suppress(OrderStateError):
            await settle_stop(standing.key, StopOrderStatus.CANCELLED, clock_now())
    loaded = await get_position(position.id)
    target = loaded if loaded is not None else position
    if target.stop_protection is StopProtection.EXCHANGE:
        return await _write(set_stop_protection(target.id, StopProtection.LOCAL, None))
    return target


async def _finish_close(
    position: Position,
    order: OrderRecord,
    trigger: ExitTrigger,
    moment: datetime,
) -> Position:
    if order.filled_price is None:
        # Rule 33: nothing downstream can tell an invented zero from a real
        # price. Leave the position open and let the caller retry.
        raise ExitFailed(f"exit for {position.ticker} has no fill price")
    closed = await _write(
        close_row(position.id, trigger, order.filled_price, moment, order)
    )
    await start_cooldown(position.ticker, moment)
    fields: dict[str, object] = {
        "position_id": closed.id,
        "ticker": closed.ticker,
        "exit_trigger": trigger.value,
        "exit_price": order.filled_price,
        "realised_pnl": closed.realised_pnl,
    }
    if trigger is ExitTrigger.STOP_LOSS:
        fields["gap_vs_stop"] = order.filled_price - position.stop_price
    _emit(logging.INFO, "position_closed", **fields)
    return closed


async def open_position(signal: Signal, lots: int, instrument: Instrument) -> Position:
    """Record intent, buy, open LOCAL, then place the standing stop."""
    async with _locks(signal.ticker):
        existing = await _already_open(signal.ticker)
        if existing is not None:
            raise PositionStateError(
                f"open position already exists for {signal.ticker}"
            )
        allowed = await get_max_lots(instrument.figi)
        requested = lots
        if allowed < requested:
            _LOG.info(
                "clamped lots for %s from %s to %s",
                signal.ticker,
                requested,
                allowed,
            )
            lots = allowed
        if lots <= 0:
            key = str(uuid4())
            await _write(
                record_submitting(key, signal.ticker, Side.BUY, requested, "ENTRY")
            )
            await _write(
                settle_order(key, OrderStatus.REJECTED, 0, None, None, "max lots is 0")
            )
            _emit(
                logging.ERROR,
                "order_rejected",
                key=key,
                ticker=signal.ticker,
                intent="ENTRY",
                broker_reason="max lots is 0",
            )
            raise OrderRejected("max lots is 0")
        key = str(uuid4())
        await _write(record_submitting(key, signal.ticker, Side.BUY, lots, "ENTRY"))
        _emit(
            logging.INFO,
            "order_submitting",
            key=key,
            ticker=signal.ticker,
            side=Side.BUY.value,
            intent="ENTRY",
            lots=lots,
        )
        try:
            posted = await post_market_order(key, instrument.figi, Side.BUY, lots)
        except OrderRejected as exc:
            await _write(
                settle_order(key, OrderStatus.REJECTED, 0, None, None, exc.reason)
            )
            _emit(
                logging.ERROR,
                "order_rejected",
                key=key,
                ticker=signal.ticker,
                intent="ENTRY",
                broker_reason=exc.reason,
            )
            await alert(f"entry rejected for {signal.ticker}: {exc.reason}")
            raise
        except (BrokerUnavailable, BrokerRateLimited):
            raise
        filled_lots = posted.filled_lots if posted.filled_lots is not None else 0
        fill_price = posted.filled_price
        if posted.status is OrderStatus.SUBMITTED and filled_lots > 0:
            settled = await _resolve_partial_entry(key)
            if settled is None:
                raise BrokerUnavailable("entry partial fill unresolved")
            got = settled.filled_lots if settled.filled_lots is not None else 0
            if got <= 0 or settled.filled_price is None:
                raise OrderRejected("entry cancelled with nothing filled")
            await alert(f"partial entry fill for {signal.ticker}: {got} of {lots}")
            if got < lots:
                _emit(
                    logging.WARNING,
                    "partial_fill",
                    key=key,
                    ticker=signal.ticker,
                    intent="ENTRY",
                    requested_lots=lots,
                    filled_lots=got,
                )
            _emit_filled(settled)
            return await _open_from_fill(signal, settled, instrument)
        # A SUBMITTED order that has filled nothing is left alone. Nothing is
        # held, so nothing is unprotected, and cancelling a market order that is
        # merely pending would turn every slow fill into a missed entry.
        if (
            posted.status is not OrderStatus.FILLED
            or filled_lots <= 0
            or fill_price is None
        ):
            raise BrokerUnavailable("entry outcome unknown")
        settled = await _write(
            settle_order(
                key,
                OrderStatus.FILLED,
                filled_lots,
                fill_price,
                posted.commission,
                posted.broker_reason,
            )
        )
        _emit_filled(settled)
        return await _open_from_fill(signal, settled, instrument)


async def _alert_fill_slippage(signal: Signal, fill_price: Decimal) -> None:
    """Tell the owner when an entry filled far from the price it was sized on.

    Alert only (brief §12). The position is opened, stopped and managed like
    any other; it is never sold back, because unwinding is a second real trade
    and the stop and target are already derived from the actual fill, so the
    percentage risk is correct. What is wrong is the position's rouble size,
    which is reportable, not tradeable.

    Cadence: at most one alert per entry fill, which is naturally bounded by
    the number of entries. There is no latch, and therefore no latch to reset.
    """
    reference = signal.reference_price
    threshold = load().fill_slippage_alert_pct
    if reference <= 0:
        # A percentage cannot be computed against a zero or negative
        # reference. Skipping the comparison would silence the alert exactly
        # where the sizing input was worst, so say so instead.
        text = (
            f"entry fill for {signal.ticker} cannot be compared: reference "
            f"price {reference} is not usable, fill {fill_price}. "
            "Slippage unknown; the position is kept."
        )
    else:
        difference = abs(fill_price - reference) / reference * _HUNDRED
        if difference <= threshold:
            return
        text = (
            f"entry fill slippage for {signal.ticker}: reference "
            f"{reference}, fill {fill_price}, "
            f"{difference.quantize(Decimal('0.01'))}% away "
            f"(threshold {threshold}%). The position is kept, not unwound."
        )
    try:
        await alert(text)
    except TelegramError:
        # Rule 13 (v1.75): the fill has already happened, and a Telegram send
        # failure — `telegram.error.TelegramError` and only that — is logged
        # loudly here and never propagates into the order path. Any other
        # exception is a defect and goes to rule 21.
        _LOG.exception("failed to alert entry slippage for %s", signal.ticker)


async def _open_from_fill(
    signal: Signal, order: OrderRecord, instrument: Instrument
) -> Position:
    if order.filled_price is None:
        raise OrderRejected("fill has no price")
    stop, target = _stop_and_target(order.filled_price)
    position = await _write(
        open_row(signal, order, instrument, stop, target, clock_now())
    )
    _emit(
        logging.INFO,
        "position_opened",
        position_id=position.id,
        ticker=position.ticker,
        strategy=signal.strategy,
        lots=position.lots,
        entry_price=position.entry_price,
        stop_price=position.stop_price,
        target_price=position.target_price,
    )
    protected = await _place_stop(position, instrument)
    # After the entry settles and the stop is placed: this is the only site
    # holding both the signal's reference price and the settled fill price.
    # It runs last so that nothing raised here can leave the position without
    # a stop order.
    await _alert_fill_slippage(signal, order.filled_price)
    return protected


async def close_position(position: Position, trigger: ExitTrigger) -> Position:
    """Bot-initiated exit. STOP_LOSS is legal only while the bot owns the stop."""
    async with _locks(position.ticker):
        current = await get_position(position.id)
        if current is None:
            raise PositionStateError(f"position {position.id} is absent")
        if (
            trigger is ExitTrigger.STOP_LOSS
            and current.stop_protection is StopProtection.EXCHANGE
        ):
            raise ValueError("STOP_LOSS on EXCHANGE is closed from the exchange fill")
        if current.stop_protection is StopProtection.EXCHANGE:
            current = await _cancel_stop_to_local(current, "exit")
        # Exactly one sell order, for the whole position. Until v1.34 this
        # looped until flat and then booked the close from the last slice
        # alone, dropping every earlier slice's price and commission from
        # realised P&L with nothing capping how many orders it could submit
        # (#10). A partial settles nothing now, so there is no second slice.
        key = str(uuid4())
        await _write(
            record_submitting(
                key, current.ticker, Side.SELL, current.lots, "EXIT", trigger
            )
        )
        _emit(
            logging.INFO,
            "order_submitting",
            key=key,
            ticker=current.ticker,
            side=Side.SELL.value,
            intent="EXIT",
            lots=current.lots,
        )
        try:
            posted = await post_market_order(key, current.figi, Side.SELL, current.lots)
        except OrderRejected as exc:
            await _write(
                settle_order(key, OrderStatus.REJECTED, 0, None, None, exc.reason)
            )
            _emit(
                logging.ERROR,
                "order_rejected",
                key=key,
                ticker=current.ticker,
                intent="EXIT",
                broker_reason=exc.reason,
            )
            _emit(
                logging.ERROR,
                "exit_failed",
                position_id=current.id,
                ticker=current.ticker,
                attempt=1,
                error="OrderRejected",
            )
            await alert(f"exit rejected for {current.ticker}: {exc.reason}")
            raise ExitFailed(exc.reason) from exc
        except (BrokerUnavailable, BrokerRateLimited) as exc:
            _emit(
                logging.ERROR,
                "exit_failed",
                position_id=current.id,
                ticker=current.ticker,
                attempt=1,
                error=type(exc).__name__,
            )
            await alert(f"exit unreachable for {current.ticker}: {exc}")
            raise ExitFailed(str(exc)) from exc
        filled = posted.filled_lots if posted.filled_lots is not None else 0
        price = posted.filled_price
        if posted.status is not OrderStatus.FILLED or filled <= 0 or price is None:
            # The remainder of an exit is never abandoned: the caller retries on
            # the next cycle under rule 4. Do not emit `partial_fill` from this
            # unsettled response — the figure is stale (rule 33); recovery
            # emits after settle.
            _emit(
                logging.ERROR,
                "exit_failed",
                position_id=current.id,
                ticker=current.ticker,
                attempt=1,
                error="unknown_outcome",
            )
            await alert(f"exit outcome unknown for {current.ticker}")
            raise ExitFailed("exit outcome unknown")
        order = await _write(
            settle_order(
                key,
                OrderStatus.FILLED,
                filled,
                price,
                posted.commission,
                posted.broker_reason,
            )
        )
        _emit_filled(order)
        return await _finish_close(current, order, trigger, clock_now())


async def close_executed_stop(position: Position, fill: OrderRecord) -> Position:
    """Close from an exchange-executed stop. Never submits a sell.

    `fill` is the broker's own record of the execution, from
    `broker.client.get_executed_stop_fills`. Its price and commission are what
    get written; there is no fallback, because a number this module invents is
    indistinguishable downstream from one the broker reported (rule 33).
    """
    fill_price = fill.filled_price
    if fill_price is None:
        raise ValueError(f"stop fill for {position.ticker} carries no executed price")
    async with _locks(position.ticker):
        current = await get_position(position.id)
        if current is None:
            raise PositionStateError(f"position {position.id} is absent")
        if current.stop_order_key:
            with suppress(OrderStateError):
                await settle_stop(
                    current.stop_order_key, StopOrderStatus.EXECUTED, clock_now()
                )
        if current.stop_protection is StopProtection.EXCHANGE:
            current = await _write(
                set_stop_protection(current.id, StopProtection.LOCAL, None)
            )
        key = str(uuid4())
        await _write(
            record_submitting(
                key,
                current.ticker,
                Side.SELL,
                current.lots,
                "EXIT",
                ExitTrigger.STOP_LOSS,
            )
        )
        order = await _write(
            settle_order(
                key,
                OrderStatus.FILLED,
                fill.filled_lots or current.lots,
                fill_price,
                fill.commission,
                "stop executed",
                # `get_executed_stop_fills` keys its records by the broker's
                # exchange_order_id. `key` above is a UUID we invented and the
                # broker has never seen, so this is the only identifier that can
                # ever fetch a commission that lands later (#8).
                fill.key,
            )
        )
        await alert(f"stop executed for {current.ticker}")
        _emit_filled(order)
        return await _finish_close(current, order, ExitTrigger.STOP_LOSS, clock_now())


async def resolve_unfinished(now: datetime) -> list[OrderRecord]:
    """Settle unresolved orders by querying the broker. Never resubmit."""
    _reject_naive(now)
    resolved: list[OrderRecord] = []
    for order in await list_unresolved_orders():
        async with _locks(order.ticker):
            settled = await _resolve_one(order, now)
            if settled is None:
                _emit(
                    logging.WARNING,
                    "order_unresolved",
                    key=order.key,
                    ticker=order.ticker,
                    age_seconds=int((now - order.created_at).total_seconds()),
                )
            else:
                source = (
                    "never_placed"
                    if settled.broker_reason == "never placed"
                    else "query"
                )
                _emit(
                    logging.INFO,
                    "order_resolved",
                    key=settled.key,
                    resolved_status=settled.status.value,
                    source=source,
                )
                resolved.append(settled)
    return resolved


async def _resolve_one(order: OrderRecord, now: datetime) -> OrderRecord | None:
    try:
        state = await get_order_state(order.key)
    except OrderNotFound:
        return await _write(
            settle_order(order.key, OrderStatus.REJECTED, 0, None, None, "never placed")
        )
    except (BrokerUnavailable, BrokerRateLimited):
        return None
    if state.status is OrderStatus.FILLED:
        filled = state.filled_lots if state.filled_lots is not None else 0
        settled = await _write(
            settle_order(
                order.key,
                OrderStatus.FILLED,
                filled,
                state.filled_price,
                state.commission,
                state.broker_reason,
            )
        )
        if filled > 0 and settled.filled_price is not None:
            _emit_filled(settled)
        await _apply_discovered_fill(settled, now)
        return settled
    if state.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
        filled = state.filled_lots if state.filled_lots is not None else 0
        settled = await _write(
            settle_order(
                order.key,
                state.status,
                filled,
                state.filled_price,
                state.commission,
                state.broker_reason,
            )
        )
        await _apply_terminal_exit(settled, now)
        return settled
    if (
        state.status is OrderStatus.SUBMITTED
        and order.intent == "ENTRY"
        and (state.filled_lots or 0) > 0
    ):
        resolved = await _resolve_partial_entry(order.key)
        if resolved is None:
            return None
        got = resolved.filled_lots if resolved.filled_lots is not None else 0
        if 0 < got < order.lots:
            _emit(
                logging.WARNING,
                "partial_fill",
                key=order.key,
                ticker=order.ticker,
                intent="ENTRY",
                requested_lots=order.lots,
                filled_lots=got,
            )
        if got > 0 and resolved.filled_price is not None:
            _emit_filled(resolved)
        await _apply_discovered_fill(resolved, now)
        return resolved
    return None


async def _apply_terminal_exit(order: OrderRecord, now: datetime) -> None:
    """Book, or shrink, a position whose exit order ended without completing.

    A terminal exit that sold part of a position reduces it to the unsold
    remainder and leaves it open. Closing it would leave shares at the broker
    with no local row, which the next reconciliation reports as a foreign
    holding and `app.startup` then refuses to start on (rule 32).

    The sold slice's profit or loss goes unbooked: `db.positions.close` books a
    whole position against one exit price, and a blended figure would be a price
    no order achieved. The gap is bounded above by one position's stop loss,
    alerted, and recorded permanently as a `LOTS_ADJUSTED` event (rule 34).
    """
    if order.intent != "EXIT" or order.side is not Side.SELL:
        return
    filled = order.filled_lots if order.filled_lots is not None else 0
    if filled <= 0 or order.filled_price is None:
        return
    position = await _already_open(order.ticker)
    if position is None:
        return
    if filled >= position.lots:
        # A cancel that landed after a full fill: the exit really did complete.
        if order.exit_trigger is None:
            await alert(
                f"exit order {order.key} has no trigger; leaving {order.ticker} open"
            )
            return
        await _cancel_stop_to_local(position, "exit")
        await _finish_close(position, order, order.exit_trigger, now)
        return
    remaining = position.lots - filled
    await _write(update_lots(position.id, remaining))
    _emit(
        logging.WARNING,
        "partial_fill",
        key=order.key,
        ticker=position.ticker,
        intent="EXIT",
        requested_lots=order.lots,
        filled_lots=filled,
    )
    await alert(
        f"partial exit for {position.ticker}: sold {filled} at "
        f"{order.filled_price}, {remaining} lots still open"
    )


async def _apply_discovered_fill(order: OrderRecord, now: datetime) -> None:
    filled = order.filled_lots if order.filled_lots is not None else 0
    if filled <= 0 or order.filled_price is None:
        return
    if order.intent == "ENTRY" and order.side is Side.BUY:
        if await _already_open(order.ticker) is not None:
            return
        instrument = await get_instrument(order.ticker)
        signal = await _signal_for_recovered_entry(order, now)
        stop, target = _stop_and_target(order.filled_price)
        position = await _write(open_row(signal, order, instrument, stop, target, now))
        _emit(
            logging.INFO,
            "position_opened",
            position_id=position.id,
            ticker=position.ticker,
            strategy=signal.strategy,
            lots=position.lots,
            entry_price=position.entry_price,
            stop_price=position.stop_price,
            target_price=position.target_price,
        )
        await _place_stop(position, instrument)
        return
    if order.intent == "EXIT" and order.side is Side.SELL:
        open_pos = await _already_open(order.ticker)
        if open_pos is None:
            return
        if order.exit_trigger is None:
            await alert(
                f"exit order {order.key} has no trigger; leaving {order.ticker} open"
            )
            return
        await _cancel_stop_to_local(open_pos, "exit")
        await _finish_close(open_pos, order, order.exit_trigger, now)
