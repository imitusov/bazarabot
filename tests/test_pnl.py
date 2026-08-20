"""Tests for zarabot.pnl — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

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
from zarabot.pnl import benchmark_return, daily_loss_pct, realised, unrealised

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER,GAZP",
}


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 2,
        "lot_size": 10,
        "entry_price": Decimal("100"),
        "entry_at": NOW,
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


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    return path


def test_realised_pnl_includes_commission() -> None:
    # Gross 2000 minus commission 5, stored net on the closed row.
    assert realised(_position()) == Decimal("1995")


def test_unrealised_uses_current_price() -> None:
    open_pos = _position(
        status="OPEN",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
    )
    assert unrealised(open_pos, Decimal("105")) == Decimal("100")  # 20 units * 5


async def test_daily_loss_pct_uses_opening_baseline_not_allocated(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await write_daily(
        DailySnapshot(
            trade_date=date(2026, 3, 16),
            opening_equity=Decimal("80000"),
            closing_equity=None,
            cash=Decimal("76000"),
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("0"),
            open_positions=0,
            orders_placed=0,
            benchmark_value=None,
        )
    )

    async def fake_portfolio() -> PortfolioState:
        return PortfolioState(cash=Decimal("76000"), positions=())

    monkeypatch.setattr("zarabot.pnl.get_portfolio", fake_portfolio)
    monkeypatch.setattr("zarabot.clock.moscow_date", lambda now: date(2026, 3, 16))
    pct = await daily_loss_pct(NOW)
    assert pct == Decimal("5")  # (80000-76000)/80000 * 100
    assert pct != Decimal("24")  # would be vs allocated 100000


async def test_empty_portfolio_figures_are_zero(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await write_daily(
        DailySnapshot(
            trade_date=date(2026, 3, 16),
            opening_equity=Decimal("100000"),
            closing_equity=None,
            cash=Decimal("100000"),
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("0"),
            open_positions=0,
            orders_placed=0,
            benchmark_value=None,
        )
    )

    async def fake_portfolio() -> PortfolioState:
        return PortfolioState(cash=Decimal("100000"), positions=())

    monkeypatch.setattr("zarabot.pnl.get_portfolio", fake_portfolio)
    monkeypatch.setattr("zarabot.clock.moscow_date", lambda now: date(2026, 3, 16))
    assert await daily_loss_pct(NOW) == Decimal("0")


async def test_benchmark_unavailable_when_a_price_is_missing(
    db: Path, monkeypatch: pytest.MonkeyPatch
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
    assert await benchmark_return(date(2026, 3, 1), date(2026, 3, 16)) is None
