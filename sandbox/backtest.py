"""Replay history by running the live trading cycle against a simulated broker.

The old module imported `strategies`, `risk.sizing` and `lifecycle.exits` but
**not** `risk.gate` — obeying "never reimplement" while omitting the gate
entirely, so cooldowns, `max_open_positions`, duplicate-ticker rejection, halt
and session state played no part in any result. An omission reads as
compliance, which is why it survived (#12).

Nothing is reimplemented here now, because nothing needs to be: the simulation
runs `app.loops.trading_cycle` itself. Live and backtest cannot diverge, since
they are the same code.

Never imported by `zarabot/`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from sandbox.exchange import Commission, Phase, SimulatedExchange
from zarabot.config import Config
from zarabot.models import (
    BacktestResult,
    Candle,
    ExitTrigger,
    Instrument,
    Position,
    ReconciliationReport,
)

_ZERO = Decimal("0")

# The simulated session runs from the bar timestamp to +8h45m, matching what
# `SimulatedExchange.get_trading_schedule` reports.
_SESSION_MINUTES = 525

# Four cycles a bar, in this order: open, low, high, close — the drawdown
# before the recovery. That is the same pessimism as the stop-beats-target
# tie-break, and for the same reason: a daily bar cannot say which came first,
# and only the pessimistic reading cannot flatter the result.
#
# The last sits five minutes before the session end, inside
# `in_closing_window`'s fifteen. That is what makes MAX_AGE reachable:
# `lifecycle.exits` requires it, and a single cycle at the session start never
# satisfied it (#53).
_MARKS: tuple[tuple[int, Phase], ...] = (
    (0, Phase.OPEN),
    (_SESSION_MINUTES // 3, Phase.LOW),
    (2 * _SESSION_MINUTES // 3, Phase.HIGH),
    (_SESSION_MINUTES - 5, Phase.CLOSE),
)


def _seams(
    exchange: SimulatedExchange, clock: _Clock, config: Config
) -> list[tuple[str, str, Any]]:
    """Every place a live module reached the broker or the clock.

    Written as an explicit table rather than discovered dynamically, so it can
    be read and audited. `test_no_real_broker_call_escapes` is what proves it
    complete: a missed seam reaches the network, which is how #39, #43 and the
    reverted #45 attempt survived their tests.
    """
    return [
        # broker.client, module by module, as each imported it by name
        (
            "zarabot.app.loops",
            "get_executed_stop_fills",
            exchange.get_executed_stop_fills,
        ),
        ("zarabot.app.loops", "get_instrument", exchange.get_instrument),
        ("zarabot.app.loops", "get_last_price", exchange.get_last_price),
        ("zarabot.app.loops", "get_portfolio", exchange.get_portfolio),
        ("zarabot.app.loops", "list_stop_orders", exchange.list_stop_orders),
        ("zarabot.execution.orders", "cancel_order", exchange.cancel_order),
        ("zarabot.execution.orders", "cancel_stop_order", exchange.cancel_stop_order),
        ("zarabot.execution.orders", "get_instrument", exchange.get_instrument),
        ("zarabot.execution.orders", "get_max_lots", exchange.get_max_lots),
        ("zarabot.execution.orders", "get_order_state", exchange.get_order_state),
        ("zarabot.execution.orders", "post_market_order", exchange.post_market_order),
        ("zarabot.execution.orders", "post_stop_loss", exchange.post_stop_loss),
        ("zarabot.market.data", "get_candles", exchange.get_candles),
        ("zarabot.market.data", "get_instrument", exchange.get_instrument),
        (
            "zarabot.market.session",
            "get_trading_schedule",
            exchange.get_trading_schedule,
        ),
        ("zarabot.pnl", "get_candles", exchange.get_candles),
        ("zarabot.pnl", "get_instrument", exchange.get_instrument),
        ("zarabot.pnl", "get_last_price", exchange.get_last_price),
        # the clock, wherever a module bound it at import
        ("zarabot.app.loops", "now", clock.now),
        ("zarabot.db.orders", "now", clock.now),
        ("zarabot.db.positions", "now", clock.now),
        ("zarabot.db.stop_orders", "now", clock.now),
        ("zarabot.db.trading_days", "clock_now", clock.now),
        ("zarabot.execution.orders", "clock_now", clock.now),
        # configuration, wherever a module reaches for it rather than being
        # handed it. The backtest's Config is the one the whole run must see.
        ("zarabot.db.positions", "load", lambda: config),
        ("zarabot.execution.orders", "load", lambda: config),
        ("zarabot.pnl", "config", _ConfigModule(config)),
        # Alerts go nowhere. At the source as well as in every importer, so a
        # module that starts importing it later is covered by default — the
        # table patched four while eleven imported it, and state.halt was not
        # among them (#49).
        ("zarabot.telegram.notifier", "alert", _silent),
        ("zarabot.app.loops", "alert", _silent),
        ("zarabot.app.shutdown", "alert", _silent),
        ("zarabot.app.startup", "alert", _silent),
        ("zarabot.broker.reconcile", "alert", _silent),
        ("zarabot.execution.orders", "alert", _silent),
        ("zarabot.market.data", "alert", _silent),
        ("zarabot.market.session", "alert", _silent),
        ("zarabot.ops.backup", "alert", _silent),
        ("zarabot.ops.commissions", "alert", _silent),
        ("zarabot.pnl", "alert", _silent),
        ("zarabot.reporter.weekly", "alert", _silent),
        ("zarabot.state.halt", "alert", _silent),
    ]


async def _silent(*_args: object, **_kwargs: object) -> None:
    return None


class _ConfigModule:
    """Stands in for the `config` module where one is imported wholesale."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def get(self) -> Config:
        return self._config

    def load(self) -> Config:
        return self._config


class _Clock:
    """Simulated time. `now()` answers with whichever bar is being replayed."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def set(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


@contextmanager
def _patched(seams: list[tuple[str, str, Any]]) -> Iterator[None]:
    import importlib

    saved: list[tuple[Any, str, Any]] = []
    try:
        for module_name, attr, replacement in seams:
            module = importlib.import_module(module_name)
            saved.append((module, attr, getattr(module, attr)))
            setattr(module, attr, replacement)
        yield
    finally:
        for module, attr, original in reversed(saved):
            setattr(module, attr, original)


def _reset_loop_state() -> None:
    """Clear the module-level latches a fresh run must not inherit."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._price_rejected_alerted = False
    loops._stop_discrepancy_alerted = False
    loops._loss_unmeasurable_alerted = False
    loops._age_unmeasurable_alerted = False
    loops._cache_exhausted_alerted = False
    loops._entries_stopped = False
    loops._started_at = None
    loops._first_cycle_at = None
    loops._snapshot_on = None


def _reset_data_state() -> None:
    """market.data counts consecutive candle failures per ticker (rule 9)."""
    import zarabot.market.data as data

    data._failures.clear()
    data._alerted.clear()


def _reset_session_state() -> None:
    import zarabot.market.session as session

    session._cache = None
    session._history = None
    session._earliest = None
    session._alerted = False


def _drawdown(equity: Sequence[Decimal]) -> Decimal:
    peak = _ZERO
    worst = _ZERO
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            fall = (peak - value) / peak * Decimal("100")
            worst = max(worst, fall)
    return worst


def _benchmark(bars: dict[str, list[Candle]]) -> Decimal | None:
    series = [candles for candles in bars.values() if len(candles) >= 2]
    if not series:
        return None
    returns = [
        (candles[-1].close - candles[0].close) / candles[0].close
        for candles in series
        if candles[0].close != 0
    ]
    if not returns:
        return None
    return sum(returns, _ZERO) / Decimal(len(returns))


async def run(
    bars: dict[str, list[Candle]],
    instruments: dict[str, Instrument],
    config: Config,
    strategies: Sequence[object],
    commission: Commission,
    slippage: Decimal,
    reject_stops: bool = False,
) -> BacktestResult:
    """Replay `bars` through the live trading cycle. Returns what it did."""
    from zarabot.app.loops import trading_cycle
    from zarabot.app.startup import AppContext
    from zarabot.db.connection import connect, disconnect
    from zarabot.db.migrations import apply
    from zarabot.db.positions import list_closed, list_open
    from zarabot.market.session import refresh
    from zarabot.pnl import bot_equity

    timeline = sorted({bar.timestamp for candles in bars.values() for bar in candles})
    if not timeline:
        return BacktestResult(
            trades=(),
            pnl=_ZERO,
            win_rate=_ZERO,
            max_drawdown=_ZERO,
            exit_trigger_distribution=(),
            benchmark_return=None,
        )

    clock = _Clock(timeline[0])
    exchange = SimulatedExchange(
        bars=bars,
        instruments=instruments,
        cash=config.allocated_capital,
        slippage=slippage,
        commission=commission,
        reject_stops=reject_stops,
    )
    ctx = AppContext(
        config=config,
        strategies=tuple(strategies),  # type: ignore[arg-type]
        halt=None,
        reconciliation=ReconciliationReport(ran_at=timeline[0], adjustments=()),
    )

    equity: list[Decimal] = []
    closed: list[Position] = []
    still_open: list[Position] = []

    with ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(_patched(_seams(exchange, clock, config)))
        _reset_loop_state()
        _reset_data_state()
        _reset_session_state()

        path = str(Path(tmp) / "backtest.db")
        conn: aiosqlite.Connection = await connect(path)
        try:
            await apply(conn)
            await exchange.advance(timeline[0])
            await refresh(len(timeline))
            for moment in timeline:
                for minutes, phase in _MARKS:
                    at = moment + timedelta(minutes=minutes)
                    clock.set(at)
                    await exchange.advance(at, phase=phase)
                    await trading_cycle(ctx)
                    # Marked to market on EVERY mark. The old module appended
                    # the cash balance on trade events only, and cash falls
                    # when you buy, so its drawdown was roughly the position
                    # size (#12).
                    equity.append(await bot_equity())
            closed = await list_closed()
            still_open = await list_open()
        finally:
            await disconnect()

    realised = sum((row.realised_pnl or _ZERO for row in closed), _ZERO)
    wins = sum(1 for row in closed if (row.realised_pnl or _ZERO) > 0)
    win_rate = Decimal(wins) / Decimal(len(closed)) if closed else _ZERO
    counts: dict[ExitTrigger, int] = {}
    for row in closed:
        if row.exit_trigger is not None:
            counts[row.exit_trigger] = counts.get(row.exit_trigger, 0) + 1
    return BacktestResult(
        trades=tuple(closed + still_open),
        pnl=realised,
        win_rate=win_rate,
        max_drawdown=_drawdown(equity),
        exit_trigger_distribution=tuple(
            sorted(counts.items(), key=lambda item: item[0].value)
        ),
        benchmark_return=_benchmark(bars),
    )
