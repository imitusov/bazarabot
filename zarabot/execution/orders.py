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

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    OrderNotFound,
    OrderRejected,
    StopOrderRejected,
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
from zarabot.db.positions import PositionStateError, list_open, set_stop_protection
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

_global_lock = asyncio.Lock()
_ticker_locks: dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


class ExitFailed(Exception):
    """An exit could not be completed. The caller retries on the next cycle."""


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


async def _halt_on_db_failure(detail: str) -> None:
    await alert(f"trading halted: {detail}")
    try:
        await halt(HaltReason.MANUAL, detail, clock_now())
    except Exception:
        _LOG.exception("halt after database failure also failed")


@asynccontextmanager
async def _locks(ticker: str) -> AsyncIterator[None]:
    async with _registry_lock:
        lock = _ticker_locks.get(ticker)
        if lock is None:
            lock = asyncio.Lock()
            _ticker_locks[ticker] = lock
    async with lock, _global_lock:
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
    day = moscow_date(moment)
    pairs = await list_for_period(day, day)
    for signal, _decision in reversed(pairs):
        if signal.ticker == order.ticker:
            return signal
    price = order.filled_price if order.filled_price is not None else Decimal("0")
    return Signal(
        ticker=order.ticker,
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=moment,
        reference_price=price,
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
            return await _write(
                set_stop_protection(position.id, StopProtection.EXCHANGE, key)
            )
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
        return await _write(
            set_stop_protection(position.id, StopProtection.EXCHANGE, stop.key)
        )


async def cancel_orphaned_stop(stop: StopOrderRecord) -> None:
    """Cancel a live stop that has no matching open position."""
    async with _locks(stop.ticker):
        if stop.stop_order_id:
            await cancel_stop_order(stop.stop_order_id)
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
        await _cancel_stop_to_local(position)
        loaded = await get_position(position.id)
        if loaded is None:
            return position
        return await _place_stop(loaded, instrument)


async def _cancel_stop_to_local(position: Position) -> Position:
    standing = await active_for_position(position.id)
    if standing is not None:
        if standing.stop_order_id:
            await cancel_stop_order(standing.stop_order_id)
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
            raise OrderRejected("max lots is 0")
        key = str(uuid4())
        await _write(record_submitting(key, signal.ticker, Side.BUY, lots, "ENTRY"))
        try:
            posted = await post_market_order(key, instrument.figi, Side.BUY, lots)
        except OrderRejected as exc:
            await _write(
                settle_order(key, OrderStatus.REJECTED, 0, None, None, exc.reason)
            )
            await alert(f"entry rejected for {signal.ticker}: {exc.reason}")
            raise
        except (BrokerUnavailable, BrokerRateLimited):
            raise
        filled_lots = posted.filled_lots if posted.filled_lots is not None else 0
        fill_price = posted.filled_price
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
        if filled_lots < lots:
            await alert(
                f"partial entry fill for {signal.ticker}: {filled_lots} of {lots}"
            )
        return await _open_from_fill(signal, settled, instrument)


async def _open_from_fill(
    signal: Signal, order: OrderRecord, instrument: Instrument
) -> Position:
    if order.filled_price is None:
        raise OrderRejected("fill has no price")
    stop, target = _stop_and_target(order.filled_price)
    position = await _write(
        open_row(signal, order, instrument, stop, target, clock_now())
    )
    return await _place_stop(position, instrument)


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
            current = await _cancel_stop_to_local(current)
        remaining = current.lots
        last_order: OrderRecord | None = None
        while remaining > 0:
            key = str(uuid4())
            await _write(
                record_submitting(
                    key, current.ticker, Side.SELL, remaining, "EXIT", trigger
                )
            )
            try:
                posted = await post_market_order(
                    key, current.figi, Side.SELL, remaining
                )
            except OrderRejected as exc:
                await _write(
                    settle_order(key, OrderStatus.REJECTED, 0, None, None, exc.reason)
                )
                await alert(f"exit rejected for {current.ticker}: {exc.reason}")
                raise ExitFailed(exc.reason) from exc
            except (BrokerUnavailable, BrokerRateLimited) as exc:
                await alert(f"exit unreachable for {current.ticker}: {exc}")
                raise ExitFailed(str(exc)) from exc
            filled = posted.filled_lots if posted.filled_lots is not None else 0
            price = posted.filled_price
            if posted.status is not OrderStatus.FILLED or filled <= 0 or price is None:
                await alert(f"exit outcome unknown for {current.ticker}")
                raise ExitFailed("exit outcome unknown")
            last_order = await _write(
                settle_order(
                    key,
                    OrderStatus.FILLED,
                    filled,
                    price,
                    posted.commission,
                    posted.broker_reason,
                )
            )
            remaining -= filled
        if last_order is None:  # pragma: no cover
            raise ExitFailed("no exit fill")
        return await _finish_close(current, last_order, trigger, clock_now())


async def close_executed_stop(position: Position, fill: OrderRecord) -> Position:
    """Close from an exchange-executed stop. Never submits a sell.

    `fill` is the broker's own record of the execution, from
    `broker.client.get_executed_stop_fills`. Its price and commission are what
    get written; there is no fallback, because a number this module invents is
    indistinguishable downstream from one the broker reported (rule 33).
    """
    fill_price = fill.filled_price
    if fill_price is None:
        raise ValueError(
            f"stop fill for {position.ticker} carries no executed price"
        )
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
            )
        )
        await alert(f"stop executed for {current.ticker}")
        return await _finish_close(current, order, ExitTrigger.STOP_LOSS, clock_now())


async def resolve_unfinished(now: datetime) -> list[OrderRecord]:
    """Settle unresolved orders by querying the broker. Never resubmit."""
    _reject_naive(now)
    resolved: list[OrderRecord] = []
    for order in await list_unresolved_orders():
        async with _locks(order.ticker):
            settled = await _resolve_one(order, now)
            if settled is not None:
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
        await _apply_discovered_fill(settled, now)
        return settled
    if state.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
        filled = state.filled_lots if state.filled_lots is not None else 0
        return await _write(
            settle_order(
                order.key,
                state.status,
                filled,
                state.filled_price,
                state.commission,
                state.broker_reason,
            )
        )
    return None


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
        await _cancel_stop_to_local(open_pos)
        await _finish_close(open_pos, order, order.exit_trigger, now)
