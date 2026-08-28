"""Tests for sandbox.exchange — written from technical-spec.md §3.2.

The fill model is where a backtest is honest or is not, so these assert the
four rules directly rather than through a whole run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sandbox.exchange import Commission, SimulatedExchange
from zarabot.broker.client import InstrumentNotFound, OrderNotFound
from zarabot.models import Candle, ExitTrigger, Instrument, OrderStatus, Side

DAY0 = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
FIGI = "BBG000SBER01"


def _bar(
    day: int,
    open_: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    return Candle(
        timestamp=DAY0 + timedelta(days=day),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
    )


def _instrument() -> Instrument:
    return Instrument(
        figi=FIGI,
        ticker="SBER",
        lot=1,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=DAY0,
    )


def _exchange(bars: list[Candle], **kwargs: object) -> SimulatedExchange:
    defaults: dict[str, object] = {
        "bars": {"SBER": bars},
        "instruments": {"SBER": _instrument()},
        "cash": Decimal("100000"),
        "slippage": Decimal("0"),
        "commission": Commission(pct=Decimal("0.05"), minimum=Decimal("0.01")),
    }
    defaults.update(kwargs)
    return SimulatedExchange(**defaults)  # type: ignore[arg-type]


async def test_market_buy_is_priced_at_the_next_bars_open() -> None:
    """Decide at a close, take the next open's price. Pricing at the decision
    bar is look-ahead: that price was not knowable when the order was placed."""
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "105", "106", "104", "105")]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    order = await ex.post_market_order("k1", FIGI, Side.BUY, 10)
    assert order.status is OrderStatus.FILLED
    assert order.filled_price == Decimal("105"), "the next bar's open"


async def test_the_fill_is_returned_on_the_submitting_call() -> None:
    """A deferred fill sends every entry through open_position's crash-recovery
    path and trips the outage counter — a live/backtest divergence on the
    ordinary path, which is what this rebuild exists to remove (#12)."""
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "105", "106", "104", "105")]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    order = await ex.post_market_order("k1", FIGI, Side.BUY, 10)
    assert order.filled_lots == 10
    assert order.settled_at is not None


async def test_an_order_on_the_last_bar_never_fills() -> None:
    """History ran out. Inventing a price for it would be the look-ahead the
    next-open rule exists to prevent."""
    bars = [_bar(0, "100", "101", "99", "100")]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    order = await ex.post_market_order("k1", FIGI, Side.BUY, 10)
    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_price is None


async def test_stop_fires_on_the_bar_low_not_the_close() -> None:
    """A day that traded 8% down intraday and closed at -1% never triggered a
    5% stop, while the exchange stop fires on the intraday print (#12)."""
    bars = [
        _bar(0, "100", "101", "99", "100"),
        _bar(1, "100", "101", "92", "99"),  # low 92, close 99
    ]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    ex.hold(FIGI, 10, Decimal("100"))
    await ex.post_stop_loss("s1", FIGI, 10, Decimal("95"))
    await ex.advance(bars[1].timestamp)
    fills = await ex.get_executed_stop_fills(bars[0].timestamp, bars[1].timestamp)
    assert fills, "the low crossed the stop"
    fill = next(iter(fills.values()))
    assert fill.filled_price == Decimal("95"), "filled at the stop, not the close"


async def test_take_profit_fires_on_the_bar_high() -> None:
    bars = [
        _bar(0, "100", "101", "99", "100"),
        _bar(1, "100", "112", "99", "101"),  # high 112, close 101
    ]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    ex.hold(FIGI, 10, Decimal("100"))
    await ex.advance(bars[1].timestamp)
    assert ex.touched(FIGI, Decimal("110"), ExitTrigger.TAKE_PROFIT) is True


async def test_a_gapped_open_fills_worse_than_the_stop() -> None:
    """The exchange cannot fill at a price the market never traded at."""
    bars = [
        _bar(0, "100", "101", "99", "100"),
        _bar(1, "90", "91", "88", "89"),  # opens below the 95 stop
    ]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    ex.hold(FIGI, 10, Decimal("100"))
    await ex.post_stop_loss("s1", FIGI, 10, Decimal("95"))
    await ex.advance(bars[1].timestamp)
    fills = await ex.get_executed_stop_fills(bars[0].timestamp, bars[1].timestamp)
    fill = next(iter(fills.values()))
    assert fill.filled_price == Decimal("90"), "the gapped open, not the stop"


async def test_a_bar_touching_both_books_the_stop() -> None:
    """Daily bars cannot say which came first; the pessimistic reading is the
    only one that cannot flatter the result."""
    bars = [
        _bar(0, "100", "101", "99", "100"),
        _bar(1, "100", "115", "90", "108"),  # touches both 95 and 110
    ]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    ex.hold(FIGI, 10, Decimal("100"))
    await ex.post_stop_loss("s1", FIGI, 10, Decimal("95"))
    await ex.advance(bars[1].timestamp)
    fills = await ex.get_executed_stop_fills(bars[0].timestamp, bars[1].timestamp)
    assert fills, "the stop wins the tie"
    assert ex.touched(FIGI, Decimal("110"), ExitTrigger.TAKE_PROFIT) is True


async def test_commission_is_a_percentage_with_a_minimum() -> None:
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "100", "101", "99", "100")]
    ex = _exchange(
        bars, commission=Commission(pct=Decimal("0.05"), minimum=Decimal("20"))
    )
    await ex.advance(bars[0].timestamp)
    settled = await ex.post_market_order("k1", FIGI, Side.BUY, 10)
    # Turnover 1000 x 0.05% = 0.50, below the 20 minimum.
    assert settled.commission == Decimal("20")

    ex2 = _exchange(
        bars, commission=Commission(pct=Decimal("1"), minimum=Decimal("0.01"))
    )
    await ex2.advance(bars[0].timestamp)
    settled2 = await ex2.post_market_order("k2", FIGI, Side.BUY, 10)
    # Turnover 1000 x 1% = 10, above the minimum.
    assert settled2.commission == Decimal("10")


async def test_it_raises_what_the_real_client_raises() -> None:
    """A simulator that cannot fail the way the broker fails cannot exercise
    the code that handles failure."""
    ex = _exchange([_bar(0, "100", "101", "99", "100")])
    with pytest.raises(InstrumentNotFound):
        await ex.get_instrument("NOPE")
    with pytest.raises(OrderNotFound):
        await ex.get_order_state("never-submitted")


async def test_portfolio_reports_cash_and_holdings() -> None:
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "100", "101", "99", "100")]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    await ex.post_market_order("k1", FIGI, Side.BUY, 10)
    state = await ex.get_portfolio()
    assert state.cash < Decimal("100000"), "cash spent on the fill"
    assert [p.ticker for p in state.positions] == ["SBER"]
    assert state.positions[0].lots == 10


async def test_candles_never_include_the_future() -> None:
    bars = [_bar(n, "100", "101", "99", "100") for n in range(5)]
    ex = _exchange(bars)
    await ex.advance(bars[2].timestamp)
    visible = await ex.get_candles(FIGI, None, bars[0].timestamp, bars[4].timestamp)
    assert [c.timestamp for c in visible] == [b.timestamp for b in bars[:3]]
