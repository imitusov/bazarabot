"""Tests for zarabot.state.halt — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.config import load
from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect
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
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture alerts at the `telegram.notifier` boundary; never send."""
    sent: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        sent.append(text)

    monkeypatch.setattr("zarabot.state.halt.alert", _alert, raising=False)
    return sent


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


async def test_second_halt_for_the_same_reason_is_idempotent(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.MANUAL, "first", NOW)
    await halt(HaltReason.MANUAL, "second", NOW + timedelta(hours=1))
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.MANUAL
    assert state.detail == "first"
    assert state.halted_at == NOW
    assert alerts == []


async def test_daily_loss_halt_replaces_manual_halt_and_alerts(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    later = NOW + timedelta(hours=1)
    await halt(HaltReason.DAILY_LOSS_LIMIT, "daily loss 5% reached limit 5%", later)
    state = await current()
    assert state is not None
    assert state.halted is True
    assert state.reason is HaltReason.DAILY_LOSS_LIMIT
    assert state.detail == "daily loss 5% reached limit 5%"
    assert len(alerts) == 1
    assert "daily loss 5% reached limit 5%" in alerts[0]


async def test_manual_halt_during_daily_loss_halt_changes_nothing(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.DAILY_LOSS_LIMIT, "daily loss breached", NOW)
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW + timedelta(hours=1))
    state = await current()
    assert state is not None
    assert state.halted is True
    assert state.reason is HaltReason.DAILY_LOSS_LIMIT
    assert state.detail == "daily loss breached"
    assert state.halted_at == NOW
    assert alerts == []


async def test_reconciliation_mismatch_replaces_manual_halt_and_alerts(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    await halt(
        HaltReason.RECONCILIATION_MISMATCH,
        "holdings disagree",
        NOW + timedelta(hours=1),
    )
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.RECONCILIATION_MISMATCH
    assert state.detail == "holdings disagree"
    assert len(alerts) == 1
    assert "holdings disagree" in alerts[0]


async def test_reconciliation_mismatch_does_not_replace_daily_loss_halt(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.DAILY_LOSS_LIMIT, "daily loss breached", NOW)
    await halt(
        HaltReason.RECONCILIATION_MISMATCH,
        "holdings disagree",
        NOW + timedelta(hours=1),
    )
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.DAILY_LOSS_LIMIT
    assert state.detail == "daily loss breached"
    assert alerts == []


async def test_severity_upgrade_keeps_the_halt_persistent_and_resumable(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    later = NOW + timedelta(hours=1)
    await halt(HaltReason.DAILY_LOSS_LIMIT, "daily loss breached", later)
    await disconnect()
    await connect(str(db))
    survived = await current()
    assert survived is not None
    assert survived.reason is HaltReason.DAILY_LOSS_LIMIT
    assert survived.detail == "daily loss breached"
    assert survived.resumed_at is None
    assert survived.resumed_by is None
    assert await resume("owner", later + timedelta(minutes=1)) is True
    assert await is_halted() is False


async def test_severity_upgrade_rejects_a_naive_datetime(
    db: Path, alerts: list[str]
) -> None:
    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    with pytest.raises(ValueError):
        await halt(
            HaltReason.DAILY_LOSS_LIMIT,
            "daily loss breached",
            NOW.replace(tzinfo=None),
        )
    state = await current()
    assert state is not None
    assert state.reason is HaltReason.MANUAL
    assert alerts == []


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
        trade_date=date(2026, 3, 16),  # the Moscow date of NOW
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


def _halt_events(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == event
    ]


async def test_daily_loss_halt_emits_halt_triggered_with_caller_pct(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    loss = Decimal("5.25")
    with caplog.at_level(logging.CRITICAL, logger="zarabot.state.halt"):
        await halt(
            HaltReason.DAILY_LOSS_LIMIT,
            "daily loss 5.25% reached limit 5%",
            NOW,
            daily_loss_pct=loss,
        )
    events = _halt_events(caplog, "halt_triggered")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.CRITICAL
    assert record.reason == HaltReason.DAILY_LOSS_LIMIT.value
    assert record.detail == "daily loss 5.25% reached limit 5%"
    assert record.daily_loss_pct == loss


async def test_halt_without_daily_loss_pct_omits_the_field(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.CRITICAL, logger="zarabot.state.halt"):
        await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    events = _halt_events(caplog, "halt_triggered")
    assert len(events) == 1
    record = events[0]
    assert record.reason == HaltReason.MANUAL.value
    assert record.detail == "owner pressed halt"
    assert not hasattr(record, "daily_loss_pct")


async def test_weaker_rehalt_emits_no_halt_triggered(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await halt(HaltReason.DAILY_LOSS_LIMIT, "daily loss breached", NOW)
    caplog.clear()
    with caplog.at_level(logging.CRITICAL, logger="zarabot.state.halt"):
        await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    assert _halt_events(caplog, "halt_triggered") == []


async def test_escalation_emits_halt_triggered_with_new_reason_and_pct(
    db: Path, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: upgrade is persist-or-upgrade; #9 is this path.

    Takes `alerts` because the escalation branch calls `alert()` one line after
    the emit. The fixture is not autouse, so without it this case runs the real
    `telegram.notifier` — rule 13 would swallow the failure and the test would
    still pass, which is exactly how a real send gets into a suite unnoticed.
    """

    await halt(HaltReason.MANUAL, "owner pressed halt", NOW)
    caplog.clear()
    loss = Decimal("5.25")
    later = NOW + timedelta(hours=1)
    with caplog.at_level(logging.CRITICAL, logger="zarabot.state.halt"):
        await halt(
            HaltReason.DAILY_LOSS_LIMIT,
            "daily loss 5.25% reached limit 5%",
            later,
            daily_loss_pct=loss,
        )
    events = _halt_events(caplog, "halt_triggered")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.CRITICAL
    assert record.reason == HaltReason.DAILY_LOSS_LIMIT.value
    assert record.detail == "daily loss 5.25% reached limit 5%"
    assert record.daily_loss_pct == loss


async def test_resume_emits_halt_cleared_with_actor(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await halt(HaltReason.MANUAL, "stop", NOW)
    with caplog.at_level(logging.INFO, logger="zarabot.state.halt"):
        assert await resume("owner", NOW) is True
    events = _halt_events(caplog, "halt_cleared")
    assert len(events) == 1
    assert events[0].levelno == logging.INFO
    assert events[0].actor == "owner"


async def test_noop_resume_emits_no_halt_cleared(
    db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="zarabot.state.halt"):
        assert await resume("owner", NOW) is False
    assert _halt_events(caplog, "halt_cleared") == []
