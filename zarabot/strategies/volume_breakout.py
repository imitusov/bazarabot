"""Volume-confirmed breakout: `momentum`'s price condition plus a volume filter.

The price half is deliberately identical to `momentum` — the latest close
strictly above the highest high of the prior twenty bars. This module exists for
the second half: the breakout bar must also have traded at or above 1.5x the mean
volume of those same twenty bars. `Candle.volume` is carried on every fetch and,
before spec v1.95, was read by no strategy at all; a breakout on thin volume is
the textbook false breakout.

Pure: no I/O, no database, no clock beyond the `now` argument.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.models import Candle, Side, Signal

_BREAKOUT_BARS = 20
_VOLUME_BARS = 20
_VOLUME_MULTIPLE = Decimal("1.5")


class VolumeBreakout:
    name = "volume_breakout"
    lookback = max(_BREAKOUT_BARS, _VOLUME_BARS) + 1

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        recent = candles[-self.lookback :]
        last = recent[-1]
        if len({c.close for c in recent}) == 1:
            return None
        prior_high = max(c.high for c in recent[-(_BREAKOUT_BARS + 1) : -1])
        if last.close <= prior_high:
            return None
        volume_window = recent[-(_VOLUME_BARS + 1) : -1]
        # Decimal, not float: the mean of twenty integers is not an integer, and
        # the contract's bound is inclusive, so the boundary case must be exact.
        average_volume = sum(
            (Decimal(c.volume) for c in volume_window), Decimal(0)
        ) / Decimal(_VOLUME_BARS)
        if average_volume == 0:
            # Nothing traded in the prior window: there is no participation to
            # confirm against, so the breakout is unconfirmable rather than
            # confirmed. Returning a BUY here would make a halted instrument the
            # easiest signal in the system.
            return None
        if Decimal(last.volume) < _VOLUME_MULTIPLE * average_volume:
            return None
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=last.close,
        )
