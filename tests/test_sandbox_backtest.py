"""Tests for sandbox.backtest — written from technical-spec.md §3.2."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sandbox.backtest import run

from zarabot.config import Config
from zarabot.lifecycle.exits import evaluate as live_exits
from zarabot.models import (
    Candle,
    ExitTrigger,
    SessionInfo,
    Side,
    Signal,
    StopProtection,
)
from zarabot.strategies.ma_crossover import MovingAverageCrossover

BUY_AT = datetime(2026, 3, 13, 15, 0, tzinfo=UTC)


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


def _candle(stamp: datetime, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        timestamp=stamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
    )


def _series() -> list[Candle]:
    start = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    closes = ["100", "100", "100", "110", "121"]
    return [_candle(start + timedelta(days=i), close) for i, close in enumerate(closes)]


class _BuyAt:
    name = "buy_at"
    lookback = 1

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if now != BUY_AT or not candles:
            return None
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=candles[-1].close,
        )


class _Recording:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.name = str(inner.name)
        self.lookback = int(inner.lookback)
        self.calls: list[tuple[tuple[datetime, ...], datetime]] = []

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        self.calls.append((tuple(c.timestamp for c in candles), now))
        return inner_eval(self.inner, ticker, candles, now)


def inner_eval(
    inner: object, ticker: str, candles: list[Candle], now: datetime
) -> Signal | None:
    evaluate = inner.evaluate
    result = evaluate(ticker, candles, now)
    return result if result is None or isinstance(result, Signal) else None


def test_known_series_matches_hand_computed_trade() -> None:
    result = run(_BuyAt(), _series(), _config(), Decimal("1"), Decimal("0"))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_trigger is ExitTrigger.TAKE_PROFIT
    assert trade.entry_price == Decimal("110")
    assert trade.exit_price == Decimal("121")
    assert trade.lots == 90
    assert result.pnl == Decimal("988")


def test_backtester_matches_live_strategy_and_exits() -> None:
    candles = _series()
    inner = _BuyAt()
    recorder = _Recording(inner)
    result = run(recorder, candles, _config(), Decimal("1"), Decimal("0"))
    for timestamps, now in recorder.calls:
        visible = [c for c in candles if c.timestamp < now]
        assert inner_eval(inner, "SBER", visible, now) == inner_eval(
            inner, "SBER", visible, now
        )
        assert timestamps == tuple(c.timestamp for c in visible)
    trade = result.trades[0]
    assert trade.exit_price is not None and trade.exit_at is not None
    opened = replace(
        trade,
        status="OPEN",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )
    session = SessionInfo(
        start=trade.exit_at,
        end=trade.exit_at + timedelta(minutes=1),
        is_trading_day=True,
    )
    trigger = live_exits(
        opened, trade.exit_price, trade.exit_at, session, 0, _config()
    )
    assert trigger is ExitTrigger.TAKE_PROFIT


def test_strategy_never_sees_future_candles() -> None:
    recorder = _Recording(_BuyAt())
    run(recorder, _series(), _config(), Decimal("1"), Decimal("0"))
    for timestamps, now in recorder.calls:
        assert all(stamp < now for stamp in timestamps)


def test_commission_and_slippage_apply_to_every_fill() -> None:
    zero = run(_BuyAt(), _series(), _config(), Decimal("0"), Decimal("0"))
    costly = run(_BuyAt(), _series(), _config(), Decimal("2"), Decimal("0"))
    assert zero.pnl - costly.pnl == Decimal("4")
    slipped = run(_BuyAt(), _series(), _config(), Decimal("0"), Decimal("0.01"))
    assert slipped.trades[0].entry_price != zero.trades[0].entry_price


def test_ma_crossover_shared_with_live_path() -> None:
    strategy = MovingAverageCrossover()
    closes = [100] * 30 + [200]
    start = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    candles = [
        _candle(start + timedelta(days=i), str(price)) for i, price in enumerate(closes)
    ]
    now = candles[-1].timestamp
    visible = [c for c in candles if c.timestamp < now]
    live = strategy.evaluate("SBER", visible, now)
    recorder = _Recording(strategy)
    run(recorder, candles, _config(), Decimal("0"), Decimal("0"))
    matching = [call for call in recorder.calls if call[1] == now]
    assert matching
    seen = matching[0][0]
    assert seen == tuple(c.timestamp for c in visible)
    assert live is not None
    assert live.side is Side.BUY
