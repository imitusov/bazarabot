"""Replay candles through live strategies, sizing, and exits."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from zarabot.clock import trading_days_between
from zarabot.config import Config
from zarabot.lifecycle.exits import evaluate as evaluate_exit
from zarabot.models import (
    BacktestResult,
    Candle,
    ExitTrigger,
    Instrument,
    Position,
    SessionInfo,
    StopProtection,
    TradingCalendar,
)
from zarabot.risk.sizing import size_position

_HUNDRED = Decimal("100")
_TICKER = "SBER"
_ONE = Decimal("1")


def _instrument(now: datetime) -> Instrument:
    return Instrument(
        figi="BACKTEST",
        ticker=_TICKER,
        lot=1,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=now,
    )


def _calendar(candles: list[Candle]) -> TradingCalendar:
    return TradingCalendar(
        sessions=tuple(
            SessionInfo(
                start=candle.timestamp,
                end=candle.timestamp + timedelta(minutes=1),
                is_trading_day=True,
            )
            for candle in candles
        )
    )


def _session(now: datetime) -> SessionInfo:
    return SessionInfo(start=now, end=now + timedelta(minutes=1), is_trading_day=True)


def _fill(price: Decimal, slippage: Decimal, side: str) -> Decimal:
    if side == "BUY":
        return price * (_ONE + slippage)
    return price * (_ONE - slippage)


def _levels(fill: Decimal, config: Config) -> tuple[Decimal, Decimal]:
    stop = fill * (_HUNDRED - config.stop_loss_pct) / _HUNDRED
    target = fill * (_HUNDRED + config.take_profit_pct) / _HUNDRED
    return stop, target


def _realised(position: Position, exit_price: Decimal, commission: Decimal) -> Decimal:
    units = Decimal(position.lots * position.lot_size)
    return (exit_price - position.entry_price) * units - commission - commission


def _drawdown(peaks: list[Decimal]) -> Decimal:
    if not peaks:
        return Decimal("0")
    peak = peaks[0]
    worst = Decimal("0")
    for equity in peaks:
        if equity > peak:
            peak = equity
        if peak == 0:
            continue
        drop = (peak - equity) / peak
        if drop > worst:
            worst = drop
    return worst


def run(
    strategy: object,
    candles: list[Candle],
    config: Config,
    commission: Decimal,
    slippage: Decimal,
) -> BacktestResult:
    """Replay `candles` through the live strategy, sizing, and exit modules."""
    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    if not ordered:
        return BacktestResult(
            trades=(),
            pnl=Decimal("0"),
            win_rate=Decimal("0"),
            max_drawdown=Decimal("0"),
            exit_trigger_distribution=(),
            benchmark_return=None,
        )
    calendar = _calendar(ordered)
    instrument = _instrument(ordered[0].timestamp)
    cash = config.allocated_capital
    position: Position | None = None
    closed: list[Position] = []
    equity: list[Decimal] = [cash]
    next_id = 1
    evaluate = strategy.evaluate
    name = str(getattr(strategy, "name", "strategy"))

    for candle in ordered:
        now = candle.timestamp
        visible = [item for item in ordered if item.timestamp < now]
        if position is not None:
            days = trading_days_between(position.entry_at, now, calendar)
            trigger = evaluate_exit(
                position, candle.close, now, _session(now), days, config
            )
            if trigger is not None:
                exit_price = _fill(candle.close, slippage, "SELL")
                cash += Decimal(position.lots * position.lot_size) * exit_price
                cash -= commission
                pnl = _realised(position, exit_price, commission)
                closed.append(
                    Position(
                        id=position.id,
                        ticker=position.ticker,
                        figi=position.figi,
                        strategy=position.strategy,
                        lots=position.lots,
                        lot_size=position.lot_size,
                        entry_price=position.entry_price,
                        entry_at=position.entry_at,
                        stop_price=position.stop_price,
                        target_price=position.target_price,
                        status="CLOSED",
                        adopted=False,
                        open_order_key=position.open_order_key,
                        close_order_key=f"BT-CLOSE-{position.id}",
                        exit_trigger=trigger,
                        exit_price=exit_price,
                        exit_at=now,
                        realised_pnl=pnl,
                        stop_protection=StopProtection.LOCAL,
                        stop_order_key=None,
                    )
                )
                position = None
                equity.append(cash)
                continue
        if position is None:
            signal = evaluate(_TICKER, visible, now)
            if signal is None:
                continue
            fill = _fill(candle.close, slippage, "BUY")
            lots = size_position(
                fill,
                instrument,
                config.allocated_capital,
                cash,
                config.position_size_pct,
                # No position is open on this branch, so nothing is committed
                # to the portfolio yet and the headroom is the whole allocation.
                Decimal(0),
                config.cash_reserve_pct,
            )
            if lots <= 0:
                continue
            cost = Decimal(lots * instrument.lot) * fill + commission
            if cost > cash:
                continue
            cash -= cost
            stop, target = _levels(fill, config)
            position = Position(
                id=next_id,
                ticker=_TICKER,
                figi=instrument.figi,
                strategy=name,
                lots=lots,
                lot_size=instrument.lot,
                entry_price=fill,
                entry_at=now,
                stop_price=stop,
                target_price=target,
                status="OPEN",
                adopted=False,
                open_order_key=f"BT-OPEN-{next_id}",
                close_order_key=None,
                exit_trigger=None,
                exit_price=None,
                exit_at=None,
                realised_pnl=None,
                stop_protection=StopProtection.LOCAL,
                stop_order_key=None,
            )
            next_id += 1
            equity.append(cash)

    trades = tuple(closed + ([position] if position is not None else []))
    pnl = sum((trade.realised_pnl or Decimal("0") for trade in closed), Decimal("0"))
    wins = sum(1 for trade in closed if (trade.realised_pnl or Decimal("0")) > 0)
    win_rate = Decimal(wins) / Decimal(len(closed)) if closed else Decimal("0")
    counts: dict[ExitTrigger, int] = {}
    for trade in closed:
        if trade.exit_trigger is not None:
            counts[trade.exit_trigger] = counts.get(trade.exit_trigger, 0) + 1
    distribution = tuple(sorted(counts.items(), key=lambda item: item[0].value))
    benchmark = None
    if len(ordered) >= 2 and ordered[0].close != 0:
        benchmark = (ordered[-1].close - ordered[0].close) / ordered[0].close
    return BacktestResult(
        trades=trades,
        pnl=pnl,
        win_rate=win_rate,
        max_drawdown=_drawdown(equity),
        exit_trigger_distribution=distribution,
        benchmark_return=benchmark,
    )
