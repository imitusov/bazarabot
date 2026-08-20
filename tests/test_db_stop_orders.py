"""Tests for zarabot.db.stop_orders — derived from the module contract."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
from zarabot.db.orders import DuplicateOrderError, OrderStateError
from zarabot.db.positions import open as open_position
from zarabot.db.stop_orders import (
    activate,
    active_for_position,
    list_active,
    record_placing,
    settle,
)
from zarabot.models import (
    Instrument,
    OrderRecord,
    OrderStatus,
    Side,
    Signal,
    StopOrderStatus,
)

AWARE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
STOP = Decimal("95.00")

REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
            ) VALUES ('order-1', 'SBER', 'BBG000000001', 'BUY', 'ENTRY', 2,
                      'FILLED', 2, '100.00', ?, ?)
            """,
            (AWARE.isoformat(), AWARE.isoformat()),
        )
        await conn.commit()
    return path


async def _position_id() -> int:
    signal = Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=AWARE,
        reference_price=Decimal("100.00"),
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
        filled_price=Decimal("100.00"),
        commission=None,
        broker_reason=None,
        created_at=AWARE,
        settled_at=AWARE,
    )
    instrument = Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=AWARE,
    )
    position = await open_position(
        signal, order, instrument, STOP, Decimal("110.00"), AWARE
    )
    return position.id


async def test_record_placing_then_activate(db: Path) -> None:
    position_id = await _position_id()
    placing = await record_placing(KEY, position_id, "SBER", 2, STOP)
    assert placing.status is StopOrderStatus.PLACING
    standing = await activate(KEY, "broker-stop-1")
    assert standing.status is StopOrderStatus.ACTIVE
    assert standing.stop_order_id == "broker-stop-1"
    found = await active_for_position(position_id)
    assert found is not None
    assert found.key == KEY
    active = await list_active()
    assert len(active) == 1
    assert active[0].key == KEY


async def test_duplicate_key_raises(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    with pytest.raises(DuplicateOrderError):
        await record_placing(KEY, position_id, "SBER", 2, STOP)


async def test_terminal_stop_cannot_resettle(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    await activate(KEY, "broker-stop-1")
    await settle(KEY, StopOrderStatus.CANCELLED, AWARE)
    with pytest.raises(OrderStateError):
        await settle(KEY, StopOrderStatus.EXECUTED, AWARE)
    assert await active_for_position(position_id) is None
    assert await list_active() == []


async def test_active_for_position_none_when_absent(db: Path) -> None:
    assert await active_for_position(999) is None
