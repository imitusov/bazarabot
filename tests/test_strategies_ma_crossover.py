"""Tests for zarabot.strategies.ma_crossover — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.ma_crossover import MovingAverageCrossover

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = MovingAverageCrossover()


def _candles(closes: list[int | str]) -> list[Candle]:
    start = NOW - timedelta(days=len(closes))
    out: list[Candle] = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
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


def test_entry_series_returns_signal() -> None:
    # 30 flats then a jump: fast SMA crosses above slow SMA.
    closes = [100] * 30 + [200]
    signal = STRATEGY.evaluate("SBER", _candles(closes), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "ma_crossover"
    assert signal.side is Side.BUY


def test_no_setup_returns_none() -> None:
    closes = list(range(150, 150 - 31, -1))
    assert STRATEGY.evaluate("SBER", _candles(closes), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100] * 40), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles([100] * 30 + [200])
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        [100] * 30 + [200],
        list(range(150, 150 - 31, -1)),
        [100] * 40,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL
