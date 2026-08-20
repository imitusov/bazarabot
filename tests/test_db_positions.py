"""Tests for zarabot.db.positions — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
from zarabot.db.positions import (
    PositionStateError,
    adopt,
    close,
    get,
    list_open,
    open,
    set_stop_protection,
    update_lots,
)
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    OrderStatus,
    Side,
    Signal,
    StopProtection,
)

AWARE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
PRICE = Decimal("100.00")
STOP = Decimal("95.00")
TARGET = Decimal("110.00")
KEY = "11111111-1111-4111-8111-111111111111"
EXIT_KEY = "22222222-2222-4222-8222-222222222222"

REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _instrument() -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=AWARE,
    )


def _signal() -> Signal:
    return Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=AWARE,
        reference_price=PRICE,
    )


def _order(**overrides: object) -> OrderRecord:
    fields: dict[str, object] = {
        "key": KEY,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "side": Side.BUY,
        "intent": "ENTRY",
        "lots": 2,
        "status": OrderStatus.FILLED,
        "filled_lots": 2,
        "filled_price": PRICE,
        "commission": Decimal("1.50"),
        "broker_reason": None,
        "created_at": AWARE,
        "settled_at": AWARE,
    }
    fields.update(overrides)
    return OrderRecord(**fields)  # type: ignore[arg-type]


async def _insert_order(db: Path, order: OrderRecord) -> None:
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, commission, created_at, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.key,
                order.ticker,
                order.figi,
                order.side.value,
                order.intent,
                order.lots,
                order.status.value,
                order.filled_lots,
                str(order.filled_price) if order.filled_price is not None else None,
                str(order.commission) if order.commission is not None else None,
                order.created_at.isoformat(),
                order.settled_at.isoformat() if order.settled_at else None,
            ),
        )
        await conn.commit()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
        await conn.commit()
    await _insert_order(path, _order())
    return path


async def test_open_then_list_open_returns_it(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    opened = await list_open()
    assert len(opened) == 1
    assert opened[0].id == position.id
    assert opened[0].ticker == "SBER"
    assert opened[0].stop_protection is StopProtection.LOCAL
    assert opened[0].stop_order_key is None


async def test_close_removes_from_open_and_preserves_history(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(db, exit_order)
    closed = await close(
        position.id,
        ExitTrigger.TAKE_PROFIT,
        Decimal("110.00"),
        AWARE,
        exit_order,
    )
    assert await list_open() == []
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "CLOSED"
    assert stored.exit_trigger is ExitTrigger.TAKE_PROFIT
    assert stored.exit_price == Decimal("110.00")
    assert closed.exit_trigger is ExitTrigger.TAKE_PROFIT


async def test_closing_already_closed_raises(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
    )
    await _insert_order(db, exit_order)
    await close(
        position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, exit_order
    )
    with pytest.raises(PositionStateError):
        await close(
            position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, exit_order
        )


async def test_list_open_on_empty_returns_empty_list(db: Path) -> None:
    result = await list_open()
    assert result == []


async def test_round_trip_prices_are_decimal(db: Path) -> None:
    await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    [position] = await list_open()
    assert position.entry_price == PRICE
    assert position.stop_price == STOP
    assert position.target_price == TARGET
    assert isinstance(position.entry_price, Decimal)
    assert isinstance(position.stop_price, Decimal)


async def test_concurrent_close_exactly_one_success(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("96.00"),
        commission=Decimal("1.00"),
    )
    await _insert_order(db, exit_order)

    async def attempt() -> str:
        try:
            await close(
                position.id,
                ExitTrigger.STOP_LOSS,
                Decimal("96.00"),
                AWARE,
                exit_order,
            )
        except PositionStateError:
            return "error"
        return "ok"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["error", "ok"]
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "CLOSED"


async def test_set_stop_protection_pairing(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(PositionStateError):
        await set_stop_protection(position.id, StopProtection.EXCHANGE, None)
    with pytest.raises(PositionStateError):
        await set_stop_protection(position.id, StopProtection.LOCAL, "stop-key")
    promoted = await set_stop_protection(
        position.id, StopProtection.EXCHANGE, "stop-key"
    )
    assert promoted.stop_protection is StopProtection.EXCHANGE
    demoted = await set_stop_protection(position.id, StopProtection.LOCAL, None)
    assert demoted.stop_order_key is None


async def test_adopt_creates_local_adopted_position(db: Path) -> None:
    position = await adopt(_instrument(), 3, PRICE, AWARE)
    assert position.adopted is True
    assert position.strategy == "ADOPTED"
    assert position.stop_protection is StopProtection.LOCAL
    assert position.lots == 3
    assert position.entry_price == PRICE


async def test_duplicate_open_raises(db: Path) -> None:
    await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(PositionStateError):
        await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)


async def test_close_absent_raises(db: Path) -> None:
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    with pytest.raises(PositionStateError):
        await close(999, ExitTrigger.EXTERNAL, PRICE, AWARE, exit_order)


async def test_update_lots_writes_broker_count(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    updated = await update_lots(position.id, 1)
    assert updated.lots == 1
    stored = await get(position.id)
    assert stored is not None
    assert stored.lots == 1
    assert stored.status == "OPEN"


async def test_update_lots_absent_or_closed_raises(db: Path) -> None:
    with pytest.raises(PositionStateError):
        await update_lots(999, 1)
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    await _insert_order(db, exit_order)
    await close(position.id, ExitTrigger.EXTERNAL, PRICE, AWARE, exit_order)
    with pytest.raises(PositionStateError):
        await update_lots(position.id, 1)


async def test_update_lots_rejects_non_positive(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(PositionStateError):
        await update_lots(position.id, 0)
