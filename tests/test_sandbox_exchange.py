"""Tests for sandbox.exchange — written from technical-spec.md §3.2.

The fill model is where a backtest is honest or is not, so these assert the
four rules directly rather than through a whole run.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sandbox.exchange import Commission, Phase, SimulatedExchange
from zarabot.broker.client import (
    InstrumentNotFound,
    OrderNotFound,
    OrderRejected,
    StopOrderRejected,
)
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


def _exchange(bars: object = None, **kwargs: object) -> SimulatedExchange:
    """`bars` is SBER's series, or a whole {ticker: series} mapping."""
    series = bars if isinstance(bars, dict) else {"SBER": bars or []}
    defaults: dict[str, object] = {
        "bars": series,
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


async def test_a_buy_beyond_the_balance_is_rejected() -> None:
    """The broker refuses it and get_max_lots exists to make it avoidable. The
    simulator debited unconditionally, so cash went negative and the next
    get_portfolio raised "cash must not be negative" (#47)."""
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "100", "101", "99", "100")]
    ex = _exchange(bars, cash=Decimal("500"))
    await ex.advance(bars[0].timestamp)
    with pytest.raises(OrderRejected):
        await ex.post_market_order("k1", FIGI, Side.BUY, 100)
    assert ex.cash == Decimal("500"), "cash untouched"
    assert (await ex.get_portfolio()).positions == (), "no holding created"


async def test_the_portfolio_never_goes_negative() -> None:
    """A simulator that funds any order cannot show that the gate and sizing
    keep the bot solvent, which is one of the things a backtest is for."""
    bars = [_bar(n, "100", "101", "99", "100") for n in range(4)]
    ex = _exchange(bars, cash=Decimal("1000"))
    await ex.advance(bars[0].timestamp)
    for n in range(3):
        with contextlib.suppress(OrderRejected):
            await ex.post_market_order(f"k{n}", FIGI, Side.BUY, 5)
        await ex.advance(bars[min(n + 1, 3)].timestamp)
        state = await ex.get_portfolio()
        assert state.cash >= 0


async def test_a_phase_becomes_the_last_price() -> None:
    """The sub-bar marks have to reach the code that values a position."""
    bars = [_bar(0, "100", "110", "90", "105"), _bar(1, "105", "106", "104", "105")]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    assert await ex.get_last_price(FIGI) == Decimal("105"), "the close by default"
    await ex.advance(bars[0].timestamp + timedelta(hours=2), phase=Phase.LOW)
    assert await ex.get_last_price(FIGI) == Decimal("90")
    await ex.advance(bars[0].timestamp + timedelta(hours=4), phase=Phase.HIGH)
    assert await ex.get_last_price(FIGI) == Decimal("110")
    await ex.advance(bars[0].timestamp + timedelta(hours=6), phase=Phase.OPEN)
    assert await ex.get_last_price(FIGI) == Decimal("100")


async def test_a_phase_resolves_per_instrument() -> None:
    """A scalar price passed in by the caller would report one ticker's low as
    every ticker's — and a backtest runs the whole watchlist."""
    sber = _bar(0, "100", "110", "90", "105")
    gazp = Candle(
        timestamp=sber.timestamp,
        open=Decimal("200"),
        high=Decimal("260"),
        low=Decimal("140"),
        close=Decimal("210"),
        volume=1000,
    )
    other = Instrument(
        figi="BBG000GAZP01",
        ticker="GAZP",
        lot=1,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=DAY0,
    )
    ex = _exchange(
        bars={"SBER": [sber], "GAZP": [gazp]},
        instruments={"SBER": _instrument(), "GAZP": other},
    )
    await ex.advance(sber.timestamp, phase=Phase.LOW)
    assert await ex.get_last_price(FIGI) == Decimal("90")
    assert await ex.get_last_price("BBG000GAZP01") == Decimal("140")


async def test_a_stop_is_checked_once_per_bar_not_once_per_mark() -> None:
    """Four cycles must not become four chances to fire."""
    bars = [
        _bar(0, "100", "101", "99", "100"),
        _bar(1, "100", "101", "90", "99"),
    ]
    ex = _exchange(bars)
    await ex.advance(bars[0].timestamp)
    ex.hold(FIGI, 10, Decimal("100"))
    await ex.post_stop_loss("s1", FIGI, 10, Decimal("95"))
    for hours, phase in ((0, Phase.OPEN), (2, Phase.LOW), (4, Phase.HIGH)):
        await ex.advance(bars[1].timestamp + timedelta(hours=hours), phase=phase)
    fills = await ex.get_executed_stop_fills(
        bars[0].timestamp, bars[1].timestamp + timedelta(hours=8)
    )
    assert len(fills) == 1, "one fill, however many marks were walked"


async def test_it_can_refuse_a_stop_order() -> None:
    """The broker can reject a stop, and the degrade path that leaves a position
    LOCAL is only reachable in a backtest if the double can too."""
    bars = [_bar(0, "100", "101", "99", "100"), _bar(1, "100", "101", "99", "100")]
    ex = _exchange(bars, reject_stops=True)
    await ex.advance(bars[0].timestamp)
    with pytest.raises(StopOrderRejected):
        await ex.post_stop_loss("s1", FIGI, 10, Decimal("95"))
    assert await ex.list_stop_orders() == []


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
