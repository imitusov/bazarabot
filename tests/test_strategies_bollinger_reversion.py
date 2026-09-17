"""Tests for zarabot.strategies.bollinger_reversion — from technical-spec.md
§3.2 and the module's own §4 heading."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zarabot.models import Candle, Side
from zarabot.strategies.bollinger_reversion import (
    _PERIOD,
    _STDDEV_MULTIPLE,
    BollingerReversion,
)

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
STRATEGY = BollingerReversion()

# A deep dip against nineteen flat bars: mean 97.5, population sd 10.8972,
# lower band 75.7055, latest close 50.
_ENTRY: list[int | str] = [100] * 19 + [50]

# The volatility-scaling pair. Both series have a mean of 99.75 and the same
# latest close of 95; only the realised volatility of the nineteen bars before
# it differs, and that alone decides the signal. This is the property that
# distinguishes the strategy from `rsi_reversion`'s fixed threshold of 30.
_QUIET: list[int | str] = ["100", "100.1", "99.9"] * 6 + ["100", "95"]
_VOLATILE: list[int | str] = ["100", "120", "80"] * 6 + ["100", "95"]
# Mean 99.85, sd 3.9278, lower band 91.9944: the close of 97 is below the mean
# but above the band, so a dip alone is not an entry.
_NEAR_MISS: list[int | str] = ["100", "105", "95"] * 6 + ["100", "97"]


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
    assert signal.strategy == "bollinger_reversion"
    assert signal.side is Side.BUY
    assert signal.reference_price == Decimal(50)
    assert signal.generated_at == NOW


def test_no_setup_returns_none() -> None:
    # A rising series at least `lookback` long: the latest close is the highest
    # of the window, nowhere near the lower band.
    assert STRATEGY.evaluate("SBER", _candles(list(range(100, 120))), NOW) is None


def test_dip_above_the_lower_band_returns_none() -> None:
    # Below the mean is not below the band. This is the case a fixed-threshold
    # rule would get wrong.
    assert STRATEGY.evaluate("SBER", _candles(_NEAR_MISS), NOW) is None


def test_the_same_dip_is_an_entry_in_a_quiet_series() -> None:
    # Volatility scaling, half one: the band is narrow, so a close of 95
    # against a mean of 99.75 is outside it.
    signal = STRATEGY.evaluate("SBER", _candles(_QUIET), NOW)
    assert signal is not None
    assert signal.side is Side.BUY
    assert signal.reference_price == Decimal(95)


def test_the_same_dip_is_not_an_entry_in_a_volatile_series() -> None:
    # Volatility scaling, half two: identical mean and identical latest close,
    # wider band, no signal. A fixed threshold cannot tell these two apart.
    assert STRATEGY.evaluate("SBER", _candles(_VOLATILE), NOW) is None


def test_short_series_returns_none() -> None:
    assert STRATEGY.evaluate("SBER", _candles([100, 90, 80]), NOW) is None


def test_flat_series_returns_none() -> None:
    # Load-bearing, not boilerplate: a flat window has zero standard
    # deviation, so the lower band equals the average equals the close, and
    # the contract's inclusive bound would otherwise return a BUY on a series
    # with no volatility at all. Longer than `lookback` on purpose.
    assert STRATEGY.evaluate("SBER", _candles([100] * 25), NOW) is None


def test_evaluate_is_deterministic() -> None:
    candles = _candles(_ENTRY)
    assert STRATEGY.evaluate("SBER", candles, NOW) == STRATEGY.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell() -> None:
    for series in (
        _ENTRY,
        list(range(100, 120)),
        _NEAR_MISS,
        _QUIET,
        _VOLATILE,
        [100] * 25,
        [100],
    ):
        signal = STRATEGY.evaluate("SBER", _candles(series), NOW)
        if signal is not None:
            assert signal.side is not Side.SELL


def test_lookback_is_derived_from_the_period() -> None:
    # Spec §4: `lookback` is the period itself — the band is a property of one
    # window, not a comparison between two bars — derived, never a literal.
    assert STRATEGY.lookback == _PERIOD
    assert _PERIOD == 20
    assert str(_STDDEV_MULTIPLE) == "2"
    assert STRATEGY.lookback == 20


def test_fixtures_are_at_least_lookback_long() -> None:
    # Failure class 9: a fixture shorter than `lookback` returns None at the
    # length guard, so a case asserting `is None` would pass for the wrong
    # reason and a case asserting a BUY would fail for the right one.
    for series in (
        _ENTRY,
        list(range(100, 120)),
        _NEAR_MISS,
        _QUIET,
        _VOLATILE,
        [100] * 25,
    ):
        assert len(_candles(series)) >= STRATEGY.lookback


def test_reference_price_is_decimal_not_float() -> None:
    # The band is computed through a square root; the price that comes back
    # must still be the exact `Decimal` close it went in as.
    signal = STRATEGY.evaluate("SBER", _candles(_ENTRY), NOW)
    assert signal is not None
    assert isinstance(signal.reference_price, Decimal)
