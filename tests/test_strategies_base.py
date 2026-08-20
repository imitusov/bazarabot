"""Tests for zarabot.strategies.base — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side, Signal
from zarabot.strategies.base import Strategy

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)


def _candles(closes: list[str]) -> list[Candle]:
    start = NOW - timedelta(days=len(closes))
    out: list[Candle] = []
    for i, close in enumerate(closes):
        price = Decimal(close)
        out.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            )
        )
    return out


class _Dummy:
    name = "dummy"
    lookback = 3

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        closes = [c.close for c in candles]
        if len(set(closes)) == 1:
            return None
        if closes[-1] > closes[0]:
            return Signal(
                ticker=ticker,
                strategy=self.name,
                side=Side.BUY,
                generated_at=now,
                reference_price=closes[-1],
            )
        return None


def test_dummy_conforms_to_strategy_protocol() -> None:
    strategy: Strategy = _Dummy()
    assert strategy.name == "dummy"
    assert strategy.lookback == 3


def test_entry_series_returns_buy_signal() -> None:
    signal = _Dummy().evaluate("SBER", _candles(["10", "11", "12"]), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "dummy"
    assert signal.side is Side.BUY


def test_no_setup_returns_none() -> None:
    assert _Dummy().evaluate("SBER", _candles(["12", "11", "10"]), NOW) is None


def test_short_series_returns_none() -> None:
    assert _Dummy().evaluate("SBER", _candles(["10", "11"]), NOW) is None


def test_flat_series_returns_none() -> None:
    assert _Dummy().evaluate("SBER", _candles(["10", "10", "10"]), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles(["10", "11", "12"])
    first = _Dummy().evaluate("SBER", candles, NOW)
    second = _Dummy().evaluate("SBER", candles, NOW)
    assert first == second


def test_never_returns_sell() -> None:
    dummy = _Dummy()
    for series in (
        ["10", "11", "12"],
        ["12", "11", "10"],
        ["10", "10", "10"],
        ["10"],
    ):
        signal = dummy.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL
