"""Tests for zarabot.strategies.ma_crossover — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.ma_crossover import _FAST, _SLOW, MovingAverageCrossover

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = MovingAverageCrossover()


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
    # 80 flats then a jump: fast SMA crosses above slow SMA. The flat run is
    # the slow window, so the series is lookback-long (81) — anything shorter
    # returns None at the length guard and proves nothing about a crossing.
    closes = [100] * 80 + [200]
    signal = STRATEGY.evaluate("SBER", _candles(closes), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "ma_crossover"
    assert signal.side is Side.BUY


def test_no_setup_returns_none() -> None:
    # A monotonically falling series long enough to clear the lookback: the
    # fast average is below the slow one on both bars, so nothing crosses.
    closes = list(range(250, 250 - 81, -1))
    assert STRATEGY.evaluate("SBER", _candles(closes), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none() -> None:
    # Longer than lookback on purpose: a 40-bar flat series would return None
    # at the length guard and never reach the degenerate-input path.
    assert STRATEGY.evaluate("SBER", _candles([100] * 90), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles([100] * 80 + [200])
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        [100] * 80 + [200],
        list(range(250, 250 - 81, -1)),
        [100] * 90,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL


def test_lookback_is_derived_from_the_slow_window() -> None:
    # Spec §4: `lookback` is the slow window plus one bar, derived rather than
    # written as a literal, so the two cannot drift apart.
    assert STRATEGY.lookback == _SLOW + 1
    assert (_FAST, _SLOW) == (40, 80)


def test_entry_fixture_is_at_least_lookback_long() -> None:
    # Guards failure class 9 directly: if the happy-path fixture ever shrinks
    # below the lookback, `evaluate` returns None at the length guard and the
    # happy-path test would pass vacuously.
    assert len(_candles([100] * 80 + [200])) >= STRATEGY.lookback
