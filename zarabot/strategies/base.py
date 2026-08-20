"""Strategy protocol: pure, entry-only, deterministic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from zarabot.models import Candle, Signal


@runtime_checkable
class Strategy(Protocol):
    name: str
    lookback: int

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        """Return a BUY signal or None. Never SELL. None on short or flat series."""
        ...
