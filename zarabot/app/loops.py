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
    list_stop_orders,
)
from zarabot.clock import moscow_date, now, to_moscow, trading_days_between
from zarabot.db.cooldowns import active_until, is_active
from zarabot.db.job_runs import has_run, mark_run
from zarabot.db.orders import DuplicateOrderError
from zarabot.db.positions import PositionStateError, list_open
from zarabot.db.signals import record
from zarabot.db.snapshots import DailySnapshot, list_for_period, write_daily
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
from zarabot.market.session import (
    cache_exhausted,
    calendar,
    covers,
    current_session,
    is_open,
    refresh,
)
from zarabot.models import (
    HaltReason,
    PortfolioState,
    Position,
    RejectionReason,
    RiskDecision,
    StopProtection,
    TradingCalendar,
)
from zarabot.ops.backup import prune
from zarabot.ops.backup import run as backup_run
from zarabot.ops.commissions import backfill
from zarabot.pnl import bot_equity, daily_loss_pct
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
_FAILURES_BEFORE_ALERT = 3
_WEEKLY_HOUR_MSK = 12

_market_failures = 0
_market_alerted = False
# The broker's own back-off hint from the last failure, when it carried one
# (rule 2). Overwritten by every failure and cleared by every success, so a
# hint from a rate limit two cycles ago cannot still be delaying a plain
# outage: that would be stale state wearing the shape of a measurement.
_retry_after: Decimal | None = None
_price_rejected_alerted = False
_stop_discrepancy_alerted = False
_loss_unmeasurable_alerted = False
_age_unmeasurable_alerted = False
_started_at: datetime | None = None
# Deliberately process-local: it means "was THIS process running when the
# session opened", which is what decides whether the day's opening snapshot may
# be written. Persisting it would let a restarted process claim an origin it
# did not have (#27).
_first_cycle_at: datetime | None = None
_snapshot_on: date | None = None
# Shutdown has been requested. Process-local by design: a restarted process
# must accept entries again, so this is the one piece of loop state that would
# be wrong to keep in `db.job_runs` (#21).
_entries_stopped = False
_cache_exhausted_alerted = False


def _poll_seconds(ctx: AppContext) -> float:
    return float(ctx.config.poll_interval_seconds)


async def _note_data_failure(exc: BaseException) -> None:
    global _market_failures, _market_alerted, _retry_after
    _market_failures += 1
    # Read off the type rather than probed for with getattr: a rename should be
    # an AttributeError naming the field, not a hint silently reading as absent
    # and the back-off silently reverting to a guess (#33's failure class).
    _retry_after = exc.retry_after if isinstance(exc, BrokerRateLimited) else None
    _LOG.warning(
        "market data failed (%s consecutive): %s",
        _market_failures,
        exc,
    )
    if _market_failures >= _FAILURES_BEFORE_ALERT and not _market_alerted:
        _market_alerted = True
        await alert(_outage_alert(exc))


def _outage_alert(exc: BaseException) -> str:
    """Name the cause. A rate limit reported as an outage reads as weather."""
    if not isinstance(exc, BrokerRateLimited):
        return "Market data failed for three consecutive cycles; still retrying."
    hint = (
        f" The broker asked for {exc.retry_after}s and that is being honoured."
        if exc.retry_after is not None
        else ""
    )
    return f"Broker rate limit hit on three consecutive cycles; still retrying.{hint}"


def _note_data_success() -> None:
    global _market_failures, _market_alerted, _retry_after
    _market_failures = 0
    _market_alerted = False
    _retry_after = None


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


async def _age_in_trading_days(
    position: Position, moment: datetime, cal: TradingCalendar
) -> int | None:
    """Trading days open, or None when the recorded calendar cannot reach back.

    A count taken against a calendar that does not span the entry comes back
    short, which reads as a young position and silently suppresses MAX_AGE —
    that was #45. `None` says so instead, and `lifecycle.exits` then leaves the
    age trigger alone while stop and target carry on.

    The alert belongs to the cycle, not to this call: see `_report_ages`.
    """
    if not covers(moscow_date(position.entry_at)):
        return None
    return trading_days_between(position.entry_at, moment, cal)


async def _report_ages(unmeasurable: list[str]) -> None:
    """One alert per incident, naming the count, re-armed by a clean cycle.

    The latch was set and never reset, so this fired once per process and a
    second occurrence after recovery was silent — which is #32 exactly, in a
    second module (#48). The condition is not permanent: it clears as soon as
    the recorded calendar reaches back far enough.

    The whole cycle is the unit. Re-arming per position would let one covered
    position clear a warning that an uncovered one still needs.
    """
    global _age_unmeasurable_alerted
    if not unmeasurable:
        _age_unmeasurable_alerted = False
        return
    if _age_unmeasurable_alerted:
        return
    _age_unmeasurable_alerted = True
    await alert(
        f"{len(unmeasurable)} position(s) with an unmeasurable age this cycle "
        f"({', '.join(sorted(unmeasurable))}): the recorded trading calendar "
        "does not reach their entry, so MAX_AGE is suspended for them. "
        "Stop-loss and take-profit are unaffected."
    )


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
    # From the cache market.session already refreshes daily, not a fetch of our
    # own. This re-fetched a fourteen-day schedule every cycle — once a minute,
    # for data that changes at most daily — and on a broker error returned an
    # EMPTY calendar, which made trading_days_between count zero and disabled
    # MAX_AGE exits with nothing raised (#19).
    calendar_now = calendar()
    unmeasurable: list[str] = []
    for position in positions:
        if position.id in skip:
            continue
        price = prices.get(position.ticker)
        if price is None:
            continue
        days = await _age_in_trading_days(position, moment, calendar_now)
        if days is None:
            unmeasurable.append(position.ticker)
        trigger = evaluate(position, price, moment, session, days, ctx.config)
        if trigger is None:
            continue
        try:
            await close_position(position, trigger)
        except (ExitFailed, ValueError):
            _LOG.exception("exit failed for %s trigger=%s", position.ticker, trigger)
            continue
        until = await active_until(
            position.ticker, ctx.config.reentry_cooldown_minutes
        )
        if until is None:
            continue
        _LOG.info(
            "cooldown_started",
            extra={
                "event": "cooldown_started",
                "ticker": position.ticker,
                "active_until": until.isoformat(),
            },
        )
    await _report_ages(unmeasurable)


async def _write_opening_snapshot(moment: datetime, open_count: int) -> None:
    """Record the session-open bot equity, once per Moscow day.

    `pnl.daily_loss_pct` reads this row and writes nothing, so the baseline is
    whatever is written here. Writing it on the first cycle of a session is what
    makes it the session open rather than whenever the process first happened to
    ask: a bot restarted at 14:00 finds this morning's row and measures the whole
    day, instead of seeding a baseline at 14:00 and being structurally blind to
    the morning's drawdown (#9).

    Two guards keep that promise. An existing row is never overwritten. And a
    process that was not yet running when the session opened writes nothing at
    all — the equity it can measure now is not the open, so `pnl` reconstructs
    the baseline from realised P&L and alerts, which is deliberately tighter.
    """
    global _snapshot_on
    today = moscow_date(moment)
    if _snapshot_on == today:
        return
    session = current_session(moment)
    if session is None or session.start is None:
        return
    if _first_cycle_at is None or _first_cycle_at > session.start:
        return
    if await list_for_period(today, today):
        _snapshot_on = today
        return
    equity = await bot_equity()
    await write_daily(
        DailySnapshot(
            trade_date=today,
            opening_equity=equity,
            closing_equity=None,
            cash=equity,
            realised_pnl=Decimal(0),
            unrealised_pnl=Decimal(0),
            open_positions=open_count,
            orders_placed=0,
            benchmark_value=None,
        )
    )
    _snapshot_on = today
    _LOG.info("opening_snapshot trade_date=%s opening_equity=%s", today, equity)


async def _measure_daily_loss(
    ctx: AppContext, moment: datetime, open_count: int
) -> bool:
    """Step 4. `False` when the day's loss could not be measured.

    `pnl.bot_equity` marks every open position to market, so one `PriceRejected`
    or `BrokerUnavailable` makes the loss unknowable rather than merely
    imprecise. Entries stop for that cycle and the owner is alerted, latched —
    but the bot is not halted: the exits at step 3 have already run and must not
    be blocked, and a halt would outlive a condition that is usually momentary.
    """
    global _loss_unmeasurable_alerted
    try:
        await _write_opening_snapshot(moment, open_count)
        loss = await daily_loss_pct(moment)
    except (PriceRejected, BrokerUnavailable) as exc:
        _LOG.warning("daily loss unmeasurable (%s); entries skipped", exc)
        if not _loss_unmeasurable_alerted:
            _loss_unmeasurable_alerted = True
            await alert(
                "Daily loss could not be measured this cycle "
                f"({type(exc).__name__}: {exc}); entries skipped. Exits still "
                "run and trading is not halted."
            )
        return False
    _loss_unmeasurable_alerted = False
    if loss >= ctx.config.daily_loss_limit_pct:
        detail = f"daily loss {loss}% reached limit {ctx.config.daily_loss_limit_pct}%"
        await halt(HaltReason.DAILY_LOSS_LIMIT, detail, moment, daily_loss_pct=loss)
        await alert(detail)
    return True


async def _evaluate_entries(ctx: AppContext, moment: datetime) -> None:
    if not ctx.strategies:
        return
    lookback = max(strategy.lookback for strategy in ctx.strategies)
    candles = await candles_for_watchlist(list(ctx.config.watchlist), lookback, moment)
    portfolio: PortfolioState = await get_portfolio()
    halted = await is_halted()
    session_open = True
    # Tickers opened earlier in THIS pass. Strategies are looped outer and
    # tickers inner, so two strategies can signal one ticker in a single pass,
    # and the gate's duplicate check reads a broker portfolio that lags a
    # market order which has only just filled. The local record is immediately
    # consistent; it lives here because `risk.gate` stays pure (#24).
    opened_this_pass: set[str] = set()
    for strategy in ctx.strategies:
        for ticker in ctx.config.watchlist:
            series = candles.get(ticker)
            if not series:
                continue
            signal = strategy.evaluate(ticker, series, moment)
            if signal is None:
                continue
            _LOG.info(
                "signal_generated",
                extra={
                    "event": "signal_generated",
                    "ticker": signal.ticker,
                    "strategy": signal.strategy,
                    "reference_price": signal.reference_price,
                },
            )
            if ticker in opened_this_pass:
                await record(
                    signal,
                    RiskDecision(
                        approved=False,
                        lots=None,
                        reason=RejectionReason.DUPLICATE_TICKER,
                    ),
                )
                _LOG.info(
                    "signal_rejected",
                    extra={
                        "event": "signal_rejected",
                        "ticker": signal.ticker,
                        "strategy": signal.strategy,
                        "rejection_reason": RejectionReason.DUPLICATE_TICKER.value,
                    },
                )
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
                if decision.reason is not None:
                    _LOG.info(
                        "signal_rejected",
                        extra={
                            "event": "signal_rejected",
                            "ticker": signal.ticker,
                            "strategy": signal.strategy,
                            "rejection_reason": decision.reason.value,
                        },
                    )
                continue
            try:
                await open_position(signal, decision.lots, instrument)
            except OrderRejected as exc:
                await alert(f"entry rejected for {ticker}: {exc.reason}")
                continue
            except (PositionStateError, DuplicateOrderError):
                # A refusal, not a fault. Both guards are correct to fire; only
                # OrderRejected had a branch, so a correct refusal reached the
                # supervisor, alerted "Background task trading crashed" and
                # abandoned every remaining ticker in the pass (#24).
                _LOG.info("entry refused as duplicate for %s", ticker)
                continue
            opened_this_pass.add(ticker)
            # Still re-read from the broker: this is what keeps MAX_POSITIONS,
            # PORTFOLIO_EXPOSURE and cash correct within a pass. Dropping it to
            # save a call would trade a spurious alert for a breached limit.
            portfolio = await get_portfolio()


def stop_entries() -> None:
    """Refuse new entries from the next cycle. Exits are unaffected.

    `app.shutdown` calls this before it drains. Without it the drain ran
    concurrently with the trading loop and the runner was cancelled only after
    the drain returned, so for the whole window the loop could open a position
    the drain had already looked past (#21).
    """
    global _entries_stopped
    _entries_stopped = True


async def trading_cycle(ctx: AppContext) -> None:
    """One iteration: session guard, exits, daily-loss halt, then entries."""
    global _cache_exhausted_alerted, _first_cycle_at
    moment = now()
    if moment.tzinfo is None:
        # Naive "now" is a contract violation, not weather. Do not call
        # `datetime.now()` here to compare; clock.now() is the sole "now".
        _LOG.warning("clock_drift", extra={"event": "clock_drift", "drift_seconds": 0})
        return
    # Recorded before the session guard: a cycle that returns because the market
    # is shut is still evidence this process was running before the open, which
    # is what entitles it to write the day's opening snapshot at step 4.
    if _first_cycle_at is None:
        _first_cycle_at = moment
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
        if not await _measure_daily_loss(ctx, moment, len(positions)):
            return
        if _entries_stopped:
            # Same shape as the halt check below: a process on its way down must
            # not open what nobody will be watching, and must not be stopped
            # from closing what is already open (#21).
            _note_data_success()
            return
        if await is_halted():
            _note_data_success()
            return
        await _evaluate_entries(ctx, moment)
    except (BrokerUnavailable, BrokerRateLimited) as exc:
        await _note_data_failure(exc)
        return
    _note_data_success()


def _next_delay(ctx: AppContext) -> float:
    """How long to wait before the next cycle.

    The escalation belongs to rule 1 and the hint to rule 2, and the longer of
    the two wins. Longer, not the hint alone: a two-second hint must not undo a
    back-off five failed cycles deep, and the escalation already reflects how
    many cycles have failed. Longer, not the escalation alone: the broker is
    the only party that knows when it will accept calls again, and until v1.53
    it was telling us and nothing listened.

    `_MAX_BACKOFF` bounds the hint as well as our own escalation. This loop
    submits exits, so no number supplied from outside may hold it asleep.
    """
    delay = float(_poll_seconds(ctx))
    if _market_failures:
        delay *= 2 ** min(_market_failures, 5)
    if _retry_after is not None:
        delay = max(delay, float(_retry_after))
    return min(delay, _MAX_BACKOFF)


async def _trading_loop(ctx: AppContext) -> None:
    while True:
        await trading_cycle(ctx)
        await asyncio.sleep(_next_delay(ctx))


async def _rollover_loop(ctx: AppContext) -> None:
    while True:
        moment = now()
        day = moscow_date(moment)
        if is_open(moment) and not await has_run("rollover", day.isoformat()):
            # No daily_loss_pct here. It used to be called to force the lazy
            # snapshot write; it now only reads the row, which at rollover does
            # not exist yet, so the call would fire `pnl`'s reconstruction alert
            # and nothing else. The row is written by trading_cycle step 4.
            await backfill(moment - _BACKFILL_LOOKBACK, moment)
            await mark_run("rollover", day.isoformat(), moment)
        await asyncio.sleep(_poll_seconds(ctx))


async def _backup_loop(ctx: AppContext) -> None:
    while True:
        moment = now()
        day = moscow_date(moment)
        if not is_open(moment) and not await has_run("backup", day.isoformat()):
            await backup_run(ctx.config.db_path, ctx.config.backup_dir)
            await prune(ctx.config.backup_dir, _BACKUP_RETENTION_DAYS)
            await mark_run("backup", day.isoformat(), moment)
        await asyncio.sleep(_poll_seconds(ctx))


async def _weekly_loop(ctx: AppContext) -> None:
    """Due from Sunday noon MSK onward, not only during that one hour.

    The old condition required the loop to observe an instant inside
    12:00-12:59. A process down, restarting, or backing off through that hour
    skipped the week entirely — no report, no alert, no record, against
    acceptance criterion 9. A report delivered at 14:00 after a restart is
    strictly better than none (#27).
    """
    while True:
        moment = now()
        local = to_moscow(moment)
        week_start = local.date() - timedelta(days=local.weekday())
        due = local.weekday() == 6 and local.hour >= _WEEKLY_HOUR_MSK
        if due and not await has_run("weekly_report", week_start.isoformat()):
            await backfill(moment - _BACKFILL_LOOKBACK, moment)
            await send_report(moment)
            await mark_run("weekly_report", week_start.isoformat(), moment)
        await asyncio.sleep(_poll_seconds(ctx))


async def _schedule_refresh_loop(ctx: AppContext) -> None:
    global _cache_exhausted_alerted
    while True:
        moment = now()
        day = moscow_date(moment)
        if not await has_run("schedule_refresh", day.isoformat()):
            await refresh(_SCHEDULE_DAYS)
            await mark_run("schedule_refresh", day.isoformat(), moment)
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
    while True:
        moment = now()
        day = moscow_date(moment)
        if not await has_run("heartbeat", day.isoformat()):
            opened = await list_open()
            halted = await is_halted()
            origin = _started_at if _started_at is not None else moment
            uptime = int((moment - origin).total_seconds())
            _LOG.info(
                "heartbeat uptime_seconds=%s open_positions=%s halted=%s",
                uptime,
                len(opened),
                halted,
                extra={
                    "event": "heartbeat",
                    "uptime_seconds": uptime,
                    "open_positions": len(opened),
                    "halted": halted,
                },
            )
            await alert(
                f"zarabot heartbeat uptime={uptime}s "
                f"positions={len(opened)} halted={halted}"
            )
            await mark_run("heartbeat", day.isoformat(), moment)
        await asyncio.sleep(_poll_seconds(ctx))


async def _supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    delay = 1.0
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.exception(
                "task_crashed",
                extra={
                    "event": "task_crashed",
                    "task": name,
                    "error": type(exc).__name__,
                    "restart_in_seconds": delay,
                },
            )
            await alert(f"Background task {name} crashed: {exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        else:
            delay = 1.0


async def run(ctx: AppContext) -> None:
    """Sole owner of composition: start every long-running task, nowhere else."""
    global _started_at, _entries_stopped
    _started_at = now()
    # A new run accepts entries. The flag is process-local by design (#21), and
    # clearing it here is what makes that true for a process that is starting
    # rather than stopping.
    _entries_stopped = False
    await asyncio.gather(
        _supervise("trading", lambda: _trading_loop(ctx)),
        _supervise("rollover", lambda: _rollover_loop(ctx)),
        _supervise("schedule", lambda: _schedule_refresh_loop(ctx)),
        _supervise("backup", lambda: _backup_loop(ctx)),
        _supervise("weekly", lambda: _weekly_loop(ctx)),
        _supervise("heartbeat", lambda: _heartbeat_loop(ctx)),
        _supervise("telegram", _telegram_loop),
    )
