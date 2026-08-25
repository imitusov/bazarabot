"""Tests for zarabot.db.orders — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.db.orders import (
    DuplicateOrderError,
    OrderStateError,
    get,
    list_missing_commission,
    list_unresolved,
    record_commission,
    record_submitting,
    settle,
)
from zarabot.models import ExitTrigger, OrderStatus, Side

KEY = "11111111-1111-4111-8111-111111111111"
KEY2 = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)

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
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    monkeypatch.setattr("zarabot.db.orders.now", lambda: NOW, raising=False)
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


async def test_submitting_then_filled_reports_terminal_state(db: Path) -> None:
    recorded = await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    assert recorded.status is OrderStatus.SUBMITTING
    filled = await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.50"), None, None)
    assert filled.status is OrderStatus.FILLED
    assert filled.filled_lots == 2
    assert filled.filled_price == Decimal("100.50")
    assert isinstance(filled.filled_price, Decimal)


async def test_submitting_orders_are_listed_unresolved(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    unresolved = await list_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].key == KEY
    assert unresolved[0].status is OrderStatus.SUBMITTING
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None, None)
    assert await list_unresolved() == []


async def test_unresolved_includes_submitted_oldest_first(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await record_submitting(KEY2, "SBER", Side.BUY, 1, "ENTRY")
    conn = shared()
    await conn.execute("UPDATE orders SET status = 'SUBMITTED' WHERE key = ?", (KEY2,))
    await conn.commit()
    unresolved = await list_unresolved()
    assert [order.key for order in unresolved] == [KEY, KEY2]
    assert unresolved[1].status is OrderStatus.SUBMITTED


async def test_duplicate_idempotency_key_raises(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    with pytest.raises(DuplicateOrderError):
        await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")


async def test_exit_without_trigger_and_entry_with_trigger_raise(db: Path) -> None:
    with pytest.raises(ValueError):
        await record_submitting(KEY, "SBER", Side.SELL, 1, "EXIT")
    with pytest.raises(ValueError):
        await record_submitting(
            KEY, "SBER", Side.BUY, 1, "ENTRY", ExitTrigger.TAKE_PROFIT
        )
    recorded = await record_submitting(
        KEY, "SBER", Side.SELL, 1, "EXIT", ExitTrigger.STOP_LOSS
    )
    assert recorded.intent == "EXIT"
    assert recorded.exit_trigger is ExitTrigger.STOP_LOSS
    entry = await record_submitting(KEY2, "SBER", Side.BUY, 1, "ENTRY")
    assert entry.intent == "ENTRY"
    assert entry.exit_trigger is None


async def test_terminal_order_cannot_leave_terminal_state(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None, None)
    with pytest.raises(OrderStateError):
        await settle(KEY, OrderStatus.SUBMITTED, 2, Decimal("100.00"), None, None)
    with pytest.raises(OrderStateError):
        await settle(KEY, OrderStatus.CANCELLED, 0, None, None, "too late")


async def test_settle_persists_commission_and_none_is_not_zero(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    filled = await settle(
        KEY, OrderStatus.FILLED, 2, Decimal("100.50"), Decimal("1.50"), None
    )
    assert filled.commission == Decimal("1.50")
    assert isinstance(filled.commission, Decimal)
    await record_submitting(KEY2, "SBER", Side.BUY, 1, "ENTRY")
    unknown = await settle(KEY2, OrderStatus.FILLED, 1, Decimal("100.00"), None, None)
    assert unknown.commission is None
    zero_key = "33333333-3333-4333-8333-333333333333"
    await record_submitting(zero_key, "SBER", Side.BUY, 1, "ENTRY")
    zero = await settle(
        zero_key, OrderStatus.FILLED, 1, Decimal("100.00"), Decimal("0"), None
    )
    assert zero.commission == Decimal("0")
    assert zero.commission is not None
    loaded_unknown = await get(KEY2)
    loaded_zero = await get(zero_key)
    assert loaded_unknown is not None
    assert loaded_zero is not None
    assert loaded_unknown.commission is None
    assert loaded_zero.commission == Decimal("0")


async def test_record_commission_clears_missing_list(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None, None)
    since = NOW - timedelta(days=1)
    until = NOW + timedelta(days=1)
    missing = await list_missing_commission(since, until)
    assert [order.key for order in missing] == [KEY]
    updated = await record_commission(KEY, Decimal("1.25"))
    assert updated.commission == Decimal("1.25")
    assert await list_missing_commission(since, until) == []


async def test_get_returns_order_or_none(db: Path) -> None:
    assert await get(KEY) is None
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    loaded = await get(KEY)
    assert loaded is not None
    assert loaded.key == KEY
    assert loaded.status is OrderStatus.SUBMITTING


async def test_record_commission_absent_raises(db: Path) -> None:
    with pytest.raises(OrderStateError):
        await record_commission(KEY, Decimal("1.00"))


async def test_list_missing_commission_rejects_naive(db: Path) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await list_missing_commission(naive, NOW)
    with pytest.raises(ValueError):
        await list_missing_commission(NOW, naive)


async def test_get_does_not_commit_outer_transaction(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    conn = shared()
    await conn.execute("BEGIN IMMEDIATE")
    await conn.execute("UPDATE orders SET ticker = 'TEMP' WHERE key = ?", (KEY,))
    loaded = await get(KEY)
    assert loaded is not None
    assert loaded.ticker == "TEMP"
    await conn.rollback()
    after = await get(KEY)
    assert after is not None
    assert after.ticker == "SBER"


async def test_settle_owns_no_transaction_and_is_atomic(db: Path) -> None:
    """Spec §4: `settle` runs inside `db.connection.transaction()`.

    The previous version asserted `"BEGIN IMMEDIATE" in inspect.getsource(...)`,
    which pinned the implementation rather than the contract.
    """
    source = inspect.getsource(settle)
    assert "BEGIN" not in source
    assert "conn.commit()" not in source
    assert "conn.rollback()" not in source

    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    conn = shared()
    original = conn.execute

    async def failing(sql: str, parameters: object = ()) -> aiosqlite.Cursor:
        if "UPDATE orders" in sql:
            raise RuntimeError("injected")
        return await original(sql, parameters)

    conn.execute = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected"):
        await settle(KEY, OrderStatus.FILLED, Decimal("10"), 2, NOW, None)
    conn.execute = original  # type: ignore[method-assign]
    still = await get(KEY)
    assert still is not None
    assert still.status is OrderStatus.SUBMITTING


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    with pytest.raises(DatabaseNotOpenError):
        await list_unresolved()
    with pytest.raises(DatabaseNotOpenError):
        await get(KEY)


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.db.orders as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await get(KEY)
    await list_unresolved()
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None, None)
    await list_missing_commission(NOW - timedelta(days=1), NOW + timedelta(days=1))
    await record_commission(KEY, Decimal("1.00"))
    assert calls == []
