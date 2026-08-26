"""Trading cycle and background schedules. Exits always run; halt blocks entries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from zarabot.app.startup import AppContext
from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderRejected,
    PriceRejected,
    get_executed_stop_fills,
    get_instrument,
    get_last_price,
    get_portfolio,
    get_trading_schedule,
    list_stop_orders,
)
from zarabot.clock import moscow_date, now, to_moscow, trading_days_between
from zarabot.db.cooldowns import is_active
from zarabot.db.positions import list_open
from zarabot.db.signals import record
from zarabot.db.stop_orders import active_for_position
from zarabot.execution.orders import (
    ExitFailed,
    close_executed_stop,
    close_position,
    open_position,
    resolve_unfinished,
)
from zarabot.lifecycle.exits import evaluate
from zarabot.market.data import candles_for_watchlist
from zarabot.market.session import cache_exhausted, current_session, is_open, refresh
from zarabot.models import (
    HaltReason,
    PortfolioState,
    Position,
    StopProtection,
    TradingCalendar,
)
from zarabot.ops.backup import prune
from zarabot.ops.backup import run as backup_run
from zarabot.ops.commissions import backfill
from zarabot.pnl import daily_loss_pct
from zarabot.reporter.weekly import send as send_report
from zarabot.risk.gate import check
from zarabot.state.halt import halt, is_halted
from zarabot.telegram.commands import build_application
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_SCHEDULE_DAYS = 14
_BACKUP_RETENTION_DAYS = 30
_BACKFILL_LOOKBACK = timedelta(days=7)
_MAX_BACKOFF = 3600

_market_failures = 0
_market_alerted = False
_price_rejected_alerted = False
_stop_discrepancy_alerted = False
_started_at: datetime | None = None
_rolled_on: date | None = None
_backed_up_on: date | None = None
_heartbeat_on: date | None = None
_weekly_on: date | None = None
_refreshed_on: date | None = None
_cache_exhausted_alerted = False


def _poll_seconds(ctx: AppContext) -> float:
    return float(ctx.config.poll_interval_seconds)


async def _note_data_failure(exc: BaseException) -> None:
    global _market_failures, _market_alerted
    _market_failures += 1
    _LOG.warning(
        "market data failed (%s consecutive): %s",
        _market_failures,
        exc,
    )
    if _market_failures >= 3 and not _market_alerted:
        _market_alerted = True
        await alert("Market data failed for three consecutive cycles; still retrying.")


def _note_data_success() -> None:
    global _market_failures, _market_alerted
    _market_failures = 0
    _market_alerted = False


async def _prices_for(positions: list[Position]) -> dict[str, Decimal]:
    global _price_rejected_alerted
    prices: dict[str, Decimal] = {}
    rejected = 0
    for position in positions:
        try:
            prices[position.ticker] = await get_last_price(position.figi)
        except PriceRejected:
            rejected += 1
            continue
    if rejected:
        if not _price_rejected_alerted:
            _price_rejected_alerted = True
            await alert(
                f"{rejected} instrument price(s) rejected this cycle; "
                "those instruments skipped."
            )
    else:
        _price_rejected_alerted = False
    return prices


def _stop_is_live(
    position: Position, standing_keys: set[str], broker_id: str | None = None
) -> bool:
    """Live if EITHER identifier is standing.

    Our `stop_order_key` is a UUID. `list_stop_orders` keys a stop by
    `order_request_id` when the broker supplies one and by `stop_order_id`
    otherwise, so when the broker omits it our UUID matches nothing — and a
    perfectly live stop would be reported missing on every cycle (#5).
    """
    candidates = {value for value in (position.stop_order_key, broker_id) if value}
    return bool(candidates & standing_keys)


def _moscow_day_start(moment: datetime) -> datetime:
    """Midnight of the current Moscow day, as an aware UTC instant."""
    moscow = to_moscow(moment)
    return moscow.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def _close_executed(positions: list[Position]) -> set[int]:
    """Close positions whose exchange stop the BROKER says has executed.

    Execution is confirmed, never inferred. The previous rule concluded a stop
    had fired from two absences — the stop missing from the active list and the
    ticker missing from the portfolio — each an eventually-consistent read, and
    correlated rather than independent when the broker hiccups. A false positive
    closed a live position at a quote, cooled down an instrument still held, and
    left the shares to be re-adopted at a new cost basis (#4, #5).
    """
    global _stop_discrepancy_alerted
    closed: set[int] = set()
    exchange = [
        position
        for position in positions
        if position.stop_protection is StopProtection.EXCHANGE
    ]
    if not exchange:
        _stop_discrepancy_alerted = False
        return closed

    moment = now()
    fills = await get_executed_stop_fills(_moscow_day_start(moment), moment)
    standing = await list_stop_orders()
    live: set[str] = {stop.key for stop in standing}
    live.update(stop.stop_order_id for stop in standing if stop.stop_order_id)

    unexplained: list[str] = []
    for position in exchange:
        # Match on the identifier the BROKER issued and we persisted, never on
        # our own UUID: list_stop_orders keys on order_request_id when the
        # broker supplies one, so our key may match nothing for a live stop.
        record = await active_for_position(position.id)
        broker_id = record.stop_order_id if record is not None else None
        fill = fills.get(broker_id) if broker_id else None
        if fill is not None:
            await close_executed_stop(position, fill)
            closed.add(position.id)
            continue
        if not _stop_is_live(position, live, broker_id):
            unexplained.append(position.ticker)

    if unexplained:
        if not _stop_discrepancy_alerted:
            _stop_discrepancy_alerted = True
            await alert(
                "stop no longer live with no confirmed execution for "
                f"{', '.join(sorted(unexplained))}; position left open",
                urgent=True,
            )
    else:
        _stop_discrepancy_alerted = False
    return closed


async def _calendar() -> TradingCalendar:
    try:
        sessions = await get_trading_schedule(_SCHEDULE_DAYS)
    except (BrokerUnavailable, BrokerRateLimited):
        return TradingCalendar(sessions=())
    return TradingCalendar(sessions=tuple(sessions))


async def _submit_exits(
    positions: list[Position],
    prices: dict[str, Decimal],
    skip: set[int],
    moment: datetime,
    ctx: AppContext,
) -> None:
    session = current_session(moment)
    if session is None:
        return
    calendar = await _calendar()
    for position in positions:
        if position.id in skip:
            continue
        price = prices.get(position.ticker)
        if price is None:
            continue
        days = trading_days_between(position.entry_at, moment, calendar)
        trigger = evaluate(position, price, moment, session, days, ctx.config)
        if trigger is None:
            continue
        try:
            await close_position(position, trigger)
        except (ExitFailed, ValueError):
            _LOG.exception("exit failed for %s trigger=%s", position.ticker, trigger)


async def _maybe_halt_on_loss(ctx: AppContext, moment: datetime) -> None:
    loss = await daily_loss_pct(moment)
    if loss < ctx.config.daily_loss_limit_pct:
        return
    detail = f"daily loss {loss}% reached limit {ctx.config.daily_loss_limit_pct}%"
    await halt(HaltReason.DAILY_LOSS_LIMIT, detail, moment)
    await alert(detail)


async def _evaluate_entries(ctx: AppContext, moment: datetime) -> None:
    if not ctx.strategies:
        return
    lookback = max(strategy.lookback for strategy in ctx.strategies)
    candles = await candles_for_watchlist(list(ctx.config.watchlist), lookback, moment)
    portfolio: PortfolioState = await get_portfolio()
    halted = await is_halted()
    session_open = True
    for strategy in ctx.strategies:
        for ticker in ctx.config.watchlist:
            series = candles.get(ticker)
            if not series:
                continue
            signal = strategy.evaluate(ticker, series, moment)
            if signal is None:
                continue
            try:
                instrument = await get_instrument(ticker)
            except InstrumentNotFound:
                _LOG.warning("instrument unavailable; skipping %s", ticker)
                continue
            cooldown = await is_active(
                ticker, moment, ctx.config.reentry_cooldown_minutes
            )
            decision = check(
                signal,
                portfolio,
                instrument,
                cooldown,
                session_open,
                halted,
                moment,
                ctx.config,
            )
            await record(signal, decision)
            if not decision.approved or decision.lots is None:
                continue
            try:
                await open_position(signal, decision.lots, instrument)
            except OrderRejected as exc:
                await alert(f"entry rejected for {ticker}: {exc.reason}")
                continue
            portfolio = await get_portfolio()


async def trading_cycle(ctx: AppContext) -> None:
    """One iteration: session guard, exits, daily-loss halt, then entries."""
    global _cache_exhausted_alerted
    moment = now()
    if not is_open(moment):
        if cache_exhausted(moment):
            if not _cache_exhausted_alerted:
                _cache_exhausted_alerted = True
                await alert(
                    "Trading schedule cache exhausted; is_open is false "
                    "because the calendar ran out, not because the market is shut."
                )
        else:
            _cache_exhausted_alerted = False
        return
    try:
        await resolve_unfinished(moment)
        positions = await list_open()
        prices = await _prices_for(positions)
        executed = await _close_executed(positions)
        await _submit_exits(positions, prices, executed, moment, ctx)
        await _maybe_halt_on_loss(ctx, moment)
        if await is_halted():
            _note_data_success()
            return
        await _evaluate_entries(ctx, moment)
    except (BrokerUnavailable, BrokerRateLimited) as exc:
        await _note_data_failure(exc)
        return
    _note_data_success()


async def _trading_loop(ctx: AppContext) -> None:
    while True:
        await trading_cycle(ctx)
        delay = _poll_seconds(ctx)
        if _market_failures:
            delay = min(delay * (2 ** min(_market_failures, 5)), _MAX_BACKOFF)
        await asyncio.sleep(delay)


async def _rollover_loop(ctx: AppContext) -> None:
    global _rolled_on
    while True:
        moment = now()
        day = moscow_date(moment)
        if is_open(moment) and _rolled_on != day:
            await daily_loss_pct(moment)
            await backfill(moment - _BACKFILL_LOOKBACK, moment)
            _rolled_on = day
        await asyncio.sleep(_poll_seconds(ctx))


async def _backup_loop(ctx: AppContext) -> None:
    global _backed_up_on
    while True:
        moment = now()
        day = moscow_date(moment)
        if not is_open(moment) and _backed_up_on != day:
            await backup_run(ctx.config.db_path, ctx.config.backup_dir)
            await prune(ctx.config.backup_dir, _BACKUP_RETENTION_DAYS)
            _backed_up_on = day
        await asyncio.sleep(_poll_seconds(ctx))


async def _weekly_loop(ctx: AppContext) -> None:
    global _weekly_on
    while True:
        moment = now()
        local = to_moscow(moment)
        week_start = local.date() - timedelta(days=local.weekday())
        if local.weekday() == 6 and local.hour == 12 and _weekly_on != week_start:
            await backfill(moment - _BACKFILL_LOOKBACK, moment)
            await send_report(moment)
            _weekly_on = week_start
        await asyncio.sleep(_poll_seconds(ctx))


async def _schedule_refresh_loop(ctx: AppContext) -> None:
    global _refreshed_on, _cache_exhausted_alerted
    while True:
        moment = now()
        day = moscow_date(moment)
        if _refreshed_on != day:
            await refresh(_SCHEDULE_DAYS)
            _refreshed_on = day
            _cache_exhausted_alerted = False
        await asyncio.sleep(_poll_seconds(ctx))


async def _telegram_loop() -> None:
    application = build_application()
    await application.initialize()
    await application.start()
    updater = getattr(application, "updater", None)
    if updater is not None:
        await updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        if updater is not None:
            await updater.stop()
        await application.stop()
        await application.shutdown()


async def _heartbeat_loop(ctx: AppContext) -> None:
    global _heartbeat_on
    while True:
        moment = now()
        day = moscow_date(moment)
        if _heartbeat_on != day:
            opened = await list_open()
            halted = await is_halted()
            origin = _started_at if _started_at is not None else moment
            uptime = int((moment - origin).total_seconds())
            _LOG.info(
                "heartbeat uptime_seconds=%s open_positions=%s halted=%s",
                uptime,
                len(opened),
                halted,
            )
            await alert(
                f"zarabot heartbeat uptime={uptime}s "
                f"positions={len(opened)} halted={halted}"
            )
            _heartbeat_on = day
        await asyncio.sleep(_poll_seconds(ctx))


async def _supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    delay = 1.0
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.exception("task_crashed task=%s error=%s", name, exc)
            await alert(f"Background task {name} crashed: {exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        else:
            delay = 1.0


async def run(ctx: AppContext) -> None:
    """Sole owner of composition: start every long-running task, nowhere else."""
    global _started_at
    _started_at = now()
    await asyncio.gather(
        _supervise("trading", lambda: _trading_loop(ctx)),
        _supervise("rollover", lambda: _rollover_loop(ctx)),
        _supervise("schedule", lambda: _schedule_refresh_loop(ctx)),
        _supervise("backup", lambda: _backup_loop(ctx)),
        _supervise("weekly", lambda: _weekly_loop(ctx)),
        _supervise("heartbeat", lambda: _heartbeat_loop(ctx)),
        _supervise("telegram", _telegram_loop),
    )
