"""Tests for zarabot.__main__ — derived from the process-entry contract."""

from __future__ import annotations

import signal
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.app.startup import AppContext, StartupError
from zarabot.config import Config
from zarabot.models import ReconciliationReport

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)


def _ctx() -> AppContext:
    return AppContext(
        config=Config(
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
        ),
        strategies=(),
        halt=None,
        reconciliation=ReconciliationReport(ran_at=NOW, adjustments=()),
    )


def test_main_returns_zero_after_start_and_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.__main__ import main

    calls: list[str] = []
    ctx = _ctx()

    async def _start() -> AppContext:
        calls.append("start")
        return ctx

    async def _run(received: AppContext) -> None:
        calls.append("run")
        assert received is ctx

    async def _shutdown(*_a: object, **_k: object) -> None:
        calls.append("shutdown")

    monkeypatch.setattr("zarabot.__main__.start", _start)
    monkeypatch.setattr("zarabot.__main__.run", _run)
    monkeypatch.setattr("zarabot.__main__.shutdown", _shutdown)
    assert main() == 0
    assert calls == ["start", "run"]


def test_startup_error_sleeps_then_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.__main__ import main

    slept: list[float] = []
    ran = False

    async def _start() -> AppContext:
        raise StartupError("bad config")

    async def _run(_ctx: AppContext) -> None:
        nonlocal ran
        ran = True

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("zarabot.__main__.start", _start)
    monkeypatch.setattr("zarabot.__main__.run", _run)
    monkeypatch.setattr("zarabot.__main__.asyncio.sleep", _sleep)
    assert main() != 0
    assert slept == [30]
    assert ran is False


def test_sigterm_and_sigint_are_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.__main__ import main

    installed: list[int] = []
    ctx = _ctx()

    async def _start() -> AppContext:
        return ctx

    async def _run(_ctx: AppContext) -> None:
        return None

    monkeypatch.setattr("zarabot.__main__.start", _start)
    monkeypatch.setattr("zarabot.__main__.run", _run)

    real_get = __import__("asyncio").get_running_loop

    def _get_loop() -> object:
        loop = real_get()
        original = loop.add_signal_handler

        def _add(sig: int, callback: object) -> None:
            installed.append(sig)
            original(sig, callback)

        loop.add_signal_handler = _add  # type: ignore[method-assign]
        return loop

    monkeypatch.setattr("zarabot.__main__.asyncio.get_running_loop", _get_loop)
    assert main() == 0
    assert signal.SIGTERM in installed
    assert signal.SIGINT in installed


def test_signal_routes_to_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.__main__ import main

    calls: list[int] = []
    ctx = _ctx()
    handlers: dict[int, object] = {}

    async def _start() -> AppContext:
        return ctx

    async def _run(_ctx: AppContext) -> None:
        callback = handlers[int(signal.SIGTERM)]
        assert callable(callback)
        result = callback()
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
        return

    async def _shutdown(received: AppContext, sig: int) -> None:
        calls.append(sig)
        assert received is ctx

    monkeypatch.setattr("zarabot.__main__.start", _start)
    monkeypatch.setattr("zarabot.__main__.run", _run)
    monkeypatch.setattr("zarabot.__main__.shutdown", _shutdown)

    real_get = __import__("asyncio").get_running_loop

    def _get_loop() -> object:
        loop = real_get()
        original = loop.add_signal_handler

        def _add(sig: int, callback: object) -> None:
            handlers[int(sig)] = callback
            original(sig, callback)

        loop.add_signal_handler = _add  # type: ignore[method-assign]
        return loop

    monkeypatch.setattr("zarabot.__main__.asyncio.get_running_loop", _get_loop)
    assert main() == 0
    assert signal.SIGTERM in calls or int(signal.SIGTERM) in calls
