"""Tests for zarabot.pnl — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.config import get
from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.snapshots import DailySnapshot, write_daily
from zarabot.models import (
    Candle,
    ExitTrigger,
    Instrument,
    PortfolioState,
    Position,
    StopProtection,
)
from zarabot.pnl import (
    benchmark_return,
    bot_equity,
    daily_loss_pct,
    realised,
    unrealised,
)

# 12:00 UTC is 15:00 Moscow, so the Moscow calendar date is unambiguous.
NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
TODAY = date(2026, 3, 16)
EARLIER_TODAY = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
BEFORE_TODAY = datetime(2026, 3, 13, 12, 0, tzinfo=UTC)
ALLOCATED = Decimal("100000")

REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER,GAZP",
}


def _closed_position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 2,
        "lot_size": 10,
        "entry_price": Decimal("100"),
        "entry_at": BEFORE_TODAY,
        "stop_price": Decimal("95"),
        "target_price": Decimal("110"),
        "status": "CLOSED",
        "adopted": False,
        "open_order_key": "open-k",
        "close_order_key": "close-k",
        "exit_trigger": ExitTrigger.TAKE_PROFIT,
        "exit_price": Decimal("110"),
        "exit_at": NOW,
        "realised_pnl": Decimal("1995"),  # 2*10*(110-100) - 5 commission
        "stop_protection": StopProtection.LOCAL,
        "stop_order_key": None,
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


def _open_position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 9,
        "ticker": "GAZP",
        "figi": "BBG000000002",
        "strategy": "momentum",
        "lots": 2,
        "lot_size": 10,
        "entry_price": Decimal("100"),
        "entry_at": BEFORE_TODAY,
        "stop_price": Decimal("95"),
        "target_price": Decimal("110"),
        "status": "OPEN",
        "adopted": False,
        "open_order_key": "open-9",
        "close_order_key": None,
        "exit_trigger": None,
        "exit_price": None,
        "exit_at": None,
        "realised_pnl": None,
        "stop_protection": StopProtection.LOCAL,
        "stop_order_key": None,
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


class Ctx:
    """Mutable stand-ins for everything `pnl` reads."""

    def __init__(self) -> None:
        self.closed: list[Position] = []
        self.open: list[Position] = []
        self.prices: dict[str, Decimal] = {}
        self.alerts: list[str] = []
        # Broker equity. `pnl` must never look at it; the spy proves it.
        self.broker_cash = Decimal("100000")
        self.portfolio_calls = 0


@pytest.fixture
async def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ctx]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    get.cache_clear()

    state = Ctx()

    async def _list_closed() -> list[Position]:
        return list(state.closed)

    async def _list_open() -> list[Position]:
        return list(state.open)

    async def _price(figi: str) -> Decimal:
        return state.prices[figi]

    async def _alert(text: str, urgent: bool = False) -> None:
        state.alerts.append(text)

    async def _portfolio() -> PortfolioState:
        state.portfolio_calls += 1
        return PortfolioState(cash=state.broker_cash, positions=())

    monkeypatch.setattr("zarabot.pnl.list_closed", _list_closed)
    monkeypatch.setattr("zarabot.pnl.list_open", _list_open)
    monkeypatch.setattr("zarabot.pnl.get_last_price", _price)
    monkeypatch.setattr("zarabot.pnl.alert", _alert)
    # Both spellings: if `pnl` ever reimports the portfolio call, the spy
    # catches it either way, and the test asserts it was never invoked.
    monkeypatch.setattr("zarabot.broker.client.get_portfolio", _portfolio)
    monkeypatch.setattr("zarabot.pnl.get_portfolio", _portfolio, raising=False)
    monkeypatch.setattr("zarabot.pnl._alerted_reconstruction", set())

    # Opened last: a patching failure above must not leak the process
    # connection into the next test (`db.connection` owns exactly one).
    conn = await connect(str(path))
    await apply(conn)

    try:
        yield state
    finally:
        await disconnect()
        get.cache_clear()


async def _write_opening(equity: Decimal) -> None:
    await write_daily(
        DailySnapshot(
            trade_date=TODAY,
            opening_equity=equity,
            closing_equity=None,
            cash=equity,
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("0"),
            open_positions=0,
            orders_placed=0,
            benchmark_value=None,
        )
    )


# --- realised / unrealised -------------------------------------------------


def test_realised_pnl_includes_commission() -> None:
    # Gross 2000 minus commission 5, stored net on the closed row.
    assert realised(_closed_position()) == Decimal("1995")


def test_unrealised_uses_current_price() -> None:
    assert unrealised(_open_position(), Decimal("105")) == Decimal("100")


# --- bot_equity ------------------------------------------------------------


async def test_bot_equity_is_allocated_plus_realised_plus_unrealised(
    ctx: Ctx,
) -> None:
    ctx.closed = [_closed_position()]  # +1995
    ctx.open = [_open_position()]  # 20 units, entry 100
    ctx.prices["BBG000000002"] = Decimal("105")  # +100

    assert await bot_equity() == ALLOCATED + Decimal("1995") + Decimal("100")
    assert ctx.portfolio_calls == 0


async def test_bot_equity_is_unchanged_by_a_deposit(ctx: Ctx) -> None:
    ctx.closed = [_closed_position()]
    ctx.open = [_open_position()]
    ctx.prices["BBG000000002"] = Decimal("105")

    before = await bot_equity()
    ctx.broker_cash += Decimal("500000")  # the owner pays money in
    after = await bot_equity()

    assert after == before
    assert ctx.portfolio_calls == 0


# --- daily_loss_pct --------------------------------------------------------


async def test_daily_loss_pct_divides_by_allocated_capital_not_equity(
    ctx: Ctx,
) -> None:
    # Bot equity at the open was 120000 — allocated 100000 plus 20000 banked
    # on earlier days. Today's loss is 6000 roubles.
    await _write_opening(Decimal("120000"))
    ctx.closed = [
        _closed_position(exit_at=BEFORE_TODAY, realised_pnl=Decimal("20000")),
        _closed_position(
            id=2,
            open_order_key="open-2",
            close_order_key="close-2",
            exit_at=EARLIER_TODAY,
            exit_trigger=ExitTrigger.STOP_LOSS,
            exit_price=Decimal("70"),
            realised_pnl=Decimal("-6000"),
        ),
    ]

    ctx.broker_cash = ALLOCATED
    first = await daily_loss_pct(NOW)
    # The very same rouble loss, on an account holding twice the allocation.
    ctx.broker_cash = ALLOCATED * 2
    second = await daily_loss_pct(NOW)

    assert first == Decimal("6")  # 6000 / 100000 * 100
    assert first != Decimal("5")  # 6000 / 120000 * 100 — the old denominator
    assert second == first
    assert ctx.portfolio_calls == 0


async def test_withdrawal_between_two_calls_does_not_change_the_percentage(
    ctx: Ctx,
) -> None:
    await _write_opening(ALLOCATED)
    ctx.closed = [
        _closed_position(
            exit_at=EARLIER_TODAY,
            exit_trigger=ExitTrigger.STOP_LOSS,
            exit_price=Decimal("50"),
            realised_pnl=Decimal("-4000"),
        )
    ]

    before = await daily_loss_pct(NOW)
    ctx.broker_cash -= Decimal("50000")  # the owner takes money out
    after = await daily_loss_pct(NOW)

    assert before == Decimal("4")
    assert after == before
    assert ctx.portfolio_calls == 0


async def test_missing_snapshot_reconstructs_baseline_and_alerts_once(
    ctx: Ctx,
) -> None:
    # No daily_snapshots row: the process started after the session opened.
    ctx.closed = [
        # Closed on an earlier day — part of the reconstructed baseline.
        _closed_position(exit_at=BEFORE_TODAY, realised_pnl=Decimal("-2000")),
        # Closed today — a loss that must land inside today's percentage.
        _closed_position(
            id=2,
            open_order_key="open-2",
            close_order_key="close-2",
            exit_at=EARLIER_TODAY,
            exit_trigger=ExitTrigger.STOP_LOSS,
            exit_price=Decimal("50"),
            realised_pnl=Decimal("-1000"),
        ),
    ]
    ctx.open = [_open_position()]
    ctx.prices["BBG000000002"] = Decimal("97.5")  # 20 units * -2.5 = -500

    # Baseline 100000 - 2000 = 98000; now 100000 - 3000 - 500 = 96500.
    assert await daily_loss_pct(NOW) == Decimal("1.5")
    assert len(ctx.alerts) == 1
    assert "reconstruct" in ctx.alerts[0].lower()

    # The same day again adds no alert noise.
    assert await daily_loss_pct(NOW) == Decimal("1.5")
    assert len(ctx.alerts) == 1


async def test_empty_portfolio_figures_are_zero(ctx: Ctx) -> None:
    await _write_opening(ALLOCATED)

    equity = await bot_equity()
    loss = await daily_loss_pct(NOW)

    assert equity == ALLOCATED
    assert loss == Decimal("0")
    assert isinstance(loss, Decimal)


async def test_daily_loss_pct_rejects_a_naive_datetime(ctx: Ctx) -> None:
    with pytest.raises(ValueError):
        await daily_loss_pct(datetime(2026, 3, 16, 12, 0))  # noqa: DTZ001


# --- benchmark_return ------------------------------------------------------


async def test_benchmark_unavailable_when_a_price_is_missing(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def get_instrument(ticker: str) -> Instrument:
        return Instrument(
            figi=f"FIGI-{ticker}",
            ticker=ticker,
            lot=10,
            min_price_increment=Decimal("0.01"),
            currency="RUB",
            trading_status="NORMAL_TRADING",
            refreshed_at=NOW,
        )

    async def get_candles(
        figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        if "GAZP" in figi:
            return []
        price = Decimal("100")
        return [
            Candle(
                timestamp=since,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1,
            ),
            Candle(
                timestamp=until,
                open=price,
                high=price,
                low=price,
                close=Decimal("110"),
                volume=1,
            ),
        ]

    monkeypatch.setattr("zarabot.pnl.get_instrument", get_instrument)
    monkeypatch.setattr("zarabot.pnl.get_candles", get_candles)
    assert await benchmark_return(date(2026, 3, 1), TODAY) is None
