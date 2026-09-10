"""Tests for zarabot.reporter.weekly — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest
from telegram.error import NetworkError

from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.signals import record
from zarabot.models import (
    ExitTrigger,
    Position,
    RejectionReason,
    RiskDecision,
    Side,
    Signal,
    StopProtection,
)
from zarabot.reporter.weekly import build, send

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
START = date(2026, 3, 16)
END = date(2026, 3, 22)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 1,
        "lot_size": 10,
        "entry_price": Decimal("100"),
        "entry_at": NOW,
        "stop_price": Decimal("95"),
        "target_price": Decimal("110"),
        "status": "CLOSED",
        "adopted": False,
        "open_order_key": "open-1",
        "close_order_key": "close-1",
        "exit_trigger": ExitTrigger.TAKE_PROFIT,
        "exit_price": Decimal("110"),
        "exit_at": NOW,
        "realised_pnl": Decimal("99.00"),
        "stop_protection": StopProtection.LOCAL,
        "stop_order_key": None,
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)

    async def _benchmark(start: date, end: date) -> Decimal:
        return Decimal("1.50")

    monkeypatch.setattr("zarabot.reporter.weekly.benchmark_return", _benchmark)
    await connect(str(path))
    try:
        yield path
    finally:
        await disconnect()


async def test_week_with_trades_contains_every_documented_section(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = _position()
    loser = _position(
        id=2,
        ticker="GAZP",
        figi="BBG000000002",
        strategy="momentum",
        open_order_key="open-2",
        close_order_key="close-2",
        exit_trigger=ExitTrigger.STOP_LOSS,
        exit_price=Decimal("90"),
        realised_pnl=Decimal("-101.00"),
    )

    async def _closed() -> list[Position]:
        return [winner, loser]

    monkeypatch.setattr("zarabot.reporter.weekly.list_closed", _closed)
    await record(
        Signal(
            ticker="SBER",
            strategy="ma_crossover",
            side=Side.BUY,
            generated_at=NOW,
            reference_price=Decimal("100"),
        ),
        RiskDecision(approved=False, lots=None, reason=RejectionReason.COOLDOWN_ACTIVE),
    )
    text = await build(START, END)
    lowered = text.lower()
    assert "p&l" in lowered or "pnl" in lowered
    assert "benchmark" in lowered
    assert "ma_crossover" in text
    assert "momentum" in text
    assert "win rate" in lowered
    assert "worst trade" in lowered
    assert "exit-trigger" in lowered or "exit trigger" in lowered
    assert "cooldown" in lowered
    assert "gap" in lowered
    assert "GAZP" in text
    assert "-101.00" in text or "-101" in text


async def test_week_with_no_trades_produces_a_valid_empty_report(db: Path) -> None:
    text = await build(START, END)
    assert "no" in text.lower() and "trade" in text.lower()
    assert len(text) > 0


async def test_win_rate_with_zero_closed_trades_is_not_applicable_not_zero(
    db: Path,
) -> None:
    text = await build(START, END)
    assert re.search(r"win rate:\s*n/a", text, re.I)
    assert not re.search(r"win rate:\s*0", text, re.I)


async def test_over_limit_drops_least_important_section_and_notes_omission(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = _position()
    loser = _position(
        id=2,
        ticker="GAZP",
        figi="BBG000000002",
        strategy="momentum",
        open_order_key="open-2",
        close_order_key="close-2",
        exit_trigger=ExitTrigger.STOP_LOSS,
        exit_price=Decimal("90"),
        realised_pnl=Decimal("-101.00"),
    )

    async def _closed() -> list[Position]:
        return [winner, loser]

    monkeypatch.setattr("zarabot.reporter.weekly.list_closed", _closed)
    monkeypatch.setattr("zarabot.reporter.weekly._LIMIT", 10**6)
    full = await build(START, END)
    assert "Exit-trigger distribution" in full
    monkeypatch.setattr("zarabot.reporter.weekly._LIMIT", len(full) - 1)
    trimmed = await build(START, END)
    assert "omitted" in trimmed.lower()
    assert "Exit-trigger distribution" not in trimmed


async def test_send_failure_alerts_and_does_not_raise(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append(text)
        raise NetworkError("network")

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    await send(NOW)
    assert calls


async def test_send_raising_does_not_emit_weekly_report_built(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _alert(text: str, urgent: bool = False) -> None:
        raise NetworkError("network")

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    with caplog.at_level(logging.INFO, logger="zarabot.reporter.weekly"):
        await send(NOW)
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "weekly_report_built"
    ]
    assert events == []


async def test_send_emits_weekly_report_built(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        sent.append(text)

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    with caplog.at_level(logging.INFO, logger="zarabot.reporter.weekly"):
        await send(NOW)
    assert sent
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "weekly_report_built"
    ]
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.INFO
    assert record.period_start == START.isoformat()
    assert record.period_end == END.isoformat()
    assert sent[0] not in caplog.text
    assert "token" not in caplog.text


async def test_event_fires_when_notifier_swallows_send_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Production `alert` never raises: §8 rule 13 retries, logs and swallows.

    So the production failure path is `alert` returning after a total Telegram
    outage. `weekly_report_built` names composition and hand-off, so it fires
    here. Delivery is `telegram_send_failed` absent, not this event present —
    and that event belongs to `telegram.notifier`, never to this module.
    """
    notifier_log = logging.getLogger("zarabot.telegram.notifier")

    async def _alert(text: str, urgent: bool = False) -> None:
        for attempt in range(1, 4):
            notifier_log.warning(
                "telegram_send_failed",
                extra={
                    "event": "telegram_send_failed",
                    "attempt": attempt,
                    "error": "network",
                },
            )

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    with caplog.at_level(logging.INFO):
        await send(NOW)
    built = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "weekly_report_built"
    ]
    assert len(built) == 1
    assert built[0].levelno == logging.INFO
    assert built[0].period_start == START.isoformat()
    assert built[0].period_end == END.isoformat()
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "telegram_send_failed"
    ]
    assert len(failures) == 3
    assert all(record.name == "zarabot.telegram.notifier" for record in failures)


# --- Narrow catches (issue #195). ---------------------------------------------
# `send` swallows what rule 12 (v1.75) licenses on this non-critical path -
# "Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error` ... Any
# other exception propagates and reaches rule 21's supervisor with its
# traceback" - plus rule 13's `TelegramError` from the `alert` leg.


async def test_send_swallows_a_database_error_from_build(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append(text)

    async def _closed() -> list[Position]:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    monkeypatch.setattr("zarabot.reporter.weekly.list_closed", _closed)
    await send(NOW)
    assert calls == ["Weekly report failed to send."]


async def test_send_propagates_a_non_database_exception_from_build(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: red against `except Exception`."""

    async def _closed() -> list[Position]:
        raise AttributeError("list_closed renamed")

    monkeypatch.setattr("zarabot.reporter.weekly.list_closed", _closed)
    with pytest.raises(AttributeError):
        await send(NOW)


async def test_send_propagates_a_non_telegram_exception_from_the_alert_leg(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A defect inside the notifier must reach rule 21, not be logged away."""

    async def _alert(text: str, urgent: bool = False) -> None:
        raise TypeError("alert signature changed")

    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    with pytest.raises(TypeError):
        await send(NOW)


async def test_send_propagates_a_defect_from_the_failure_alert_leg(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner catch is narrow too: only rule 13's class is swallowed there."""
    calls: list[str] = []

    async def _closed() -> list[Position]:
        raise aiosqlite.OperationalError("database is locked")

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append(text)
        raise TypeError("alert signature changed")

    monkeypatch.setattr("zarabot.reporter.weekly.list_closed", _closed)
    monkeypatch.setattr("zarabot.reporter.weekly.alert", _alert)
    with pytest.raises(TypeError):
        await send(NOW)
    assert calls == ["Weekly report failed to send."]
