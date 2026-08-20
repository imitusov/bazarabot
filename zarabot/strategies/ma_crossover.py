"""Moving-average crossover: buy when the fast SMA crosses above the slow SMA."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.models import Candle, Side, Signal

_FAST = 10
_SLOW = 30


def _sma(closes: list[Decimal], period: int) -> Decimal:
    window = closes[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


class MovingAverageCrossover:
    name = "ma_crossover"
    lookback = _SLOW + 1

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        closes = [c.close for c in candles]
        if len(set(closes)) == 1:
            return None
        prev = closes[:-1]
        fast_now = _sma(closes, _FAST)
        slow_now = _sma(closes, _SLOW)
        fast_prev = _sma(prev, _FAST)
        slow_prev = _sma(prev, _SLOW)
        if fast_prev <= slow_prev and fast_now > slow_now:
            return Signal(
                ticker=ticker,
                strategy=self.name,
                side=Side.BUY,
                generated_at=now,
                reference_price=closes[-1],
            )
        return None
