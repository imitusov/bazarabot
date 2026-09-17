"""MACD trend: buy when the MACD line crosses above its signal line.

The MACD line is the 12-period EMA of closes minus the 26-period EMA; the signal
line is the 9-period EMA of the MACD line. This is a trend strategy like
`ma_crossover`, but momentum-of-momentum rather than a level comparison:
`ma_crossover` compares two averages of price, while this compares the rate of
change of the gap between them against its own average. It therefore turns
earlier in a move and costs more false starts.

The condition is the crossing and not the ordering, exactly as in
`ma_crossover`: a series that has been above for weeks crosses nothing.

Pure: no I/O, no database, no clock beyond the `now` argument, `Decimal`
throughout.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.models import Candle, Side, Signal

_FAST = 12
_SLOW = 26
_SIGNAL = 9


def _ema(values: list[Decimal], period: int) -> list[Decimal]:
    """EMA series for `values`, aligned to `values[period - 1:]`.

    Seeded with the simple average of the first `period` values rather than with
    the first value alone, so the result is a function of the window rather than
    of how much history happened to be fetched.
    """
    seed = sum(values[:period], Decimal(0)) / Decimal(period)
    out = [seed]
    factor = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        out.append((value - out[-1]) * factor + out[-1])
    return out


class MACDTrend:
    name = "macd_trend"
    # 26 closes seed the slow EMA, after which each further close yields one
    # MACD value; 9 MACD values seed the signal EMA. That leaves exactly two
    # signal values, the minimum a crossing can be read from.
    lookback = _SLOW + _SIGNAL

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        closes = [c.close for c in candles]
        if len(set(closes)) == 1:
            return None
        fast = _ema(closes, _FAST)
        slow = _ema(closes, _SLOW)
        # `fast` is aligned to closes[_FAST - 1:] and `slow` to
        # closes[_SLOW - 1:], so the fast series is offset by the difference.
        offset = _SLOW - _FAST
        macd = [fast[i + offset] - slow[i] for i in range(len(slow))]
        signal_line = _ema(macd, _SIGNAL)
        macd_aligned = macd[_SIGNAL - 1 :]
        if macd_aligned[-2] <= signal_line[-2] and macd_aligned[-1] > signal_line[-1]:
            return Signal(
                ticker=ticker,
                strategy=self.name,
                side=Side.BUY,
                generated_at=now,
                reference_price=closes[-1],
            )
        return None
