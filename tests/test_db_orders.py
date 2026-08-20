"""Tests for zarabot.db.orders — written from technical-spec.md §3.2."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
from zarabot.db.orders import (
    DuplicateOrderError,
    OrderStateError,
    list_unresolved,
    record_submitting,
    settle,
)
from zarabot.models import OrderStatus, Side

KEY = "11111111-1111-4111-8111-111111111111"

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
    return path


async def test_submitting_then_filled_reports_terminal_state(db: Path) -> None:
    recorded = await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    assert recorded.status is OrderStatus.SUBMITTING
    filled = await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.50"), None)
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
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None)
    assert await list_unresolved() == []


async def test_duplicate_idempotency_key_raises(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    with pytest.raises(DuplicateOrderError):
        await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")


async def test_terminal_order_cannot_leave_terminal_state(db: Path) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await settle(KEY, OrderStatus.FILLED, 2, Decimal("100.00"), None)
    with pytest.raises(OrderStateError):
        await settle(KEY, OrderStatus.SUBMITTED, 2, Decimal("100.00"), None)
    with pytest.raises(OrderStateError):
        await settle(KEY, OrderStatus.CANCELLED, 0, None, "too late")
