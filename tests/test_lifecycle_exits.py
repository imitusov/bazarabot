"""Tests for zarabot.lifecycle.exits — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.config import Config
from zarabot.lifecycle.exits import evaluate
from zarabot.models import (
    ExitTrigger,
    Position,
    SessionInfo,
    StopProtection,
)

STOP = Decimal("95.00")
TARGET = Decimal("110.00")
INCREMENT = Decimal("0.01")
ENTRY = datetime(2026, 3, 13, 10, 0, tzinfo=UTC)  # Friday
SESSION_END = datetime(2026, 3, 16, 15, 50, tzinfo=UTC)  # Monday
SESSION_START = datetime(2026, 3, 16, 6, 50, tzinfo=UTC)
IN_WINDOW = SESSION_END - timedelta(minutes=10)
BEFORE_WINDOW = SESSION_END - timedelta(minutes=20)


def _config(max_holding_days: int = 3) -> Config:
    return Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=Decimal("100000"),
        position_size_pct=Decimal("10"),
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("10"),
        max_holding_days=max_holding_days,
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


def _session() -> SessionInfo:
    return SessionInfo(
        trade_date=date(2026, 3, 16),  # the Moscow date of SESSION_START
        start=SESSION_START,
        end=SESSION_END,
        is_trading_day=True,
    )


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 2,
        "lot_size": 10,
        "entry_price": Decimal("100.00"),
        "entry_at": ENTRY,
        "stop_price": STOP,
        "target_price": TARGET,
        "status": "OPEN",
        "adopted": False,
        "open_order_key": "11111111-1111-4111-8111-111111111111",
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


def test_unmeasured_age_suppresses_only_max_age() -> None:
    """A short count reads as a young position — the silent shape of #45. None
    cannot be mistaken for a measurement, and leaves stop and target working."""
    config = _config(max_holding_days=1)
    stale = _position()
    closing = _session()

    assert evaluate(stale, Decimal("100"), IN_WINDOW, closing, None, config) is None, (
        "age cannot fire when it cannot be measured"
    )
    assert (
        evaluate(stale, Decimal("100"), IN_WINDOW, closing, 5, config)
        is ExitTrigger.MAX_AGE
    ), "and does fire when it can"
    assert (
        evaluate(stale, Decimal("90"), IN_WINDOW, closing, None, config)
        is ExitTrigger.STOP_LOSS
    )
    assert (
        evaluate(stale, Decimal("120"), IN_WINDOW, closing, None, config)
        is ExitTrigger.TAKE_PROFIT
    )


def test_price_exactly_at_stop_triggers_stop_loss() -> None:
    assert (
        evaluate(_position(), STOP, IN_WINDOW, _session(), 0, _config())
        is ExitTrigger.STOP_LOSS
    )
    assert (
        evaluate(_position(), STOP + INCREMENT, IN_WINDOW, _session(), 0, _config())
        is None
    )


def test_price_exactly_at_target_triggers_take_profit() -> None:
    assert (
        evaluate(_position(), TARGET, IN_WINDOW, _session(), 0, _config())
        is ExitTrigger.TAKE_PROFIT
    )
    assert (
        evaluate(_position(), TARGET - INCREMENT, IN_WINDOW, _session(), 0, _config())
        is None
    )


def test_max_age_only_in_closing_window() -> None:
    assert (
        evaluate(_position(), Decimal("100"), IN_WINDOW, _session(), 3, _config())
        is ExitTrigger.MAX_AGE
    )
    assert (
        evaluate(_position(), Decimal("100"), BEFORE_WINDOW, _session(), 3, _config())
        is None
    )


def test_stop_loss_precedes_max_age() -> None:
    assert (
        evaluate(_position(), STOP, IN_WINDOW, _session(), 3, _config())
        is ExitTrigger.STOP_LOSS
    )


def test_weekend_does_not_age_a_friday_position() -> None:
    # Friday → Monday is one trading day elapsed, not three calendar days.
    assert (
        evaluate(_position(), Decimal("100"), IN_WINDOW, _session(), 1, _config())
        is None
    )


def test_adopted_position_ages_from_adoption_count() -> None:
    adopted = _position(adopted=True, strategy="ADOPTED", entry_at=ENTRY)
    assert (
        evaluate(adopted, Decimal("100"), IN_WINDOW, _session(), 3, _config())
        is ExitTrigger.MAX_AGE
    )


def test_evaluate_is_pure_and_repeatable() -> None:
    args = (_position(), STOP, IN_WINDOW, _session(), 0, _config())
    assert evaluate(*args) is ExitTrigger.STOP_LOSS
    assert evaluate(*args) is ExitTrigger.STOP_LOSS


def test_exchange_stop_does_not_return_stop_loss() -> None:
    protected = _position(
        stop_protection=StopProtection.EXCHANGE, stop_order_key="stop-1"
    )
    assert evaluate(protected, STOP, IN_WINDOW, _session(), 0, _config()) is None
    assert (
        evaluate(protected, TARGET, IN_WINDOW, _session(), 0, _config())
        is ExitTrigger.TAKE_PROFIT
    )


def test_naive_now_raises_value_error() -> None:
    naive = datetime(2026, 3, 16, 15, 40)  # noqa: DTZ001
    with pytest.raises(ValueError):
        evaluate(_position(), Decimal("100"), naive, _session(), 0, _config())
