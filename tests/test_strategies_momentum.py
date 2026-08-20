"""Tests for zarabot.strategies.momentum — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.momentum import MomentumBreakout

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = MomentumBreakout()


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
    # Close pushes above the prior 20-bar high.
    closes = [100] * 20 + [110]
    signal = STRATEGY.evaluate("SBER", _candles(closes), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "momentum"
    assert signal.side is Side.BUY


def test_no_setup_returns_none() -> None:
    closes = list(range(120, 99, -1))
    assert STRATEGY.evaluate("SBER", _candles(closes), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100] * 30), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles([100] * 20 + [110])
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        [100] * 20 + [110],
        list(range(120, 99, -1)),
        [100] * 30,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL
