"""RSI mean-reversion: buy when RSI is oversold."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.models import Candle, Side, Signal

_PERIOD = 14
_OVERSOLD = Decimal(30)


def _rsi(closes: list[Decimal], period: int) -> Decimal | None:
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = changes[-period:]
    avg_gain = sum((c for c in window if c > 0), Decimal(0)) / Decimal(period)
    avg_loss = sum((-c for c in window if c < 0), Decimal(0)) / Decimal(period)
    if avg_gain == 0 and avg_loss == 0:
        return None
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


class RSIReversion:
    name = "rsi_reversion"
    lookback = _PERIOD + 1

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        closes = [c.close for c in candles]
        if len(set(closes)) == 1:
            return None
        rsi = _rsi(closes, _PERIOD)
        if rsi is None or rsi >= _OVERSOLD:
            return None
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=closes[-1],
        )
