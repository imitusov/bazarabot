"""Tests for zarabot.strategies.macd_trend — from technical-spec.md §3.2 and
the module's own §4 heading."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.macd_trend import (
    _FAST,
    _SIGNAL,
    _SLOW,
    MACDTrend,
)

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = MACDTrend()

# Thirty-four bars falling from 100 to 67, then a jump to 80: the MACD line is
# at or below its signal line on the second-to-last bar and strictly above it
# on the last. Exactly 35 bars, which is `lookback`.
_ENTRY: list[int | str] = [100 - i for i in range(34)] + [80]

# Thirty bars falling, then five rising: the MACD line is already above its
# signal line on BOTH of the last two bars. Nothing crosses, so the answer is
# None — the condition is the crossing, not the ordering.
_ALREADY_ABOVE: list[int | str] = [100 - i for i in range(30)] + [71, 74, 78, 83, 89]

# A plain downtrend: below on both bars.
_DOWNTREND: list[int | str] = list(range(135, 100, -1))


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
    signal = STRATEGY.evaluate("SBER", _candles(_ENTRY), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "macd_trend"
    assert signal.side is Side.BUY
    assert signal.reference_price == Decimal(80)
    assert signal.generated_at == NOW


def test_already_above_the_signal_line_returns_none() -> None:
    # The case that separates a crossing from an ordering. A series that has
    # been above for bars crosses nothing; without this case the condition
    # could be `macd > signal` and the suite would not notice.
    assert STRATEGY.evaluate("SBER", _candles(_ALREADY_ABOVE), NOW) is None


def test_no_setup_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles(_DOWNTREND), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_one_bar_short_of_lookback_returns_none() -> None:
    # The boundary of the length guard: 34 bars cannot yield two signal
    # values, so no crossing can be read even though the prices would give one.
    assert len(_ENTRY) - 1 == STRATEGY.lookback - 1
    assert STRATEGY.evaluate("SBER", _candles(_ENTRY[:-1]), NOW) is None


def test_flat_series_returns_none() -> None:
    # Longer than `lookback` on purpose: a shorter flat fixture would return
    # None at the length guard and never reach the degenerate-input path.
    assert STRATEGY.evaluate("SBER", _candles([100] * 40), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles(_ENTRY)
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        _ENTRY,
        _ALREADY_ABOVE,
        _DOWNTREND,
        [100] * 40,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL


def test_lookback_is_derived_from_the_slow_and_signal_periods() -> None:
    # Spec §4: 26 closes seed the slow EMA, 9 MACD values seed the signal EMA,
    # and that leaves exactly two signal values — the minimum a crossing can be
    # read from. Derived, never a literal.
    assert STRATEGY.lookback == _SLOW + _SIGNAL
    assert (_FAST, _SLOW, _SIGNAL) == (12, 26, 9)
    assert STRATEGY.lookback == 35


def test_lookback_does_not_exceed_the_longest_in_the_project() -> None:
    # `app.loops` sizes its candle fetch from max(strategy.lookback), which is
    # `ma_crossover`'s 81. 35 is shorter, so this strategy changes no fetch.
    from zarabot.strategies.ma_crossover import MovingAverageCrossover

    assert STRATEGY.lookback <= MovingAverageCrossover.lookback


def test_fixtures_are_at_least_lookback_long() -> None:
    # Failure class 9: a fixture shorter than `lookback` returns None at the
    # length guard, so a case asserting `is None` passes for the wrong reason.
    for series in (_ENTRY, _ALREADY_ABOVE, _DOWNTREND, [100] * 40):
        assert len(_candles(series)) >= STRATEGY.lookback
