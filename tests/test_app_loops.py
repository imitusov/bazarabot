"""Tests for zarabot.app.loops — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.app.startup import AppContext
from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderRejected,
    PriceRejected,
)
from zarabot.clock import moscow_date
from zarabot.config import Config
from zarabot.db.snapshots import DailySnapshot
from zarabot.execution.orders import ExitFailed
from zarabot.models import (
    Candle,
    ExitTrigger,
    HaltReason,
    Instrument,
    OrderRecord,
    OrderStatus,
    PortfolioState,
    Position,
    ReconciliationReport,
    RejectionReason,
    RiskDecision,
    SessionInfo,
    Side,
    Signal,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
    TradingCalendar,
)

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
SESSION = SessionInfo(
    start=datetime(2026, 3, 16, 6, 50, tzinfo=UTC),
    end=datetime(2026, 3, 16, 15, 50, tzinfo=UTC),
    is_trading_day=True,
)
# Before the session opens: a process running from here is present at the open.
PRE_OPEN = datetime(2026, 3, 16, 6, 0, tzinfo=UTC)
# Well after the open: a process whose first cycle lands here missed it.
LATE = datetime(2026, 3, 16, 11, 0, tzinfo=UTC)


def _config(watchlist: tuple[str, ...] = ("SBER",)) -> Config:
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
        max_holding_days=3,
        max_open_positions=10,
        reentry_cooldown_minutes=120,
        daily_loss_limit_pct=Decimal("5"),
        watchlist=watchlist,
        enabled_strategies=("ma_crossover",),
        ml_model_path=None,
        poll_interval_seconds=60,
        db_path=Path("zarabot.db"),
        backup_dir=Path("backups"),
        log_level="INFO",
        tz="Europe/Moscow",
    )


def _ctx(
    strategies: tuple[object, ...] = (), watchlist: tuple[str, ...] = ("SBER",)
) -> AppContext:
    return AppContext(
        config=_config(watchlist=watchlist),
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


def _patch_defaults(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> set[tuple[str, str]]:
    """Patch the module's collaborators. Returns the in-memory `job_runs`
    record, which tests seed to stand for a job already done this period."""
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

    async def _resolve(moment: datetime) -> list[object]:
        calls.append("resolve")
        return []

    async def _backfill(since: datetime, until: datetime) -> int:
        calls.append("backfill")
        return 0

    async def _cooldown(*_a: object, **_k: object) -> bool:
        return False

    async def _refresh(days: int) -> None:
        calls.append("schedule_refresh")

    class _IdleTelegram:
        async def initialize(self) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def run_polling(self, *args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "get_portfolio", _portfolio)
    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "is_halted", _halted)
    monkeypatch.setattr(loops, "resolve_unfinished", _resolve)
    monkeypatch.setattr(loops, "list_open", _empty)
    monkeypatch.setattr(loops, "is_active", _cooldown)
    monkeypatch.setattr(loops, "record", _none)
    monkeypatch.setattr(loops, "alert", _none)
    monkeypatch.setattr(loops, "halt", _none)
    monkeypatch.setattr(loops, "open_position", _none)
    monkeypatch.setattr(loops, "close_position", _none)
    monkeypatch.setattr(loops, "close_executed_stop", _none)
    monkeypatch.setattr(loops, "backfill", _backfill)
    monkeypatch.setattr(loops, "refresh", _refresh)
    monkeypatch.setattr(loops, "build_application", _IdleTelegram)
    monkeypatch.setattr(loops, "cache_exhausted", lambda moment: False)
    monkeypatch.setattr(loops, "calendar", lambda: TradingCalendar(sessions=(SESSION,)))

    runs: set[tuple[str, str]] = set()

    async def _has_run(job: str, period_key: str) -> bool:
        return (job, period_key) in runs

    async def _mark_run(job: str, period_key: str, ran_at: datetime) -> None:
        runs.add((job, period_key))

    monkeypatch.setattr(loops, "has_run", _has_run)
    monkeypatch.setattr(loops, "mark_run", _mark_run)
    monkeypatch.setattr(loops, "covers", lambda day: True)
    loops._age_unmeasurable_alerted = False
    loops._cache_exhausted_alerted = False
    loops._price_rejected_alerted = False
    loops._snapshot_on = None
    loops._first_cycle_at = None
    loops._loss_unmeasurable_alerted = False
    loops._entries_stopped = False
    return runs


def _snapshot(opening: Decimal) -> DailySnapshot:
    return DailySnapshot(
        trade_date=moscow_date(NOW),
        opening_equity=opening,
        closing_equity=None,
        cash=opening,
        realised_pnl=Decimal(0),
        unrealised_pnl=Decimal(0),
        open_positions=0,
        orders_placed=0,
        benchmark_value=None,
    )


def _patch_pnl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    equity: Decimal | Exception = Decimal("100000"),
    rows: list[DailySnapshot] | None = None,
    written: list[DailySnapshot] | None = None,
) -> None:
    """Patch the P&L collaborators step 4 uses. `pnl` and `db.snapshots` are
    other modules' code; this module is under test, not theirs."""
    import zarabot.app.loops as loops

    async def _equity() -> Decimal:
        if isinstance(equity, Exception):
            raise equity
        return equity

    async def _rows(start: date, end: date) -> list[DailySnapshot]:
        return list(rows or [])

    async def _write(snapshot: DailySnapshot) -> None:
        if written is not None:
            written.append(snapshot)

    monkeypatch.setattr(loops, "bot_equity", _equity)
    monkeypatch.setattr(loops, "list_for_period", _rows)
    monkeypatch.setattr(loops, "write_daily", _write)


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

    async def _executed(pos: Position, fill: OrderRecord) -> Position:
        executed.append((pos.id, fill.filled_price or Decimal("0")))
        return pos

    async def _close(*_a: object, **_k: object) -> None:
        sold.append("sold")

    async def _fills(since: object, until: object) -> dict[str, OrderRecord]:
        return {"broker-stop": _broker_fill(Decimal("95.00"))}

    async def _active(position_id: int) -> StopOrderRecord | None:
        return _our_stop()

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "get_executed_stop_fills", _fills)
    monkeypatch.setattr(loops, "active_for_position", _active)
    monkeypatch.setattr(loops, "close_executed_stop", _executed)
    monkeypatch.setattr(loops, "close_position", _close)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    # The broker says 95.00; the quote says 94. Before v1.28 this asserted 94.
    assert executed == [(1, Decimal("95.00"))]
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


async def test_one_price_rejected_leaves_other_positions_evaluated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    closed: list[tuple[int, ExitTrigger]] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    sber = _position(id=1, ticker="SBER", figi="BBG000SBER01")
    gazp = _position(id=2, ticker="GAZP", figi="BBG000GAZP01")

    async def _open() -> list[Position]:
        return [sber, gazp]

    async def _price(figi: str) -> Decimal:
        if figi == sber.figi:
            raise PriceRejected("stale")
        return Decimal("110")

    async def _close(pos: Position, trigger: ExitTrigger) -> Position:
        closed.append((pos.id, trigger))
        return pos

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "close_position", _close)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert closed == [(2, ExitTrigger.TAKE_PROFIT)]
    assert loops._market_failures == 0


async def test_all_prices_rejected_alerts_once_naming_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    alerts: list[str] = []
    closed: list[object] = []
    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    first = _position(id=1, ticker="SBER", figi="BBG000SBER01")
    second = _position(id=2, ticker="GAZP", figi="BBG000GAZP01")

    async def _open() -> list[Position]:
        return [first, second]

    async def _price(figi: str) -> Decimal:
        raise PriceRejected("unusable")

    async def _close(*_a: object, **_k: object) -> None:
        closed.append("closed")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "close_position", _close)
    monkeypatch.setattr(loops, "alert", _alert)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert closed == []
    assert len(alerts) == 1
    assert "2" in alerts[0]
    assert loops._market_failures == 0


async def test_consecutive_price_rejections_alert_once_until_rearmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    alerts: list[str] = []
    calls: list[str] = []
    reject = True
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    first = _position(id=1, ticker="SBER", figi="BBG000SBER01")
    second = _position(id=2, ticker="GAZP", figi="BBG000GAZP01")

    async def _open() -> list[Position]:
        return [first, second]

    async def _price(figi: str) -> Decimal:
        if reject:
            raise PriceRejected("unusable")
        return Decimal("110")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "alert", _alert)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    assert "2" in alerts[0]
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    reject = False
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    reject = True
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 2
    assert "2" in alerts[1]


async def test_duplicate_ticker_in_one_pass_opens_one_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard was doing its job; the caller routed its success through the
    crash path — a spurious alert and the rest of the pass abandoned (#24)."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    opened: list[str] = []
    recorded: list[tuple[str, RiskDecision]] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _open(signal: Signal, lots: int, instrument: Instrument) -> Position:
        opened.append(signal.ticker)
        return _position(ticker=signal.ticker)

    async def _record(signal: Signal, decision: RiskDecision) -> None:
        recorded.append((signal.ticker, decision))

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "open_position", _open)
    monkeypatch.setattr(loops, "record", _record)
    monkeypatch.setattr(loops, "alert", _alert)

    async def _fetch_instrument(ticker: str) -> Instrument:
        return _make_instrument()

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(
        loops,
        "check",
        lambda *a, **k: RiskDecision(approved=True, lots=3, reason=None),
    )

    ctx = _ctx(strategies=(_BuyStrategy(), _BuyStrategy()))
    await trading_cycle(ctx)

    assert opened == ["SBER"]
    assert not [text for text in alerts if "crash" in text.lower()]
    duplicates = [
        decision
        for ticker, decision in recorded
        if not decision.approved and decision.reason is RejectionReason.DUPLICATE_TICKER
    ]
    assert len(duplicates) == 1


@pytest.mark.parametrize("failure", ["position_state", "duplicate_order"])
async def test_a_refused_entry_does_not_abandon_the_rest_of_the_pass(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PositionStateError and DuplicateOrderError are ordinary outcomes beside
    OrderRejected; only the last had a branch, so a correct refusal reached the
    supervisor and skipped every remaining ticker (#24)."""
    from zarabot.app.loops import trading_cycle
    from zarabot.db.orders import DuplicateOrderError
    from zarabot.db.positions import PositionStateError

    raised: Exception = (
        PositionStateError("dup")
        if failure == "position_state"
        else DuplicateOrderError("dup")
    )
    calls: list[str] = []
    attempted: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _open(signal: Signal, lots: int, instrument: Instrument) -> Position:
        attempted.append(signal.ticker)
        raise raised

    monkeypatch.setattr(loops, "open_position", _open)

    async def _fetch_instrument(ticker: str) -> Instrument:
        return _make_instrument()

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(
        loops,
        "check",
        lambda *a, **k: RiskDecision(approved=True, lots=3, reason=None),
    )

    ctx = _ctx(strategies=(_BuyStrategy(),), watchlist=("SBER", "GAZP"))
    await trading_cycle(ctx)
    assert attempted == ["SBER", "GAZP"]


def test_loops_cannot_fetch_a_trading_schedule() -> None:
    """A fourteen-day schedule was re-fetched once a minute for data
    market.session already holds and refreshes daily (#19). The strongest form
    of "zero calls per cycle" is that the module cannot make one at all."""
    import zarabot.app.loops as loops

    assert not hasattr(loops, "get_trading_schedule")
    source = Path(loops.__file__).read_text()
    assert "get_trading_schedule" not in source


async def test_uncovered_entry_suppresses_max_age_and_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An age the recorded calendar cannot reach must not be passed as a short
    number — that reads as a young position, which is what #45 was."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    seen: list[int | None] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(stop_protection=StopProtection.LOCAL)

    async def _open() -> list[Position]:
        return [position]

    def _evaluate(
        pos: Position,
        price: Decimal,
        moment: datetime,
        session: SessionInfo,
        days: int | None,
        config: Config,
    ) -> ExitTrigger | None:
        seen.append(days)
        return None

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "covers", lambda day: False)
    monkeypatch.setattr(loops, "evaluate", _evaluate)
    monkeypatch.setattr(loops, "alert", _alert)

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert seen == [None]
    assert len(alerts) == 1
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1, "latched, like every other alert in this module"


async def test_unmeasurable_age_alerts_once_until_a_clean_cycle_rearms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set and never reset means one alert per process, so a second occurrence
    after recovery is silent. That is #32, in a second module (#48)."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(stop_protection=StopProtection.LOCAL)
    covered = True

    async def _open() -> list[Position]:
        return [position]

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "covers", lambda day: covered)
    monkeypatch.setattr(loops, "alert", _alert)

    covered = False
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    assert "1" in alerts[0], "the count is named"
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1, "latched"
    covered = True
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1, "a clean cycle is silent"
    covered = False
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 2, "re-armed by the clean cycle"


async def test_one_measurable_position_does_not_rearm_the_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole cycle is the unit, so a covered position cannot clear a
    warning that an uncovered one still needs."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    sber = _position(id=1, ticker="SBER", figi="BBG000SBER01")
    gazp = _position(id=2, ticker="GAZP", figi="BBG000GAZP01")

    async def _open() -> list[Position]:
        return [sber, gazp]

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    # SBER's entry is covered, GAZP's is not, on every cycle.
    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "covers", lambda day: False)
    monkeypatch.setattr(loops, "alert", _alert)

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1, "still latched while any position is unmeasurable"


async def test_covered_entry_passes_the_measured_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    seen: list[int | None] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(stop_protection=StopProtection.LOCAL)

    async def _open() -> list[Position]:
        return [position]

    def _evaluate(
        pos: Position,
        price: Decimal,
        moment: datetime,
        session: SessionInfo,
        days: int | None,
        config: Config,
    ) -> ExitTrigger | None:
        seen.append(days)
        return None

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "evaluate", _evaluate)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert seen == [0]


async def test_max_age_uses_the_cached_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calendar handed to lifecycle.exits comes from market.session."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    seen: list[TradingCalendar] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    marker = TradingCalendar(sessions=(SESSION,))
    monkeypatch.setattr(loops, "calendar", lambda: marker)
    position = _position(stop_protection=StopProtection.LOCAL)

    async def _open() -> list[Position]:
        return [position]

    def _days(start: datetime, end: datetime, cal: TradingCalendar) -> int:
        seen.append(cal)
        return 0

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "trading_days_between", _days)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert seen == [marker]


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

    sunday_noon = datetime(2026, 3, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "backup_run", _backup)
    monkeypatch.setattr(loops, "prune", _prune)
    monkeypatch.setattr(loops, "send_report", _send)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "now", lambda: sunday_noon)

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(50):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "trade" in calls
    assert "backup" in calls
    assert "weekly" in calls
    assert calls.index("backfill") < calls.index("weekly")
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


async def test_live_exchange_stop_is_not_closed_as_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    executed: list[object] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key="stop-key-1",
    )

    async def _open() -> list[Position]:
        return [position]

    async def _stops() -> list[StopOrderRecord]:
        return [
            StopOrderRecord(
                key="stop-key-1",
                stop_order_id="broker-stop",
                position_id=1,
                ticker="SBER",
                lots=2,
                stop_price=Decimal("95"),
                status=StopOrderStatus.ACTIVE,
                created_at=NOW,
                settled_at=None,
            )
        ]

    async def _executed(*_a: object, **_k: object) -> None:
        executed.append("closed")

    async def _no_fills(since: object, until: object) -> dict[str, OrderRecord]:
        return {}

    async def _active(position_id: int) -> StopOrderRecord | None:
        return _our_stop()

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "get_executed_stop_fills", _no_fills)
    monkeypatch.setattr(loops, "active_for_position", _active)
    monkeypatch.setattr(loops, "close_executed_stop", _executed)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert executed == []


async def test_missing_stop_while_still_held_does_not_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    executed: list[object] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key="stop-key-1",
    )

    async def _open() -> list[Position]:
        return [position]

    async def _portfolio() -> PortfolioState:
        return PortfolioState(cash=Decimal("1000"), positions=(position,))

    async def _executed(*_a: object, **_k: object) -> None:
        executed.append("closed")

    async def _no_fills(since: object, until: object) -> dict[str, OrderRecord]:
        return {}

    async def _active(position_id: int) -> StopOrderRecord | None:
        return _our_stop()

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_portfolio", _portfolio)
    monkeypatch.setattr(loops, "get_executed_stop_fills", _no_fills)
    monkeypatch.setattr(loops, "active_for_position", _active)
    monkeypatch.setattr(loops, "close_executed_stop", _executed)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert executed == []


async def test_rejected_entry_alerts_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _fetch_instrument(ticker: str) -> Instrument:
        return _make_instrument()

    async def _open(*_a: object, **_k: object) -> None:
        raise OrderRejected("max lots")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(loops, "open_position", _open)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(
        loops,
        "check",
        lambda *a, **k: RiskDecision(approved=True, lots=1, reason=None),
    )
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert alerts


async def test_missing_instrument_skips_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    opened: list[object] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _fetch_instrument(ticker: str) -> Instrument:
        raise InstrumentNotFound("gone")

    async def _open(*_a: object, **_k: object) -> None:
        opened.append("open")

    monkeypatch.setattr(loops, "get_instrument", _fetch_instrument)
    monkeypatch.setattr(loops, "open_position", _open)
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    assert opened == []


async def test_exit_failure_is_retried_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(target_price=Decimal("100"))

    async def _open() -> list[Position]:
        return [position]

    async def _price(figi: str) -> Decimal:
        return Decimal("110")

    async def _close(pos: Position, trigger: ExitTrigger) -> Position:
        raise ExitFailed("broker down")

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "get_last_price", _price)
    monkeypatch.setattr(loops, "close_position", _close)
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))


async def test_rollover_backfills_without_reading_the_daily_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`daily_loss_pct` no longer writes, so calling it here bought nothing.

    It reads the day's opening snapshot; at rollover there is none yet, so the
    call would only fire `pnl`'s reconstruction alert. The snapshot is written
    by the first cycle of the session, in `trading_cycle` step 4.
    """
    from zarabot.app.loops import run

    calls: list[str] = []
    windows: list[tuple[datetime, datetime]] = []
    runs = _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _trade(_ctx: AppContext) -> None:
        return None

    async def _loss(moment: datetime) -> Decimal:
        calls.append("rollover_loss")
        return Decimal("0")

    async def _bf(since: datetime, until: datetime) -> int:
        calls.append("backfill")
        windows.append((since, until))
        return 0

    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "backfill", _bf)
    monkeypatch.setattr(loops, "is_open", lambda moment: True)
    monkeypatch.setattr(loops, "now", lambda: NOW)
    today = moscow_date(NOW).isoformat()
    runs.update(
        {("backup", today), ("heartbeat", today), ("weekly_report", "2026-03-16")}
    )

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(50):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "backfill" in calls
    assert "rollover_loss" not in calls
    assert windows
    assert windows[0] == (NOW - timedelta(days=7), NOW)


async def test_weekly_backfills_immediately_before_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import run

    calls: list[str] = []
    runs = _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _trade(_ctx: AppContext) -> None:
        return None

    async def _bf(since: datetime, until: datetime) -> int:
        calls.append("backfill")
        return 0

    async def _send(moment: datetime) -> None:
        calls.append("weekly")

    sunday_noon = datetime(2026, 3, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "backfill", _bf)
    monkeypatch.setattr(loops, "send_report", _send)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "now", lambda: sunday_noon)
    day = moscow_date(sunday_noon).isoformat()
    runs.update({("backup", day), ("heartbeat", day), ("rollover", day)})

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(50):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls.index("backfill") < calls.index("weekly")


async def test_restart_after_the_report_hour_still_sends_the_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact-hour condition lost the week entirely — no report, no alert,
    no record — against acceptance criterion 9 (#27)."""
    from zarabot.app.loops import run

    calls: list[str] = []
    runs = _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _trade(_ctx: AppContext) -> None:
        return None

    async def _send(moment: datetime) -> None:
        calls.append("weekly")

    # Sunday 15:00 MSK: two hours after the window the old condition required.
    sunday_late = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)
    day = moscow_date(sunday_late).isoformat()
    runs.update({("backup", day), ("heartbeat", day), ("rollover", day)})

    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "send_report", _send)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "now", lambda: sunday_late)

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(50):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls.count("weekly") == 1


async def test_a_job_already_recorded_does_not_run_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three restarts in a day produced three heartbeats, because the guard was
    a module global that every restart cleared (#27)."""
    from zarabot.app.loops import run

    calls: list[str] = []
    runs = _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _trade(_ctx: AppContext) -> None:
        return None

    async def _send(moment: datetime) -> None:
        calls.append("weekly")

    async def _backup(*_a: object, **_k: object) -> Path:
        calls.append("backup")
        return Path("x")

    sunday_noon = datetime(2026, 3, 22, 9, 0, tzinfo=UTC)
    day = moscow_date(sunday_noon).isoformat()
    runs.update(
        {
            ("backup", day),
            ("heartbeat", day),
            ("rollover", day),
            ("weekly_report", "2026-03-16"),
        }
    )

    monkeypatch.setattr(loops, "trading_cycle", _trade)
    monkeypatch.setattr(loops, "send_report", _send)
    monkeypatch.setattr(loops, "backup_run", _backup)
    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "now", lambda: sunday_noon)

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx()))
    for _ in range(50):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "weekly" not in calls
    assert "backup" not in calls
    assert not [item for item in calls if item.startswith("heartbeat")]


async def test_stop_entries_suppresses_entries_but_not_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown's contract said it stops accepting new signals and nothing
    implemented that half; the loop kept cycling for the whole drain (#21)."""
    from zarabot.app.loops import stop_entries, trading_cycle

    calls: list[str] = []
    closed: list[int] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    position = _position(stop_protection=StopProtection.LOCAL)

    async def _open() -> list[Position]:
        return [position]

    async def _close(pos: Position, trigger: ExitTrigger) -> Position:
        closed.append(pos.id)
        return pos

    async def _price(figi: str) -> Decimal:
        return Decimal("120")  # above the target, so an exit is due

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "close_position", _close)
    monkeypatch.setattr(loops, "get_last_price", _price)

    stop_entries()
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))

    assert "candles" not in calls, "no entry evaluation after a stop request"
    assert closed == [position.id], "exits still run"


async def test_run_starts_telegram_listener_and_halt_stops_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import run, trading_cycle
    from zarabot.telegram import commands as commands_mod

    calls: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    listener_started = asyncio.Event()
    halted_flag = False

    class _App:
        async def initialize(self) -> None:
            return None

        async def start(self) -> None:
            listener_started.set()

        async def stop(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def run_polling(self, *args: object, **kwargs: object) -> None:
            listener_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(loops, "build_application", _App)

    async def _is_halted() -> bool:
        return halted_flag

    async def _persist(
        reason: HaltReason,
        detail: str,
        at: datetime,
        daily_loss_pct: Decimal | None = None,
    ) -> None:
        nonlocal halted_flag
        halted_flag = True

    async def _loss(_at: datetime) -> Decimal:
        return Decimal("0")

    async def _reply(update: object, text: str) -> None:
        return None

    monkeypatch.setattr(loops, "is_halted", _is_halted)
    monkeypatch.setattr(commands_mod, "persist_halt", _persist)
    monkeypatch.setattr(commands_mod, "daily_loss_pct", _loss)
    monkeypatch.setattr(commands_mod, "_authorised", lambda update: True)
    monkeypatch.setattr(commands_mod, "_reply", _reply)
    monkeypatch.setattr(commands_mod, "now", lambda: NOW)

    async def _trade(_ctx: AppContext) -> None:
        return None

    monkeypatch.setattr(loops, "trading_cycle", _trade)
    loops._backed_up_on = NOW.date()
    loops._heartbeat_on = NOW.date()
    loops._weekly_on = NOW.date()
    loops._rolled_on = NOW.date()

    real_sleep = asyncio.sleep

    async def _yield(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _yield)
    task = asyncio.create_task(run(_ctx(strategies=(_BuyStrategy(),))))
    for _ in range(80):
        await real_sleep(0)
        if listener_started.is_set():
            break
    assert listener_started.is_set()

    class _Chat:
        id = 1

    class _Message:
        async def reply_text(self, text: str, **kwargs: object) -> None:
            return None

    class _Update:
        effective_chat = _Chat()
        message = _Message()

    await commands_mod.halt(_Update(), None)  # type: ignore[arg-type]
    monkeypatch.setattr(loops, "trading_cycle", trading_cycle)

    candle_calls: list[str] = []

    async def _candles(
        tickers: list[str], lookback: int, now: datetime
    ) -> dict[str, list[Candle]]:
        candle_calls.append("candles")
        return {}

    monkeypatch.setattr(loops, "candles_for_watchlist", _candles)
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert candle_calls == []
    assert halted_flag is True


async def test_exhausted_schedule_cache_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    async def _price(figi: str) -> Decimal:
        calls.append(f"price:{figi}")
        return Decimal("100")

    monkeypatch.setattr(loops, "is_open", lambda moment: False)
    monkeypatch.setattr(loops, "cache_exhausted", lambda moment: True)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "get_last_price", _price)
    await trading_cycle(_ctx())
    assert alerts
    assert calls == []


# ---------------------------------------------------------------------------
# #5: an exit is confirmed by the broker, never inferred from two absences.
# ---------------------------------------------------------------------------


def _exchange_position(**overrides: object) -> Position:
    return _position(
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key="stop-key-1",
        **overrides,
    )


def _our_stop(stop_order_id: str | None = "broker-stop") -> StopOrderRecord:
    return StopOrderRecord(
        key="stop-key-1",
        stop_order_id=stop_order_id,
        position_id=1,
        ticker="SBER",
        lots=2,
        stop_price=Decimal("95"),
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )


def _broker_fill(price: Decimal = Decimal("95.00")) -> OrderRecord:
    return OrderRecord(
        key="exch-1",
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=price,
        commission=Decimal("1.25"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )


def _arrange_stop_detection(
    monkeypatch: pytest.MonkeyPatch,
    position: Position,
    standing: list[StopOrderRecord],
    fills: dict[str, OrderRecord],
    booked: list[object],
    alerts: list[str],
) -> None:
    import zarabot.app.loops as loops

    async def _open() -> list[Position]:
        return [position]

    async def _stops() -> list[StopOrderRecord]:
        return standing

    async def _portfolio() -> PortfolioState:
        return PortfolioState(cash=Decimal("1000"), positions=())

    async def _fills(since: object, until: object) -> dict[str, OrderRecord]:
        return fills

    async def _active(position_id: int) -> StopOrderRecord | None:
        return _our_stop()

    async def _booked(pos: Position, fill: OrderRecord) -> Position:
        booked.append((pos.id, fill.filled_price))
        return pos

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "list_open", _open)
    monkeypatch.setattr(loops, "list_stop_orders", _stops)
    monkeypatch.setattr(loops, "get_portfolio", _portfolio)
    monkeypatch.setattr(loops, "get_executed_stop_fills", _fills)
    monkeypatch.setattr(loops, "active_for_position", _active)
    monkeypatch.setattr(loops, "close_executed_stop", _booked)
    monkeypatch.setattr(loops, "alert", _alert)


async def test_two_absences_alone_do_not_close_a_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5: stop gone and ticker gone is a discrepancy, not an exit."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    booked: list[object] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    _arrange_stop_detection(monkeypatch, _exchange_position(), [], {}, booked, alerts)

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))

    assert booked == []
    assert any("stop" in text.lower() for text in alerts)


async def test_confirmed_execution_closes_at_the_brokers_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    booked: list[object] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    _arrange_stop_detection(
        monkeypatch,
        _exchange_position(),
        [],
        {"broker-stop": _broker_fill(Decimal("95.00"))},
        booked,
        alerts,
    )

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))

    assert booked == [(1, Decimal("95.00"))]


async def test_match_is_on_the_persisted_broker_id_not_our_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local key is a UUID the broker may never echo back."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    booked: list[object] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    _arrange_stop_detection(
        monkeypatch,
        _exchange_position(),
        [],
        {"stop-key-1": _broker_fill()},
        booked,
        alerts,
    )

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))

    assert booked == []


async def test_live_stop_keyed_only_by_broker_id_is_not_reported_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5 verification 3: order_request_id absent, so our UUID matches nothing.

    list_stop_orders then keys the stop by the broker's own id. The stop is
    alive; treating it as missing would alert on a healthy position every cycle.
    """
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    booked: list[object] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    standing = [
        StopOrderRecord(
            key="broker-stop",  # no order_request_id: keyed by the broker id
            stop_order_id="broker-stop",
            position_id=1,
            ticker="SBER",
            lots=2,
            stop_price=Decimal("95"),
            status=StopOrderStatus.ACTIVE,
            created_at=NOW,
            settled_at=None,
        )
    ]
    _arrange_stop_detection(
        monkeypatch, _exchange_position(), standing, {}, booked, alerts
    )

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))

    assert booked == []
    assert [a for a in alerts if "no confirmed execution" in a] == []


# ---------------------------------------------------------------------------
# #9: the day's opening snapshot, and what happens when the loss cannot be
# measured. `pnl.daily_loss_pct` reads the baseline and writes nothing, so
# step 4 of the cycle owns the write.
# ---------------------------------------------------------------------------


async def test_first_cycle_of_the_session_writes_the_opening_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The baseline is the session open, not whenever the process first asked."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    written: list[DailySnapshot] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    clock = {"now": PRE_OPEN}
    monkeypatch.setattr(loops, "now", lambda: clock["now"])
    monkeypatch.setattr(
        loops,
        "is_open",
        lambda moment: SESSION.start is not None and moment >= SESSION.start,
    )
    _patch_pnl(monkeypatch, equity=Decimal("101234.50"), rows=[], written=written)

    # Running before the open: the session guard returns, nothing is written.
    await trading_cycle(_ctx())
    assert written == []

    clock["now"] = NOW
    await trading_cycle(_ctx())

    assert len(written) == 1
    snapshot = written[0]
    assert snapshot.trade_date == moscow_date(NOW)
    assert snapshot.opening_equity == Decimal("101234.50")
    assert snapshot.closing_equity is None
    assert "daily_loss" in calls

    # Every later cycle of the same day measures against that row.
    clock["now"] = NOW + timedelta(minutes=5)
    await trading_cycle(_ctx())
    assert len(written) == 1


async def test_restart_later_the_same_day_does_not_overwrite_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row written this morning is the baseline; a restart must not move it."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    written: list[DailySnapshot] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    clock = {"now": PRE_OPEN}
    monkeypatch.setattr(loops, "now", lambda: clock["now"])
    monkeypatch.setattr(
        loops,
        "is_open",
        lambda moment: SESSION.start is not None and moment >= SESSION.start,
    )
    _patch_pnl(
        monkeypatch,
        equity=Decimal("90000"),
        rows=[_snapshot(Decimal("99000"))],
        written=written,
    )

    await trading_cycle(_ctx())
    clock["now"] = NOW
    await trading_cycle(_ctx())

    assert written == []
    assert "daily_loss" in calls


async def test_process_that_missed_the_open_writes_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its equity now is not the session open, so `pnl` reconstructs instead.

    Writing here would seed the baseline at 11:00 and make the morning's
    drawdown structurally invisible — the defect #9 named.
    """
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    written: list[DailySnapshot] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    monkeypatch.setattr(loops, "now", lambda: LATE)
    _patch_pnl(monkeypatch, equity=Decimal("90000"), rows=[], written=written)

    await trading_cycle(_ctx())

    assert written == []
    assert "daily_loss" in calls


async def test_price_rejected_in_pnl_skips_entries_alerts_once_and_no_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unmarkable position makes the day's loss unknowable, not imprecise."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    halted: list[HaltReason] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _loss(moment: datetime) -> Decimal:
        calls.append("daily_loss")
        raise PriceRejected("unusable")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    async def _do_halt(reason: HaltReason, detail: str, at: datetime) -> None:
        halted.append(reason)

    _patch_pnl(monkeypatch)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "halt", _do_halt)
    loops._market_failures = 0
    loops._market_alerted = False

    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))

    assert halted == []
    assert "candles" not in calls
    assert len(alerts) == 1
    assert loops._market_failures == 0


async def test_broker_unavailable_in_pnl_skips_entries_without_halting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A halt would outlive a condition that is usually momentary."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    halted: list[HaltReason] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    async def _loss(moment: datetime) -> Decimal:
        calls.append("daily_loss")
        raise BrokerUnavailable("down")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    async def _do_halt(reason: HaltReason, detail: str, at: datetime) -> None:
        halted.append(reason)

    _patch_pnl(monkeypatch)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "halt", _do_halt)

    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))

    assert halted == []
    assert "candles" not in calls
    assert len(alerts) == 1


async def test_unmeasurable_loss_alerts_once_until_a_clean_cycle_rearms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latch every alert in this module keeps: a muted bot is unmonitored."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    alerts: list[str] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    failing = True

    async def _loss(moment: datetime) -> Decimal:
        if failing:
            raise PriceRejected("unusable")
        return Decimal("0")

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    _patch_pnl(monkeypatch)
    monkeypatch.setattr(loops, "daily_loss_pct", _loss)
    monkeypatch.setattr(loops, "alert", _alert)

    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    failing = False
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 1
    failing = True
    await trading_cycle(_ctx(strategies=(_QuietStrategy(),)))
    assert len(alerts) == 2


async def test_unmarkable_equity_writes_no_opening_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A baseline the broker could not price is worse than no baseline."""
    from zarabot.app.loops import trading_cycle

    calls: list[str] = []
    written: list[DailySnapshot] = []
    alerts: list[str] = []
    halted: list[HaltReason] = []
    _patch_defaults(monkeypatch, calls)
    import zarabot.app.loops as loops

    clock = {"now": PRE_OPEN}
    monkeypatch.setattr(loops, "now", lambda: clock["now"])
    monkeypatch.setattr(
        loops,
        "is_open",
        lambda moment: SESSION.start is not None and moment >= SESSION.start,
    )

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    async def _do_halt(reason: HaltReason, detail: str, at: datetime) -> None:
        halted.append(reason)

    _patch_pnl(monkeypatch, equity=PriceRejected("unusable"), rows=[], written=written)
    monkeypatch.setattr(loops, "alert", _alert)
    monkeypatch.setattr(loops, "halt", _do_halt)

    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))
    clock["now"] = NOW
    await trading_cycle(_ctx(strategies=(_BuyStrategy(),)))

    assert written == []
    assert halted == []
    assert "daily_loss" not in calls
    assert "candles" not in calls
    assert len(alerts) == 1


class _LoopStopped(Exception):
    """Breaks `_trading_loop` out of its `while True` after one sleep."""


async def _one_iteration_delay(monkeypatch: pytest.MonkeyPatch, cycle: object) -> float:
    """The delay `_trading_loop` actually sleeps after one cycle.

    Driven through the real loop rather than through the helper that computes
    the number, because the contract is about what the bot waits, not about how
    the waiting is worked out.
    """
    import zarabot.app.loops as loops

    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)
        raise _LoopStopped

    monkeypatch.setattr(loops.asyncio, "sleep", _sleep)
    monkeypatch.setattr(loops, "trading_cycle", cycle)
    with pytest.raises(_LoopStopped):
        await loops._trading_loop(_ctx())
    return slept[0]


def _rate_limited(retry_after: Decimal | None) -> object:
    async def _cycle(ctx: AppContext) -> None:
        import zarabot.app.loops as loops

        await loops._note_data_failure(BrokerRateLimited(retry_after))

    return _cycle


async def test_the_brokers_hint_delays_the_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 2: back off for at least as long as the broker asked."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._retry_after = None

    delay = await _one_iteration_delay(monkeypatch, _rate_limited(Decimal("300")))
    # One failure escalates 60s to 120s; the broker asked for 300.
    assert delay == 300


async def test_a_hint_shorter_than_the_backoff_does_not_shorten_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint raises the floor; it never lowers the ceiling."""
    import zarabot.app.loops as loops

    loops._market_failures = 2
    loops._market_alerted = True
    loops._retry_after = None

    delay = await _one_iteration_delay(monkeypatch, _rate_limited(Decimal("2")))
    # The third consecutive failure escalates 60s to 480s.
    assert delay == 480


async def test_a_hint_beyond_the_maximum_backoff_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No number from outside may hold the exit path asleep."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._retry_after = None

    delay = await _one_iteration_delay(monkeypatch, _rate_limited(Decimal("999999")))
    assert delay == loops._MAX_BACKOFF


async def test_a_successful_cycle_clears_the_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remembered hint is stale state shaped like a measurement (rule 36)."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._retry_after = Decimal("600")

    async def _clean(ctx: AppContext) -> None:
        loops._note_data_success()

    delay = await _one_iteration_delay(monkeypatch, _clean)
    assert delay == 60


async def test_a_later_failure_does_not_inherit_an_earlier_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every failure overwrites the hint, carrying one or not."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._retry_after = Decimal("600")

    async def _plain_outage(ctx: AppContext) -> None:
        await loops._note_data_failure(BrokerUnavailable("down"))

    delay = await _one_iteration_delay(monkeypatch, _plain_outage)
    assert delay == 120


async def test_sustained_throttling_alerts_once_and_names_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate limit reported as a market-data outage reads as weather (#6, #23)."""
    import zarabot.app.loops as loops

    loops._market_failures = 0
    loops._market_alerted = False
    loops._retry_after = None
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr(loops, "alert", _alert)
    for _ in range(4):
        await loops._note_data_failure(BrokerRateLimited(Decimal("30")))

    assert len(alerts) == 1
    assert "rate limit" in alerts[0].lower()
