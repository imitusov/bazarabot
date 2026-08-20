"""Tests for zarabot.state.halt — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.config import Config
from zarabot.db.migrations import apply
from zarabot.lifecycle.exits import evaluate
from zarabot.models import (
    ExitTrigger,
    HaltReason,
    Position,
    SessionInfo,
    StopProtection,
)
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
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    return path


async def test_halt_then_read_reports_reason(db: Path) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    assert await is_halted() is True
    state = await current()
    assert state is not None
    assert state.halted is True
    assert state.reason is HaltReason.MANUAL
    assert state.detail == "owner pressed halt"
    assert state.halted_at == NOW


async def test_halt_survives_reread(db: Path) -> None:
    await halt(HaltReason.DAILY_LOSS_LIMIT, "loss", NOW)
    assert await is_halted() is True
    again = await current()
    assert again is not None
    assert again.halted is True
    assert again.reason is HaltReason.DAILY_LOSS_LIMIT


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
    assert await is_halted() is False
    assert await resume("owner", NOW) is False
    assert await is_halted() is False


async def test_second_halt_is_idempotent(db: Path) -> None:
    await halt(HaltReason.MANUAL, "first", NOW)
    await halt(HaltReason.RECONCILIATION_MISMATCH, "second", NOW + timedelta(hours=1))
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.MANUAL
    assert state.detail == "first"


async def test_halt_does_not_block_exit_evaluation(db: Path) -> None:
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
    config = Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=Decimal("100000"),
        position_size_pct=Decimal("10"),
        max_position_pct=Decimal("20"),
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("10"),
        max_holding_days=3,
        max_open_positions=10,
        reentry_cooldown_minutes=120,
        daily_loss_limit_pct=Decimal("5"),
        watchlist=("SBER",),
        enabled_strategies=("ma_crossover",),
        ml_model_path=None,
        poll_interval_seconds=60,
        db_path=Path("zarabot.db"),
        backup_dir=Path("backups"),
        log_level="INFO",
        tz="Europe/Moscow",
    )
    trigger = evaluate(position, Decimal("95"), NOW, session, 0, config)
    assert trigger is ExitTrigger.STOP_LOSS
    assert await is_halted() is True
