"""Tests for zarabot.db.orders — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

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
    monkeypatch.setattr("zarabot.db.orders.now", lambda: NOW)
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    return path


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
    entry = await record_submitting(
        "22222222-2222-4222-8222-222222222222", "SBER", Side.BUY, 1, "ENTRY"
    )
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
