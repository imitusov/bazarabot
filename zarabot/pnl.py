"""Realised and unrealised P&L. Commission is never estimated."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from t_tech.invest.schemas import CandleInterval

from zarabot import config
from zarabot.broker.client import get_candles, get_instrument, get_last_price
from zarabot.clock import moscow_date
from zarabot.db.positions import list_closed, list_open
from zarabot.db.snapshots import list_for_period
from zarabot.models import Position
from zarabot.telegram.notifier import alert

_HUNDRED = Decimal("100")

# Moscow dates whose reconstructed baseline has already been alerted.
# `daily_loss_pct` runs every trading cycle, and the contract asks for one
# alert per reconstruction, not one per cycle.
_alerted_reconstruction: set[date] = set()


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


def _units(position: Position) -> Decimal:
    return Decimal(position.lots * position.lot_size)


def realised(position: Position) -> Decimal:
    """Net realised P&L, using the commission the broker actually reported."""
    if position.realised_pnl is not None:
        return position.realised_pnl
    if position.exit_price is None:
        return Decimal(0)
    return (position.exit_price - position.entry_price) * _units(position)


def unrealised(position: Position, price: Decimal) -> Decimal:
    """Mark-to-market against entry for an open position."""
    return (price - position.entry_price) * _units(position)


async def bot_equity() -> Decimal:
    """Allocated capital plus every realised and unrealised result.

    Broker cash and broker equity are never read. They move when the owner pays
    money in or takes it out, and a transfer is not a trading result: reading
    them made a withdrawal look like a loss big enough to halt trading, and let
    a deposit mask a real one (#9).
    """
    total = config.get().allocated_capital
    for position in await list_closed():
        total += realised(position)
    for position in await list_open():
        total += unrealised(position, await get_last_price(position.figi))
    return total


async def _reconstructed_baseline(allocated: Decimal, today: date) -> Decimal:
    """Opening bot equity inferred when the session-open snapshot is missing.

    Deliberately tight: unrealised movement on positions carried overnight is
    attributed to today, so the limit trips earlier rather than later. An early
    halt is recoverable with `/resume`; a late one is not.
    """
    total = allocated
    for position in await list_closed():
        if position.exit_at is not None and moscow_date(position.exit_at) < today:
            total += realised(position)
    return total


async def daily_loss_pct(now: datetime) -> Decimal:
    """Today's loss as a percentage of allocated capital. Positive is a loss.

    The denominator is `ALLOCATED_CAPITAL`, the money actually at risk. Divided
    by account equity, a 5% limit permitted a 10% loss of the allocation on an
    account holding twice it (#9).
    """
    _reject_naive(now)
    today = moscow_date(now)
    allocated = config.get().allocated_capital

    rows = await list_for_period(today, today)
    if rows:
        # Written at the session open by `app.loops`, so a process starting at
        # 14:00 still measures against the morning rather than against whatever
        # equity happened to greet its first call.
        opening = rows[0].opening_equity
    else:
        opening = await _reconstructed_baseline(allocated, today)
        if today not in _alerted_reconstruction:
            _alerted_reconstruction.add(today)
            await alert(
                f"No opening snapshot for {today}. The daily-loss baseline was "
                f"reconstructed as {opening} from realised P&L before today, so "
                "overnight unrealised movement counts against today and the "
                "limit is tighter than usual."
            )

    return (opening - await bot_equity()) / allocated * _HUNDRED


async def benchmark_return(start: date, end: date) -> Decimal | None:
    """Equal-weight buy-and-hold over the watchlist, or `None` if unavailable."""
    cfg = config.get()
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
