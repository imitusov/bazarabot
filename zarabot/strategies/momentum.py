"""Momentum breakout: buy when close exceeds the prior N-bar high."""

from __future__ import annotations

from datetime import datetime

from zarabot.models import Candle, Side, Signal

_LOOKBACK_BARS = 20


class MomentumBreakout:
    name = "momentum"
    lookback = _LOOKBACK_BARS + 1

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        recent = candles[-self.lookback :]
        prior = recent[:-1]
        last = recent[-1]
        if len({c.close for c in recent}) == 1:
            return None
        prior_high = max(c.high for c in prior)
        if last.close > prior_high:
            return Signal(
                ticker=ticker,
                strategy=self.name,
                side=Side.BUY,
                generated_at=now,
                reference_price=last.close,
            )
        return None
