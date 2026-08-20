"""Tests for zarabot.broker.reconcile — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest
from zarabot.broker.reconcile import reconcile

from zarabot.db.migrations import apply
from zarabot.db.positions import get, list_open
from zarabot.db.positions import open as open_position
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    OrderStatus,
    PortfolioState,
    Position,
    Side,
    Signal,
    StopOrderRecord,
    StopProtection,
)

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
PRICE = Decimal("100.00")
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _instrument(ticker: str = "SBER", figi: str = "BBG000000001") -> Instrument:
    return Instrument(
        figi=figi,
        ticker=ticker,
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


def _broker_position(
    ticker: str = "SBER",
    figi: str = "BBG000000001",
    lots: int = 2,
    price: Decimal = PRICE,
) -> Position:
    return Position(
        id=0,
        ticker=ticker,
        figi=figi,
        strategy="ADOPTED",
        lots=lots,
        lot_size=10,
        entry_price=price,
        entry_at=NOW,
        stop_price=price * Decimal("95") / Decimal("100"),
        target_price=price * Decimal("110") / Decimal("100"),
        status="OPEN",
        adopted=True,
        open_order_key=f"BROKER-{figi}",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )


class _Broker:
    def __init__(self) -> None:
        self.holdings: tuple[Position, ...] = ()
        self.stops: list[StopOrderRecord] = []
        self.last_price = PRICE
        self.calls: list[str] = []

    async def get_portfolio(self) -> PortfolioState:
        self.calls.append("get_portfolio")
        return PortfolioState(cash=Decimal("100000"), positions=self.holdings)

    async def get_last_price(self, figi: str) -> Decimal:
        self.calls.append("get_last_price")
        return self.last_price

    async def get_instrument(self, ticker: str) -> Instrument:
        self.calls.append("get_instrument")
        return _instrument(ticker=ticker)

    async def list_stop_orders(self) -> list[StopOrderRecord]:
        self.calls.append("list_stop_orders")
        return list(self.stops)

    async def post_market_order(self, *args: object, **kwargs: object) -> None:
        self.calls.append("post_market_order")
        raise AssertionError("reconcile must not place orders")

    async def post_stop_loss(self, *args: object, **kwargs: object) -> None:
        self.calls.append("post_stop_loss")
        raise AssertionError("reconcile must not place stop orders")

    async def cancel_stop_order(self, *args: object, **kwargs: object) -> None:
        self.calls.append("cancel_stop_order")
        raise AssertionError("reconcile must not cancel stop orders")


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Broker:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, created_at, settled_at
            ) VALUES (
                'order-1', 'SBER', 'BBG000000001', 'BUY', 'ENTRY', 2,
                'FILLED', 2, '100.00', ?, ?
            )
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        await conn.commit()
    broker = _Broker()
    module = "zarabot.broker.reconcile"
    monkeypatch.setattr(f"{module}.get_portfolio", broker.get_portfolio)
    monkeypatch.setattr(f"{module}.get_last_price", broker.get_last_price)
    monkeypatch.setattr(f"{module}.get_instrument", broker.get_instrument)
    monkeypatch.setattr(f"{module}.list_stop_orders", broker.list_stop_orders)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    counter = {"n": 0}

    def _uuid() -> UUID:
        counter["n"] += 1
        return UUID(f"aaaaaaaa-aaaa-4aaa-8aaa-{counter['n']:012d}")

    monkeypatch.setattr(f"{module}.uuid4", _uuid)
    return broker


async def _open_local() -> Position:
    signal = Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=NOW,
        reference_price=PRICE,
    )
    order = OrderRecord(
        key="order-1",
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=PRICE,
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    return await open_position(
        signal, order, _instrument(), Decimal("95"), Decimal("110"), NOW
    )


async def test_agreement_produces_no_adjustments_and_no_alert(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    with caplog.at_level(logging.ERROR):
        report = await reconcile(NOW)
    assert report.adjustments == ()
    assert not caplog.records
    assert "post_market_order" not in env.calls
    assert "cancel_stop_order" not in env.calls


async def test_local_open_absent_at_broker_is_closed_externally(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    position = await _open_local()
    env.holdings = ()
    env.last_price = Decimal("123.45")
    with caplog.at_level(logging.ERROR):
        report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "CLOSED_EXTERNALLY" in types
    closed = next(
        item for item in report.adjustments if item["type"] == "CLOSED_EXTERNALLY"
    )
    assert closed["ticker"] == "SBER"
    assert closed["position_id"] == position.id
    assert closed["last_price"] == "123.45"
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "CLOSED"
    assert stored.exit_trigger is ExitTrigger.EXTERNAL
    assert await list_open() == []
    assert caplog.records
    assert "post_market_order" not in env.calls


async def test_broker_holding_absent_locally_is_adopted(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    with caplog.at_level(logging.ERROR):
        report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "ADOPTED" in types
    adopted = next(item for item in report.adjustments if item["type"] == "ADOPTED")
    assert adopted["ticker"] == "SBER"
    assert adopted["lots"] == 3
    assert adopted["average_price"] == "123.45"
    opened = await list_open()
    assert len(opened) == 1
    assert opened[0].adopted is True
    assert opened[0].strategy == "ADOPTED"
    assert opened[0].entry_price == Decimal("123.45")
    assert caplog.records


async def test_lot_mismatch_writes_broker_count(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    position = await _open_local()
    env.holdings = (_broker_position(lots=1),)
    with caplog.at_level(logging.ERROR):
        report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "LOTS_ADJUSTED" in types
    adjusted = next(
        item for item in report.adjustments if item["type"] == "LOTS_ADJUSTED"
    )
    assert adjusted["position_id"] == position.id
    assert adjusted["from"] == 2
    assert adjusted["to"] == 1
    stored = await get(position.id)
    assert stored is not None
    assert stored.lots == 1
    assert stored.status == "OPEN"
    assert caplog.records


async def test_reconcile_is_idempotent(env: _Broker) -> None:
    await _open_local()
    env.holdings = ()
    env.last_price = Decimal("123.45")
    first = await reconcile(NOW)
    assert first.adjustments
    second = await reconcile(NOW)
    assert second.adjustments == ()
    assert await list_open() == []
