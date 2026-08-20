"""Tests for zarabot.app.loops — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.app.startup import AppContext
from zarabot.broker.client import BrokerUnavailable
from zarabot.config import Config
from zarabot.models import (
    Candle,
    ExitTrigger,
    HaltReason,
    Instrument,
    PortfolioState,
    Position,
    ReconciliationReport,
    RejectionReason,
    RiskDecision,
    SessionInfo,
    Side,
    Signal,
    StopOrderRecord,
    StopProtection,
)

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
SESSION = SessionInfo(
    start=datetime(2026, 3, 16, 6, 50, tzinfo=UTC),
    end=datetime(2026, 3, 16, 15, 50, tzinfo=UTC),
    is_trading_day=True,
)


def _config() -> Config:
    return Config(
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


def _ctx(strategies: tuple[object, ...] = ()) -> AppContext:
    return AppContext(
        config=_config(),
        strategies=strategies,  # type: ignore[arg-type]
        halt=None,
        reconciliation=ReconciliationReport(ran_at=NOW, adjustments=()),
    )


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


def _make_instrument() -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


def _candle() -> Candle:
    return Candle(
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1,
    )


class _QuietStrategy:
    name = "quiet"
    lookback = 1

    def evaluate(self, ticker: str, candles: object, now: datetime) -> None:
        return None


class _BuyStrategy:
    name = "buyer"
    lookback = 1

    def evaluate(self, ticker: str, candles: object, now: datetime) -> Signal:
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=Decimal("100"),
        )


def _patch_defaults(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    import zarabot.app.loops as loops

    monkeypatch.setattr(loops, "now", lambda: NOW)
    monkeypatch.setattr(loops, "is_open", lambda moment: True)
    monkeypatch.setattr(loops, "current_session", lambda moment: SESSION)

    async def _none(*_a: object, **_k: object) -> None:
        return None

    async def _empty(*_a: object, **_k: object) -> list[object]:
        return []

    async def _price(figi: str) -> Decimal:
        calls.append(f"price:{figi}")
        return Decimal("100")

    async def _stops() -> list[StopOrderRecord]:
        calls.append("list_stop_orders")
        return []

    async def _portfolio() -> PortfolioState:
        calls.append("get_portfolio")
        return PortfolioState(cash=Decimal("100000"), positions=())

    async def _candles(
        tickers: list[str], lookback: int, now: datetime
    ) -> dict[str, list[Candle]]:
        calls.append("candles")
        return {ticker: [_candle()] for ticker in tickers}

    async def _loss(moment: datetime) -> Decimal:
        calls.append("daily_loss")
        return Decimal("0")

    async def _halted() -> bool:
        return False

    async def _schedule(days: int) -> list[SessionInfo]:
        return [SESSION]

    async def _resolve(moment: datetime) -> list[object]:
        calls.append("resolve")
        return []

    async def _cooldown(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "get_portfolio", _portfolio)
    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "is_halted", _halted)
    monkeypatch.setattr(loops, "get_trading_schedule", _schedule)
    monkeypatch.setattr(loops, "resolve_unfinished", _resolve)
    monkeypatch.setattr(loops, "list_open", _empty)
    monkeypatch.setattr(loops, "is_active", _cooldown)
    monkeypatch.setattr(loops, "record", _none)
    monkeypatch.setattr(loops, "alert", _none)
    monkeypatch.setattr(loops, "halt", _none)
    monkeypatch.setattr(loops, "open_position", _none)
    monkeypatch.setattr(loops, "close_position", _none)
    monkeypatch.setattr(loops, "close_executed_stop", _none)


async def test_session_closed_makes_no_market_data_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []

    async def _price(figi: str) -> Decimal:
        calls.append(f"price:{figi}")
        return Decimal("100")

    async def _candles(
        tickers: list[str], lookback: int, now: datetime
    ) -> dict[str, list[Candle]]:
        calls.append("candles")
        return {}

    async def _stops() -> list[object]:
        calls.append("stops")
        return []

    import zarabot.app.loops as loops

    monkeypatch.setattr(loops, "now", lambda: NOW)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    await trading_cycle(_ctx())
    assert calls == []


async def test_local_stop_loss_submits_close_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    closed: list[tuple[int, ExitTrigger]] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(stop_protection=StopProtection.LOCAL)

    async def _open() -> list[Position]:
        return [position]

    async def _price(figi: str) -> Decimal:
        calls.append(f"price:{figi}")
        return Decimal("95")

    async def _close(pos: Position, trigger: ExitTrigger) -> Position:
        closed.append((pos.id, trigger))
        return pos

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "close_position", _close)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert closed == [(1, ExitTrigger.STOP_LOSS)]


async def test_exchange_executed_stop_closes_without_selling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    executed: list[tuple[int, Decimal]] = []
    sold: list[object] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key="stop-key-1",
    )

    async def _open() -> list[Position]:
        return [position]

    async def _price(figi: str) -> Decimal:
        return Decimal("94")

    async def _stops() -> list[StopOrderRecord]:
        return []

    async def _executed(pos: Position, fill: Decimal) -> Position:
        executed.append((pos.id, fill))
        return pos

    async def _close(*_a: object, **_k: object) -> None:
        sold.append("sold")

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "close_executed_stop", _executed)
    monkeypatch.setattr(loops, "close_position", _close)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert executed == [(1, Decimal("94"))]
    assert sold == []


async def test_exits_run_while_halted_and_entries_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    closed: list[ExitTrigger] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(target_price=Decimal("100"))

    async def _open() -> list[Position]:
        return [position]

    async def _price(figi: str) -> Decimal:
        return Decimal("110")

    async def _halted() -> bool:
        return True

    async def _close(pos: Position, trigger: ExitTrigger) -> Position:
        closed.append(trigger)
        return pos

    async def _candles(
        tickers: list[str], lookback: int, now: datetime
    ) -> dict[str, list[Candle]]:
        calls.append("candles")
        return {}

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "is_halted", _halted)
    monkeypatch.setattr(loops, "close_position", _close)
    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert ExitTrigger.TAKE_PROFIT in closed
    assert "candles" not in calls


async def test_daily_loss_limit_halts_before_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    halted: list[HaltReason] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _loss(moment: datetime) -> Decimal:
        calls.append("daily_loss")
        return Decimal("5")

    halted_flag = False

    async def _is_halted() -> bool:
        return halted_flag

    async def _do_halt(reason: HaltReason, detail: str, at: datetime) -> None:
        nonlocal halted_flag
        halted_flag = True
        halted.append(reason)

    async def _candles(
        tickers: list[str], lookback: int, now: datetime
    ) -> dict[str, list[Candle]]:
        calls.append("candles")
        return {}

    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "halt", _do_halt)
    monkeypatch.setattr(loops, "is_halted", _is_halted)
    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert HaltReason.DAILY_LOSS_LIMIT in halted
    assert "candles" not in calls
    assert calls.index("daily_loss") >= 0


async def test_approved_signal_is_recorded_and_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    recorded: list[tuple[str, bool]] = []
    opened: list[int] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _fetch_instrument(ticker: str) -> Instrument:
        return _make_instrument()

    async def _record(signal: Signal, decision: RiskDecision) -> None:
        recorded.append((signal.ticker, decision.approved))

    async def _open(signal: Signal, lots: int, instrument: Instrument) -> Position:
        opened.append(lots)
        return _position()

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(loops, "record", _record)
    monkeypatch.setattr(loops, "open_position", _open)
    monkeypatch.setattr(
        loops,
        "check",
        lambda *a, **k: RiskDecision(approved=True, lots=3, reason=None),
    )
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert recorded == [("SBER", True)]
    assert opened == [3]


async def test_rejected_signal_is_recorded_and_not_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    recorded: list[RejectionReason | None] = []
    opened: list[object] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _fetch_instrument(ticker: str) -> Instrument:
        return _make_instrument()

    async def _record(signal: Signal, decision: RiskDecision) -> None:
        recorded.append(decision.reason)

    async def _open(*_a: object, **_k: object) -> None:
        opened.append("open")

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(loops, "record", _record)
    monkeypatch.setattr(loops, "open_position", _open)
    monkeypatch.setattr(
        loops,
        "check",
        lambda *a, **k: RiskDecision(
            approved=False, lots=None, reason=RejectionReason.MAX_POSITIONS
        ),
    )
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert recorded == [RejectionReason.MAX_POSITIONS]
    assert opened == []


async def test_three_consecutive_market_data_failures_alert_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    alerts: list[str] = []
    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False

    async def _price(figi: str) -> Decimal:
        raise BrokerUnavailable("down")

    async def _open() -> list[Position]:
        return [_position()]

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "alert", _alert)
    for _ in range(3):
        await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1


async def test_run_one_task_failure_does_not_kill_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import run

    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _trade(ctx: AppContext) -> None:
        calls.append("trade")
        raise RuntimeError("cycle failed")

    async def _backup(db_path: Path, backup_dir: Path) -> Path:
        calls.append("backup")
        return Path("backups/x.db")

    async def _prune(backup_dir: Path, retention_days: int) -> int:
        calls.append("prune")
        return 0

    async def _send(moment: datetime) -> None:
        calls.append("weekly")

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append(f"alert:{text[:20]}")

    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "backup_run", _backup)
    monkeypatch.setattr(loops, "prune", _prune)
    monkeypatch.setattr(loops, "send_report", _send)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)

    async def _yield(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(loops.asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(30):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "trade" in calls
    assert any(item.startswith("alert:") for item in calls)


async def test_market_data_failure_logs_warning_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False

    async def _price(figi: str) -> Decimal:
        raise BrokerUnavailable("down")

    async def _open() -> list[Position]:
        return [_position()]

    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_open", _open)
    with caplog.at_level(logging.WARNING):
        await trading_cycle(_ctx())
    assert caplog.records
    assert loops._market_failures == 1
