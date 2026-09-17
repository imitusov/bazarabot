"""Tests for zarabot.strategies.volume_breakout — from technical-spec.md §3.2
and the module's own §4 heading."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.volume_breakout import (
    _BREAKOUT_BARS,
    _VOLUME_BARS,
    _VOLUME_MULTIPLE,
    VolumeBreakout,
)

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = VolumeBreakout()

# The prior window: twenty flat bars on an unremarkable 1000 lots each.
_PRIOR_CLOSES = [100] * 20
_PRIOR_VOLUMES = [1000] * 20
# The breakout bar: clears the prior high of 100 by ten.
_BREAKOUT_CLOSE = 110


def _candles(
    closes: Sequence[int | str], volumes: Sequence[int] | None = None
) -> list[Candle]:
    vols = [1000] * len(closes) if volumes is None else list(volumes)
    assert len(vols) == len(closes)
    start = NOW - timedelta(days=len(closes))
    out: list[Candle] = []
    for i, (close, volume) in enumerate(zip(closes, vols, strict=True)):
        price = Decimal(str(close))
        out.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        )
    return out


def _breakout(volume: int) -> list[Candle]:
    """The same price breakout every time; only the breakout bar's volume moves."""
    return _candles([*_PRIOR_CLOSES, _BREAKOUT_CLOSE], [*_PRIOR_VOLUMES, volume])


def test_entry_series_returns_signal() -> None:
    signal = STRATEGY.evaluate("SBER", _breakout(2000), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "volume_breakout"
    assert signal.side is Side.BUY
    assert signal.reference_price == Decimal(_BREAKOUT_CLOSE)
    assert signal.generated_at == NOW


def test_breakout_on_thin_volume_returns_none() -> None:
    # The pair that is the whole strategy, half one: identical prices to
    # `test_breakout_on_heavy_volume_returns_buy`, volume below 1.5x the
    # trailing average of 1000. Without this case the volume filter could be
    # deleted and every other test here would stay green.
    assert STRATEGY.evaluate("SBER", _breakout(1400), NOW) is None


def test_breakout_on_heavy_volume_returns_buy() -> None:
    # The pair, half two: the same prices at exactly 1.5x the trailing average.
    # The contract's bound is inclusive, so the boundary bar is confirmation.
    signal = STRATEGY.evaluate("SBER", _breakout(1500), NOW)
    assert signal is not None
    assert signal.side is Side.BUY


def test_volume_just_below_the_multiple_returns_none() -> None:
    # One lot under the inclusive bound, to pin which side of 1.5x is an entry.
    assert STRATEGY.evaluate("SBER", _breakout(1499), NOW) is None


def test_no_price_setup_returns_none_even_on_huge_volume() -> None:
    # A falling series at least `lookback` long: no breakout to confirm, so
    # volume cannot manufacture one.
    closes = list(range(120, 120 - 21, -1))
    volumes = [1000] * 20 + [100_000]
    assert STRATEGY.evaluate("SBER", _candles(closes, volumes), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none() -> None:
    # Longer than `lookback` on purpose: a shorter flat fixture would return
    # None at the length guard and never reach the degenerate-input path.
    closes = [100] * 30
    volumes = [1000] * 29 + [100_000]
    assert STRATEGY.evaluate("SBER", _candles(closes, volumes), NOW) is None


def test_zero_prior_volume_returns_none() -> None:
    # A halted instrument: nothing traded in the prior window, so there is no
    # average to confirm against and a zero-volume breakout bar must not pass.
    closes = [*_PRIOR_CLOSES, _BREAKOUT_CLOSE]
    volumes = [0] * 20 + [0]
    assert STRATEGY.evaluate("SBER", _candles(closes, volumes), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _breakout(2000)
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for candles in (
        _breakout(2000),
        _breakout(1400),
        _candles(list(range(120, 120 - 21, -1))),
        _candles([100] * 30),
        _candles([100]),
    ):
        signal = STRATEGY.evaluate("SBER", candles, NOW)
        if signal is not None:
            assert signal.side is not Side.SELL


def test_lookback_is_derived_from_the_two_windows() -> None:
    # Spec §4: derived as max(breakout window, volume window) + 1, never a
    # literal, so the two windows and the lookback cannot drift apart.
    assert STRATEGY.lookback == max(_BREAKOUT_BARS, _VOLUME_BARS) + 1
    assert (_BREAKOUT_BARS, _VOLUME_BARS) == (20, 20)
    assert str(_VOLUME_MULTIPLE) == "1.5"
    assert STRATEGY.lookback == 21


def test_entry_fixture_is_at_least_lookback_long() -> None:
    # Failure class 9, the trap #252 found twice in this package: a fixture
    # shorter than `lookback` returns None at the length guard, so every case
    # built on it passes for the wrong reason.
    assert len(_breakout(2000)) >= STRATEGY.lookback
    assert len(_candles([100] * 30)) >= STRATEGY.lookback
    assert len(_candles(list(range(120, 120 - 21, -1)))) >= STRATEGY.lookback
