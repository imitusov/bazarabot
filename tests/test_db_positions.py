"""Tests for zarabot.db.positions — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.db.orders import record_commission
from zarabot.db.positions import (
    PositionEvent,
    PositionStateError,
    adopt,
    close,
    get,
    list_closed,
    list_events,
    list_open,
    open,
    recompute_realised,
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
ADOPTED_KEY = "ADOPTED-BBG000000001"

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


async def _insert_order(order: OrderRecord) -> None:
    conn = shared()
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
    monkeypatch.setattr("zarabot.clock.now", lambda: AWARE)
    monkeypatch.setattr("zarabot.db.positions.now", lambda: AWARE, raising=False)
    conn = await connect(str(path))
    await apply(conn)
    await _insert_order(_order())
    await _insert_order(_order(key=ADOPTED_KEY, commission=None))
    try:
        yield path
    finally:
        await disconnect()


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
    await _insert_order(exit_order)
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
    history = await list_closed()
    assert len(history) == 1
    assert history[0].id == position.id
    assert history[0].exit_trigger is ExitTrigger.TAKE_PROFIT


async def test_closing_already_closed_raises(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
    )
    await _insert_order(exit_order)
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
    assert result is not None


async def test_round_trip_prices_are_decimal(db: Path) -> None:
    await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    [position] = await list_open()
    assert position.entry_price == PRICE
    assert position.stop_price == STOP
    assert position.target_price == TARGET
    assert isinstance(position.entry_price, Decimal)
    assert isinstance(position.stop_price, Decimal)
    assert isinstance(position.target_price, Decimal)


async def test_concurrent_close_exactly_one_success(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("96.00"),
        commission=Decimal("1.00"),
    )
    await _insert_order(exit_order)

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


async def test_mutations_write_one_event_each(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    opened_events = await list_events(position.id)
    assert len(opened_events) == 1
    assert opened_events[0].event == "OPENED"
    assert opened_events[0].position_id == position.id
    assert opened_events[0].occurred_at == AWARE
    assert opened_events[0].occurred_at.tzinfo is not None

    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-key")
    await update_lots(position.id, 1)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(exit_order)
    await close(
        position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, exit_order
    )
    await record_commission(KEY, Decimal("3.00"))
    await recompute_realised(position.id)

    events = await list_events(position.id)
    kinds = [item.event for item in events]
    assert kinds == [
        "OPENED",
        "STOP_PROTECTION_CHANGED",
        "LOTS_ADJUSTED",
        "CLOSED",
        "REALISED_RECOMPUTED",
    ]
    assert all(isinstance(item, PositionEvent) for item in events)
    assert all(item.position_id == position.id for item in events)

    adopted = await adopt(_instrument(), 3, PRICE, AWARE)
    adopted_events = await list_events(adopted.id)
    assert len(adopted_events) == 1
    assert adopted_events[0].event == "ADOPTED"


async def test_close_rollback_leaves_no_event(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(exit_order)
    conn = shared()
    original = conn.execute

    async def failing(sql: str, parameters: object = ()) -> aiosqlite.Cursor:
        if "position_events" in sql and "CLOSED" in str(parameters):
            raise RuntimeError("injected event failure")
        return await original(sql, parameters)

    conn.execute = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected event failure"):
        await close(
            position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, exit_order
        )
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "OPEN"
    assert [item.event for item in await list_events(position.id)] == ["OPENED"]


async def test_list_events_reconstructs_stop_ownership_history(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-a")
    await set_stop_protection(position.id, StopProtection.LOCAL, None)
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-b")
    events = await list_events(position.id)
    assert [item.event for item in events] == [
        "OPENED",
        "STOP_PROTECTION_CHANGED",
        "STOP_PROTECTION_CHANGED",
        "STOP_PROTECTION_CHANGED",
    ]
    details = [json.loads(item.detail) for item in events[1:]]
    assert details[0]["previous_protection"] == "LOCAL"
    assert details[0]["new_protection"] == "EXCHANGE"
    assert details[0]["new_stop_order_key"] == "stop-a"
    assert details[1]["previous_protection"] == "EXCHANGE"
    assert details[1]["new_protection"] == "LOCAL"
    assert details[1]["new_stop_order_key"] is None
    assert details[2]["new_stop_order_key"] == "stop-b"
    assert await list_events(999_999) == []


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.db.positions as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    await list_open()
    await get(position.id)
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-key")
    await update_lots(position.id, 1)
    await list_events(position.id)
    await list_closed()
    assert calls == []


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
        await close(999, ExitTrigger.TAKE_PROFIT, PRICE, AWARE, exit_order)


async def test_update_lots_writes_broker_count(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    updated = await update_lots(position.id, 1)
    assert updated.lots == 1
    stored = await get(position.id)
    assert stored is not None
    assert stored.lots == 1
    assert stored.status == "OPEN"
    assert stored.entry_price == PRICE
    assert stored.stop_price == STOP
    assert stored.target_price == TARGET


async def test_update_lots_absent_or_closed_raises(db: Path) -> None:
    with pytest.raises(PositionStateError):
        await update_lots(999, 1)
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    await _insert_order(exit_order)
    await close(position.id, ExitTrigger.TAKE_PROFIT, PRICE, AWARE, exit_order)
    with pytest.raises(PositionStateError):
        await update_lots(position.id, 1)


async def test_update_lots_rejects_non_positive(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(PositionStateError):
        await update_lots(position.id, 0)


async def test_list_closed_returns_closed_newest_first(db: Path) -> None:
    assert await list_closed() == []
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    assert await list_closed() == []
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    await _insert_order(exit_order)
    closed = await close(
        position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, exit_order
    )
    found = await list_closed()
    assert len(found) == 1
    assert found[0].id == closed.id
    assert found[0].status == "CLOSED"
    assert found[0].exit_trigger is ExitTrigger.TAKE_PROFIT
    assert await list_open() == []


async def test_close_nets_commission_on_both_legs(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(exit_order)
    closed = await close(
        position.id,
        ExitTrigger.TAKE_PROFIT,
        Decimal("110.00"),
        AWARE,
        exit_order,
    )
    # Gross 200 minus entry 1.50 minus exit 1.50.
    assert closed.realised_pnl == Decimal("197.00")
    assert closed.stop_protection is StopProtection.LOCAL
    assert closed.stop_order_key is None


async def test_external_close_requires_no_order(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    with pytest.raises(ValueError):
        await close(position.id, ExitTrigger.EXTERNAL, PRICE, AWARE, exit_order)
    closed = await close(position.id, ExitTrigger.EXTERNAL, PRICE, AWARE, None)
    assert closed.exit_trigger is ExitTrigger.EXTERNAL
    assert closed.close_order_key is None
    assert closed.status == "CLOSED"


async def test_external_close_nets_and_stores_its_commission(db: Path) -> None:
    """An EXTERNAL close has no closing order row, so its fee had nowhere to
    live and every such position overstated its result by it (#11)."""
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    closed = await close(
        position.id,
        ExitTrigger.EXTERNAL,
        Decimal("110.00"),
        AWARE,
        None,
        Decimal("2.50"),
    )
    # Gross 100 minus entry 1.50 minus the resolved exit fee 2.50.
    assert closed.realised_pnl == Decimal("96.00")
    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT exit_commission FROM positions WHERE id = ?", (position.id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert Decimal(str(row["exit_commission"])) == Decimal("2.50")


async def test_exit_commission_is_refused_where_an_order_carries_it(
    db: Path,
) -> None:
    """A second source for a number the order row already holds is a way for
    the two to disagree."""
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(key=EXIT_KEY, side=Side.SELL, intent="EXIT")
    with pytest.raises(ValueError):
        await close(
            position.id,
            ExitTrigger.TAKE_PROFIT,
            Decimal("110.00"),
            AWARE,
            exit_order,
            Decimal("2.50"),
        )


async def test_recompute_preserves_an_external_exit_commission(db: Path) -> None:
    """The backfill must not undo a fee the operations feed already resolved."""
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    closed = await close(
        position.id,
        ExitTrigger.EXTERNAL,
        Decimal("110.00"),
        AWARE,
        None,
        Decimal("2.50"),
    )
    recomputed = await recompute_realised(position.id)
    assert recomputed.realised_pnl == closed.realised_pnl


async def test_non_external_close_requires_an_order(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(ValueError):
        await close(
            position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), AWARE, None
        )


async def test_adopt_uses_configured_stop_and_target(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOP_LOSS_PCT", "3")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "12")
    position = await adopt(_instrument(), 3, PRICE, AWARE)
    assert position.stop_price == Decimal("97.00")
    assert position.target_price == Decimal("112.00")


async def test_close_reads_opening_commission_through_orders_get(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(_key: str) -> None:
        return None

    monkeypatch.setattr("zarabot.db.positions.get_order", missing, raising=False)
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(exit_order)
    closed = await close(
        position.id,
        ExitTrigger.TAKE_PROFIT,
        Decimal("110.00"),
        AWARE,
        exit_order,
    )
    # Opening row is ignored when get_order returns None → entry commission 0.
    assert closed.realised_pnl == Decimal("198.50")


async def test_recompute_realised_rewrites_closed_pnl(db: Path) -> None:
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    exit_order = _order(
        key=EXIT_KEY,
        side=Side.SELL,
        intent="EXIT",
        filled_price=Decimal("110.00"),
        commission=Decimal("1.50"),
    )
    await _insert_order(exit_order)
    closed = await close(
        position.id,
        ExitTrigger.TAKE_PROFIT,
        Decimal("110.00"),
        AWARE,
        exit_order,
    )
    assert closed.realised_pnl == Decimal("197.00")
    await record_commission(KEY, Decimal("3.00"))
    updated = await recompute_realised(position.id)
    assert updated.realised_pnl == Decimal("195.50")
    assert updated.status == "CLOSED"


async def test_recompute_realised_rejects_open_or_absent(db: Path) -> None:
    with pytest.raises(PositionStateError):
        await recompute_realised(999)
    position = await open(_signal(), _order(), _instrument(), STOP, TARGET, AWARE)
    with pytest.raises(PositionStateError):
        await recompute_realised(position.id)
