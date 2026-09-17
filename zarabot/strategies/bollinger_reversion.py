"""Bollinger-band mean reversion: buy at or below the lower band.

The lower band is the simple moving average of the last twenty closes minus two
population standard deviations of those same closes. This is mean reversion like
`rsi_reversion`, but volatility-scaled: RSI's threshold is a fixed 30 whatever
the instrument has been doing, while this band widens as realised volatility
rises and narrows as it falls, so the same percentage dip is an entry in a quiet
instrument and not in a violent one.

Pure: no I/O, no database, no clock beyond the `now` argument. Every figure is
`Decimal` end to end, including the square root — see `_stddev`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Context, Decimal

from zarabot.models import Candle, Side, Signal

_PERIOD = 20
_STDDEV_MULTIPLE = Decimal(2)

# Local to this module and never installed with `setcontext`: a module-level
# side effect is forbidden in this package, and mutating the process-wide
# decimal context would change arithmetic in every other module.
_SQRT_CONTEXT = Context(prec=40)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _stddev(values: list[Decimal], mean: Decimal) -> Decimal:
    """Population standard deviation, in `Decimal` throughout.

    `math.sqrt` is not used: it would round a price through `float`, and a band
    edge compared against money must not have platform-dependent last bits.
    `N` rather than `N - 1` because the window *is* the population — the
    convention every published Bollinger band uses.
    """
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / Decimal(len(values))
    return _SQRT_CONTEXT.sqrt(variance)


class BollingerReversion:
    name = "bollinger_reversion"
    lookback = _PERIOD

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        window = [c.close for c in candles[-_PERIOD:]]
        if len(set(window)) == 1:
            # Zero standard deviation puts the lower band exactly on the close,
            # and the entry bound is inclusive — so without this guard a series
            # with no volatility at all would be a BUY. This is the degenerate
            # input the protocol requires be answered with None.
            return None
        mean = _mean(window)
        lower_band = mean - _STDDEV_MULTIPLE * _stddev(window, mean)
        last_close = window[-1]
        if last_close > lower_band:
            return None
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=last_close,
        )
