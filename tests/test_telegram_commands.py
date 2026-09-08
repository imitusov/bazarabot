"""Tests for zarabot.telegram.commands — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from zarabot.config import Config, get, load
from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.snapshots import DailySnapshot, write_daily
from zarabot.models import (
    ExitTrigger,
    Position,
    SessionInfo,
    StopProtection,
)
from zarabot.state.halt import is_halted
from zarabot.telegram.commands import (
    halt,
    help,
    history,
    pnl,
    positions,
    report,
    resume,
    set_report_builder,
    status,
    strategies,
)

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
SESSION_END = datetime(2026, 3, 16, 16, 0, tzinfo=UTC)
AUTH_CHAT = 42
REQUIRED_ENV = {
    "TINVEST_TOKEN": "tinvest-secret-token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
    "TELEGRAM_CHAT_ID": "42",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.replies: list[str] = []
        self.fail_times = 0
        self.text = text

    async def reply_text(self, text: str, **kwargs: object) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("network")
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, chat_id: int, command: str = "") -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage(f"/{command}" if command else "")


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 2,
        "lot_size": 10,
        "entry_price": Decimal("100"),
        "entry_at": NOW,
        "stop_price": Decimal("95"),
        "target_price": Decimal("110"),
        "status": "OPEN",
        "adopted": False,
        "open_order_key": "open-k",
        "close_order_key": None,
        "exit_trigger": None,
        "exit_price": None,
        "exit_at": None,
        "realised_pnl": None,
        "stop_protection": StopProtection.LOCAL,
        "stop_order_key": None,
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


def _closed(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "status": "CLOSED",
        "close_order_key": "close-k",
        "exit_trigger": ExitTrigger.TAKE_PROFIT,
        "exit_price": Decimal("110"),
        "exit_at": NOW,
        "realised_pnl": Decimal("199.50"),
    }
    fields.update(overrides)
    return _position(**fields)


def _risk_limits(cfg: Config) -> tuple[object, ...]:
    """Every limit the bot enforces. `max_position_pct` is gone — it never bound."""
    return (
        cfg.allocated_capital,
        cfg.position_size_pct,
        cfg.cash_reserve_pct,
        cfg.stop_loss_pct,
        cfg.take_profit_pct,
        cfg.max_holding_days,
        cfg.max_open_positions,
        cfg.reentry_cooldown_minutes,
        cfg.daily_loss_limit_pct,
    )


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    get.cache_clear()
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    monkeypatch.setattr("zarabot.telegram.commands.now", lambda: NOW)
    session = SessionInfo(
        start=NOW - timedelta(hours=2),
        end=SESSION_END,
        is_trading_day=True,
    )
    monkeypatch.setattr("zarabot.telegram.commands.is_open", lambda moment: True)
    monkeypatch.setattr(
        "zarabot.telegram.commands.current_session", lambda moment: session
    )
    monkeypatch.setattr("zarabot.telegram.commands.next_open", lambda moment: moment)

    async def _price(figi: str) -> Decimal:
        return Decimal("105")

    async def _benchmark(start: date, end: date) -> Decimal:
        return Decimal("1.25")

    monkeypatch.setattr("zarabot.telegram.commands.get_last_price", _price)
    monkeypatch.setattr("zarabot.telegram.commands.benchmark_return", _benchmark)
    set_report_builder(None)
    await connect(str(path))
    try:
        yield path
    finally:
        await disconnect()
        get.cache_clear()


async def _reply(handler: Any, chat_id: int = AUTH_CHAT) -> str:
    update = _FakeUpdate(chat_id, handler.__name__)
    await handler(update, None)
    assert update.message.replies
    return update.message.replies[-1]


async def test_each_command_from_authorised_chat_returns_documented_content(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await write_daily(
        DailySnapshot(
            trade_date=date(2026, 3, 16),
            opening_equity=Decimal("100000"),
            closing_equity=None,
            cash=Decimal("90000"),
            realised_pnl=Decimal("50.00"),
            unrealised_pnl=Decimal("25.00"),
            open_positions=1,
            orders_placed=7,
            benchmark_value=None,
        )
    )
    open_pos = _position()
    closed = _closed()

    async def _open() -> list[Position]:
        return [open_pos]

    async def _closed_rows() -> list[Position]:
        return [closed]

    monkeypatch.setattr("zarabot.telegram.commands.list_open", _open)
    monkeypatch.setattr("zarabot.telegram.commands.list_closed", _closed_rows)

    async def _benchmark(start: date, end: date) -> Decimal:
        return Decimal("1.25")

    monkeypatch.setattr("zarabot.telegram.commands.benchmark_return", _benchmark)

    report_calls: list[tuple[date, date]] = []

    async def _build(start: date, end: date) -> str:
        report_calls.append((start, end))
        return "Weekly report body"

    set_report_builder(_build)

    status_text = await _reply(status)
    assert "active" in status_text.lower()
    assert "75.00" in status_text or "75" in status_text
    assert "7" in status_text
    assert "1" in status_text
    assert "session" in status_text.lower() or "until" in status_text.lower()

    positions_text = await _reply(positions)
    assert "SBER" in positions_text
    assert "100" in positions_text
    assert "105" in positions_text
    assert "ma_crossover" in positions_text

    history_text = await _reply(history)
    assert "SBER" in history_text
    assert "TAKE_PROFIT" in history_text or "take profit" in history_text.lower()

    pnl_text = await _reply(pnl)
    assert "today" in pnl_text.lower()
    assert "week" in pnl_text.lower()
    assert "inception" in pnl_text.lower() or "since" in pnl_text.lower()
    assert "1.25" in pnl_text or "benchmark" in pnl_text.lower()

    halt_text = await _reply(halt)
    assert await is_halted() is True
    assert "halt" in halt_text.lower()
    status_halted = await _reply(status)
    assert "halt" in status_halted.lower()

    resume_text = await _reply(resume)
    assert await is_halted() is False
    assert "stop" in resume_text.lower() or "limit" in resume_text.lower()
    assert "5" in resume_text
    lowered = resume_text.lower()
    for label in (
        "daily loss",
        "position size",
        "portfolio exposure",
        "cash reserve",
        "maximum open positions",
        "cooldown",
    ):
        assert label in lowered, f"/resume omits the {label} limit"

    strategies_text = await _reply(strategies)
    assert "ma_crossover" in strategies_text
    assert "rsi_reversion" in strategies_text
    assert "momentum" in strategies_text
    assert "N/A" in strategies_text or "n/a" in strategies_text.lower()

    report_text = await _reply(report)
    assert report_text == "Weekly report body"
    assert report_calls == [(date(2026, 3, 16), date(2026, 3, 22))]

    help_text = await _reply(help)
    for command in (
        "/status",
        "/positions",
        "/history",
        "/pnl",
        "/halt",
        "/resume",
        "/strategies",
        "/report",
        "/help",
    ):
        assert command in help_text


async def test_unauthorised_chat_gets_no_reply_no_state_change_and_is_logged(
    env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: the security boundary is a structured event, not free text."""
    handlers = (
        status,
        positions,
        history,
        pnl,
        halt,
        resume,
        strategies,
        report,
        help,
    )
    commands = (
        "status",
        "positions",
        "history",
        "pnl",
        "halt",
        "resume",
        "strategies",
        "report",
        "help",
    )
    replies: list[str] = []
    with caplog.at_level(logging.INFO, logger="zarabot.telegram.commands"):
        for handler, command in zip(handlers, commands, strict=True):
            probe = _FakeUpdate(99, command)
            await handler(probe, None)
            replies.extend(probe.message.replies)
    assert replies == []
    assert await is_halted() is False
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "unauthorised_command"
    ]
    assert len(events) == len(handlers)
    for record, command in zip(events, commands, strict=True):
        assert record.levelno == logging.INFO
        assert record.chat_id == 99
        assert record.command == command
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text


async def test_halt_passes_daily_loss_pct_from_pnl(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.69: a manual /halt still records the day's loss percentage."""
    captured: dict[str, object] = {}

    async def _persist(
        reason: object,
        detail: str,
        at: datetime,
        daily_loss_pct: Decimal | None = None,
    ) -> None:
        captured["reason"] = reason
        captured["daily_loss_pct"] = daily_loss_pct
        captured["at"] = at

    async def _loss(at: datetime) -> Decimal:
        captured["loss_at"] = at
        return Decimal("1.25")

    monkeypatch.setattr("zarabot.telegram.commands.persist_halt", _persist)
    monkeypatch.setattr(
        "zarabot.telegram.commands.daily_loss_pct", _loss, raising=False
    )
    await _reply(halt)
    assert captured["daily_loss_pct"] == Decimal("1.25")
    assert captured["at"] == NOW
    assert captured["loss_at"] == NOW


async def test_resume_when_not_halted_replies_nothing_was_halted(env: Path) -> None:
    text = await _reply(resume)
    assert "nothing was halted" in text.lower()
    assert await is_halted() is False


async def test_response_over_limit_is_truncated_with_omission_count(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = [
        _closed(
            id=i,
            ticker=f"T{i:04d}",
            open_order_key=f"open-{i}",
            close_order_key=f"close-{i}",
        )
        for i in range(1, 250)
    ]

    async def _closed_rows() -> list[Position]:
        return closed

    monkeypatch.setattr("zarabot.telegram.commands.list_closed", _closed_rows)
    text = await _reply(history)
    assert len(text) <= 4096
    assert "omitted" in text.lower()
    assert any(ch.isdigit() for ch in text)


async def test_no_command_mutates_a_risk_limit(env: Path) -> None:
    warm = get()
    before = _risk_limits(warm)
    handlers = (
        status,
        positions,
        history,
        pnl,
        halt,
        resume,
        strategies,
        report,
        help,
    )
    for handler in handlers:
        await _reply(handler)
    assert get() is warm
    assert _risk_limits(get()) == before
    # A fresh read of the environment agrees: no command wrote a limit anywhere.
    assert _risk_limits(load()) == before
    cfg = get()
    assert cfg.stop_loss_pct == Decimal("5")
    assert cfg.daily_loss_limit_pct == Decimal("5")
    assert cfg.position_size_pct == Decimal("10")
    assert cfg.cash_reserve_pct == Decimal("1")
    assert cfg.max_open_positions == 10
    assert cfg.reentry_cooldown_minutes == 120


async def test_resume_reports_every_binding_limit_and_no_withdrawn_cap(
    env: Path,
) -> None:
    """The summary names the controls that can actually reject an order (#15).

    `MAX_POSITION_PCT` was withdrawn because it could never be the binding
    minimum, yet this reply presented it as an active control. What replaces it
    is the runtime portfolio-exposure ceiling and the cash reserve.
    """
    cfg = get()
    await _reply(halt)
    text = await _reply(resume)
    lowered = text.lower()

    assert f"{cfg.daily_loss_limit_pct}%" in text
    assert f"{cfg.position_size_pct}%" in text
    assert f"{cfg.cash_reserve_pct}%" in text
    assert str(cfg.max_open_positions) in text
    assert str(cfg.reentry_cooldown_minutes) in text
    # The exposure ceiling is allocated capital itself: the summed cost of open
    # positions may not exceed it (risk.sizing headroom, gate PORTFOLIO_EXPOSURE).
    assert "portfolio exposure" in lowered
    assert "100000.00" in text
    # Exit rules bind too, and are read from the same configuration.
    assert f"{cfg.stop_loss_pct}%" in text
    assert f"{cfg.take_profit_pct}%" in text
    assert str(cfg.max_holding_days) in text

    for withdrawn in ("max position", "maximum per position", "per-position cap"):
        assert withdrawn not in lowered, f"/resume still reports {withdrawn}"


async def test_configuration_is_read_through_the_memoised_accessor(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handlers read `config.get()`, so limits cannot drift under a live process."""
    warm = get()
    monkeypatch.setenv("POSITION_SIZE_PCT", "7")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")

    await _reply(halt)
    text = await _reply(resume)
    assert get() is warm
    assert f"{warm.position_size_pct}%" in text
    assert "7%" not in text

    # The authorised chat is the memoised one, never a re-read of the environment.
    intruder = _FakeUpdate(777)
    await status(intruder, None)
    assert intruder.message.replies == []
