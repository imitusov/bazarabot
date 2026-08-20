"""Realised and unrealised P&L. Commission is never estimated."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from t_tech.invest.schemas import CandleInterval

from zarabot.broker.client import (
    get_candles,
    get_instrument,
    get_last_price,
    get_portfolio,
)
from zarabot.clock import moscow_date
from zarabot.config import load
from zarabot.db.snapshots import DailySnapshot, list_for_period, write_daily
from zarabot.models import Position

_HUNDRED = Decimal("100")


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


def _units(position: Position) -> Decimal:
    return Decimal(position.lots * position.lot_size)


def realised(position: Position) -> Decimal:
    if position.realised_pnl is not None:
        return position.realised_pnl
    if position.exit_price is None:
        return Decimal(0)
    return (position.exit_price - position.entry_price) * _units(position)


def unrealised(position: Position, price: Decimal) -> Decimal:
    return (price - position.entry_price) * _units(position)


async def _equity() -> Decimal:
    portfolio = await get_portfolio()
    total = portfolio.cash
    for position in portfolio.positions:
        price = await get_last_price(position.figi)
        total += price * _units(position)
    return total


async def daily_loss_pct(now: datetime) -> Decimal:
    _reject_naive(now)
    day = moscow_date(now)
    equity = await _equity()
    rows = await list_for_period(day, day)
    if not rows:
        await write_daily(
            DailySnapshot(
                trade_date=day,
                opening_equity=equity,
                closing_equity=None,
                cash=equity,
                realised_pnl=Decimal(0),
                unrealised_pnl=Decimal(0),
                open_positions=0,
                orders_placed=0,
                benchmark_value=None,
            )
        )
        return Decimal(0)
    opening = rows[0].opening_equity
    if opening == 0:
        return Decimal(0)
    return (opening - equity) / opening * _HUNDRED


async def benchmark_return(start: date, end: date) -> Decimal | None:
    cfg = load()
    returns: list[Decimal] = []
    since = datetime(start.year, start.month, start.day, tzinfo=UTC)
    until = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)
    for ticker in cfg.watchlist:
        try:
            instrument = await get_instrument(ticker)
            candles = await get_candles(
                instrument.figi,
                CandleInterval.CANDLE_INTERVAL_DAY,
                since,
                until,
            )
        except Exception:
            return None
        if len(candles) < 2 or candles[0].close == 0:
            return None
        first = candles[0].close
        last = candles[-1].close
        returns.append((last - first) / first)
    if not returns:
        return None
    return sum(returns, Decimal(0)) / Decimal(len(returns))
