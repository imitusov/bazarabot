"""Tests for zarabot.state.halt — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.config import load
from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect
from zarabot.db.migrations import apply
from zarabot.lifecycle.exits import evaluate
from zarabot.models import ExitTrigger, HaltReason, Position, SessionInfo, StopProtection
from zarabot.state.halt import current, halt, is_halted, resume

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
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
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


async def test_halt_then_read_reports_reason(db: Path) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    assert await is_halted() is True
    state = await current()
    assert state is not None
    assert state.halted is True
    assert state.reason is HaltReason.MANUAL
    assert state.detail == "owner pressed halt"
    assert state.halted_at == NOW


async def test_halt_survives_disconnect_then_connect_restart(db: Path) -> None:
    await halt(HaltReason.DAILY_LOSS_LIMIT, "loss", NOW)
    await disconnect()
    await connect(str(db))
    assert await is_halted() is True
    again = await current()
    assert again is not None
    assert again.halted is True
    assert again.reason is HaltReason.DAILY_LOSS_LIMIT
    assert again.detail == "loss"
    assert again.halted_at == NOW


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.state.halt as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    assert "_connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    await halt(HaltReason.MANUAL, "stop", NOW)
    assert await is_halted() is True
    assert await current() is not None
    assert await resume("owner", NOW + timedelta(minutes=1)) is True
    assert calls == []


async def test_resume_clears_halt_and_records_actor(db: Path) -> None:
    await halt(HaltReason.MANUAL, "stop", NOW)
    later = NOW + timedelta(minutes=5)
    assert await resume("owner", later) is True
    assert await is_halted() is False
    state = await current()
    assert state is not None
    assert state.halted is False
    assert state.resumed_by == "owner"
    assert state.resumed_at == later


async def test_resume_when_not_halted_returns_false(db: Path) -> None:
    before = await current()
    assert await is_halted() is False
    assert await resume("owner", NOW) is False
    assert await is_halted() is False
    after = await current()
    assert after is not None
    assert before is not None
    assert after == before


async def test_second_halt_is_idempotent(db: Path) -> None:
    await halt(HaltReason.MANUAL, "first", NOW)
    await halt(HaltReason.RECONCILIATION_MISMATCH, "second", NOW + timedelta(hours=1))
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.MANUAL
    assert state.detail == "first"


async def test_halt_does_not_block_exits_or_exit_orders(db: Path) -> None:
    await halt(HaltReason.MANUAL, "entries off", NOW)
    position = Position(
        id=1,
        ticker="SBER",
        figi="BBG000000001",
        strategy="ma_crossover",
        lots=1,
        lot_size=10,
        entry_price=Decimal("100"),
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=False,
        open_order_key="k",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )
    session = SessionInfo(
        start=NOW,
        end=NOW + timedelta(hours=4),
        is_trading_day=True,
    )
    trigger = evaluate(position, Decimal("95"), NOW, session, 0, load())
    assert trigger is ExitTrigger.STOP_LOSS
    assert await is_halted() is True

    from zarabot.execution.orders import close_position
    from zarabot.lifecycle import exits as exits_mod

    assert "is_halted" not in inspect.getsource(exits_mod)
    assert "is_halted" not in inspect.getsource(close_position)


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    with pytest.raises(DatabaseNotOpenError):
        await is_halted()
    with pytest.raises(DatabaseNotOpenError):
        await current()
    with pytest.raises(DatabaseNotOpenError):
        await halt(HaltReason.MANUAL, "x", NOW)
    with pytest.raises(DatabaseNotOpenError):
        await resume("owner", NOW)
