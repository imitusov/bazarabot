"""Tests for zarabot.strategies.rsi_reversion — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.rsi_reversion import RSIReversion

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = RSIReversion()


def _candles(closes: Sequence[int | str]) -> list[Candle]:
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
    # Consecutive declines drive RSI into oversold territory.
    closes = list(range(100, 100 - 15, -1))
    signal = STRATEGY.evaluate("SBER", _candles(closes), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "rsi_reversion"
    assert signal.side is Side.BUY


def test_no_setup_returns_none() -> None:
    closes = list(range(100, 115))
    assert STRATEGY.evaluate("SBER", _candles(closes), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100] * 20), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles(list(range(100, 100 - 15, -1)))
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        list(range(100, 100 - 15, -1)),
        list(range(100, 115)),
        [100] * 20,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL
