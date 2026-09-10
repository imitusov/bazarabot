"""Tests for zarabot.db.stop_orders — derived from the module contract."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.db.orders import (
    DuplicateOrderError,
    OrderStateError,
    record_submitting,
    settle,
)
from zarabot.db.positions import close as close_position
from zarabot.db.positions import open as open_position
from zarabot.db.stop_orders import (
    activate,
    active_for_position,
    list_active,
    record_placing,
)
from zarabot.db.stop_orders import settle as settle_stop
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    OrderStatus,
    Side,
    Signal,
    StopOrderStatus,
)

AWARE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KEY2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
STOP = Decimal("95.00")
ORDER_KEY = "11111111-1111-4111-8111-111111111111"

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
    monkeypatch.setattr("zarabot.clock.now", lambda: AWARE)
    monkeypatch.setattr("zarabot.db.stop_orders.now", lambda: AWARE, raising=False)
    conn = await connect(str(path))
    await apply(conn)
    await record_submitting(ORDER_KEY, "SBER", Side.BUY, 2, "ENTRY")
    await settle(ORDER_KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None, None)
    try:
        yield path
    finally:
        await disconnect()


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
        reference_price=Decimal("100.00"),
    )


def _order() -> OrderRecord:
    return OrderRecord(
        key=ORDER_KEY,
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


async def _position_id() -> int:
    position = await open_position(
        _signal(), _order(), _instrument(), STOP, Decimal("110.00"), AWARE
    )
    return position.id


async def test_record_placing_then_activate(db: Path) -> None:
    position_id = await _position_id()
    placing = await record_placing(KEY, position_id, "SBER", 2, STOP)
    assert placing.status is StopOrderStatus.PLACING
    assert placing.stop_order_id is None
    assert placing.stop_price == STOP
    assert isinstance(placing.stop_price, Decimal)
    assert placing.created_at == AWARE
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
    await activate(KEY, "broker-stop-1")
    with pytest.raises(DuplicateOrderError):
        await record_placing(KEY, position_id, "SBER", 2, STOP)
    # §3.2 "and leaves one row": the rejected write must not have replaced,
    # reset or duplicated the row that was already there.
    surviving = await active_for_position(position_id)
    assert surviving is not None
    assert surviving.key == KEY
    assert surviving.status is StopOrderStatus.ACTIVE
    assert surviving.stop_order_id == "broker-stop-1"
    assert [row.key for row in await list_active()] == [KEY]


async def test_missing_position_raises_integrity_error(db: Path) -> None:
    with pytest.raises(aiosqlite.IntegrityError):
        await record_placing(KEY, 999_999, "SBER", 2, STOP)


async def test_second_live_stop_for_position_raises_integrity_error(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    with pytest.raises(aiosqlite.IntegrityError):
        await record_placing(KEY2, position_id, "SBER", 2, STOP)


async def test_terminal_stop_cannot_resettle(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    await activate(KEY, "broker-stop-1")
    await settle_stop(KEY, StopOrderStatus.CANCELLED, AWARE)
    later = datetime(2026, 3, 16, 11, 0, tzinfo=UTC)
    with pytest.raises(OrderStateError):
        await settle_stop(KEY, StopOrderStatus.EXECUTED, later)
    assert await active_for_position(position_id) is None
    assert await list_active() == []
    # §3.2 "and leaves the first outcome in place": both CANCELLED and
    # EXECUTED are terminal, so neither view above can tell them apart. Read
    # the row back — a stop the exchange fired and a stop that never stood
    # must stay distinguishable after a late duplicate report.
    conn = shared()
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT status, settled_at FROM stop_orders WHERE key = ?", (KEY,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == StopOrderStatus.CANCELLED.value
    assert row["settled_at"] == AWARE.isoformat()


async def test_settle_rejects_non_terminal_and_naive(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    with pytest.raises(OrderStateError):
        await settle_stop(KEY, StopOrderStatus.ACTIVE, AWARE)
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await settle_stop(KEY, StopOrderStatus.FAILED, naive)
    # The row must be untouched: rule 22 rejects the argument, it does not
    # write it and discover the problem on read-back.
    still_placing = await active_for_position(position_id)
    assert still_placing is not None
    assert still_placing.status is StopOrderStatus.PLACING
    # And the rejection must happen before the row is looked up at all —
    # otherwise deleting the guard in `settle` leaves this case green, because
    # a naive timestamp written through raises `ValueError` again on read-back
    # and nothing distinguishes the two. An unknown key proves the ordering:
    # `OrderStateError` is not a `ValueError`, so only the guard can satisfy
    # this.
    with pytest.raises(ValueError):
        await settle_stop("never-recorded", StopOrderStatus.FAILED, naive)


async def test_activate_and_settle_absent_or_terminal_raise(db: Path) -> None:
    with pytest.raises(OrderStateError):
        await activate(KEY, "broker-stop-1")
    with pytest.raises(OrderStateError):
        await settle_stop(KEY, StopOrderStatus.FAILED, AWARE)
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    await settle_stop(KEY, StopOrderStatus.FAILED, AWARE)
    with pytest.raises(OrderStateError):
        await activate(KEY, "too-late")


async def test_placing_is_standing_for_position_but_not_list_active(db: Path) -> None:
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    found = await active_for_position(position_id)
    assert found is not None
    assert found.status is StopOrderStatus.PLACING
    assert await list_active() == []


async def test_list_active_oldest_first(db: Path) -> None:
    first = await open_position(
        _signal(), _order(), _instrument(), STOP, Decimal("110.00"), AWARE
    )
    await record_placing(KEY, first.id, "SBER", 2, STOP)
    await activate(KEY, "broker-a")
    await close_position(first.id, ExitTrigger.EXTERNAL, Decimal("100.00"), AWARE, None)
    conn = shared()
    second_order_key = "22222222-2222-4222-8222-222222222222"
    await record_submitting(second_order_key, "GAZP", Side.BUY, 1, "ENTRY")
    await settle(second_order_key, OrderStatus.FILLED, 1, Decimal("50.00"), None, None)
    gazp_order = OrderRecord(
        key=second_order_key,
        ticker="GAZP",
        figi="BBG000000002",
        side=Side.BUY,
        intent="ENTRY",
        lots=1,
        status=OrderStatus.FILLED,
        filled_lots=1,
        filled_price=Decimal("50.00"),
        commission=None,
        broker_reason=None,
        created_at=AWARE,
        settled_at=AWARE,
    )
    gazp_signal = Signal(
        ticker="GAZP",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=AWARE,
        reference_price=Decimal("50.00"),
    )
    gazp_instrument = Instrument(
        figi="BBG000000002",
        ticker="GAZP",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=AWARE,
    )
    second = await open_position(
        gazp_signal,
        gazp_order,
        gazp_instrument,
        Decimal("45.00"),
        Decimal("60.00"),
        AWARE,
    )
    await record_placing(KEY2, second.id, "GAZP", 1, Decimal("45.00"))
    await activate(KEY2, "broker-b")
    # Age the row that was inserted FIRST, so that "oldest first" and
    # "insertion order" disagree. With them agreeing — as they did before —
    # deleting `ORDER BY created_at ASC` left this assertion green, because
    # SQLite returns rowid order anyway. Now only the ORDER BY can satisfy it.
    later = datetime(2026, 3, 16, 11, 0, tzinfo=UTC)
    await conn.execute(
        "UPDATE stop_orders SET created_at = ? WHERE key = ?",
        (later.isoformat(), KEY),
    )
    await conn.commit()
    active = await list_active()
    assert [row.key for row in active] == [KEY2, KEY]
    assert active[0].created_at == AWARE
    assert active[1].created_at == later


async def test_active_for_position_none_when_absent(db: Path) -> None:
    assert await active_for_position(999) is None
    # §3.2 says "a position with no standing stop", not "a position id that
    # does not exist" — a real open position with no stop row is the case the
    # callers actually hit (failure class 9, fixture differs from caller).
    position_id = await _position_id()
    assert await active_for_position(position_id) is None


async def test_active_for_position_raises_on_duplicate_standing(db: Path) -> None:
    position_id = await _position_id()
    conn = shared()
    await conn.execute("DROP INDEX idx_stop_orders_one_live")
    await conn.commit()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    await record_placing(KEY2, position_id, "SBER", 2, STOP)
    with pytest.raises(OrderStateError):
        await active_for_position(position_id)


async def test_settle_all_terminal_statuses(db: Path) -> None:
    terminals = (
        StopOrderStatus.CANCELLED,
        StopOrderStatus.EXECUTED,
        StopOrderStatus.ORPHANED,
        StopOrderStatus.FAILED,
    )
    for index, status in enumerate(terminals):
        order_key = f"order-term-{index}"
        stop_key = f"stop-term-{index}"
        ticker = f"T{index}"
        await record_submitting(order_key, ticker, Side.BUY, 1, "ENTRY")
        await settle(order_key, OrderStatus.FILLED, 1, Decimal("10.00"), None, None)
        order = OrderRecord(
            key=order_key,
            ticker=ticker,
            figi=f"BBG{index}",
            side=Side.BUY,
            intent="ENTRY",
            lots=1,
            status=OrderStatus.FILLED,
            filled_lots=1,
            filled_price=Decimal("10.00"),
            commission=None,
            broker_reason=None,
            created_at=AWARE,
            settled_at=AWARE,
        )
        signal = Signal(
            ticker=ticker,
            strategy="ma_crossover",
            side=Side.BUY,
            generated_at=AWARE,
            reference_price=Decimal("10.00"),
        )
        instrument = Instrument(
            figi=f"BBG{index}",
            ticker=ticker,
            lot=10,
            min_price_increment=Decimal("0.01"),
            currency="RUB",
            trading_status="NORMAL_TRADING",
            refreshed_at=AWARE,
        )
        position = await open_position(
            signal, order, instrument, Decimal("9.00"), Decimal("12.00"), AWARE
        )
        await record_placing(stop_key, position.id, ticker, 1, Decimal("9.00"))
        settled = await settle_stop(stop_key, status, AWARE)
        assert settled.status is status
        assert settled.settled_at == AWARE


async def test_mutations_are_atomic_and_own_no_transaction(db: Path) -> None:
    """Spec §4: writes run inside `db.connection.transaction()`.

    Asserts the contract — the mutation is atomic and this module touches no
    transaction state — rather than asserting that a particular SQL string
    appears in the source, which pinned the implementation and would have gone
    red for the fix rather than for a defect.
    """
    for func in (record_placing, activate, settle_stop):
        source = inspect.getsource(func)
        assert "BEGIN" not in source
        assert "conn.commit()" not in source
        assert "conn.rollback()" not in source

    position_id = await _position_id()
    conn = shared()
    original = conn.execute

    async def failing(sql: str, parameters: object = ()) -> aiosqlite.Cursor:
        if "INSERT INTO stop_orders" in sql:
            raise RuntimeError("injected")
        return await original(sql, parameters)

    conn.execute = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected"):
        await record_placing("k-atomic", position_id, "SBER", 1, Decimal("9.00"))
    conn.execute = original  # type: ignore[method-assign]
    assert await active_for_position(position_id) is None


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    with pytest.raises(DatabaseNotOpenError):
        await list_active()
    with pytest.raises(DatabaseNotOpenError):
        await active_for_position(1)


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.db.stop_orders as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    assert "_connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    position_id = await _position_id()
    await record_placing(KEY, position_id, "SBER", 2, STOP)
    await activate(KEY, "broker-stop-1")
    await active_for_position(position_id)
    await list_active()
    await settle_stop(KEY, StopOrderStatus.CANCELLED, AWARE)
    assert calls == []
