"""Watchlist candles. One failed ticker never blinds the rest."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from t_tech.invest.schemas import CandleInterval

from zarabot.broker.client import get_candles, get_instrument
from zarabot.models import Candle

_LOG = logging.getLogger(__name__)
_CALENDAR_PAD = 14


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


async def candles_for_watchlist(
    tickers: list[str], lookback: int, now: datetime
) -> dict[str, list[Candle]]:
    _reject_naive(now)
    since = now - timedelta(days=max(lookback, 1) * 3 + _CALENDAR_PAD)
    result: dict[str, list[Candle]] = {}
    for ticker in tickers:
        try:
            instrument = await get_instrument(ticker)
            candles = await get_candles(
                instrument.figi,
                CandleInterval.CANDLE_INTERVAL_DAY,
                since,
                now,
            )
        except Exception:
            _LOG.warning("candle fetch failed for %s; omitting from batch", ticker)
            continue
        candles = sorted(candles, key=lambda candle: candle.timestamp)
        result[ticker] = candles
    return result
