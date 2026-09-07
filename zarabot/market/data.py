"""Watchlist candles. One failed ticker never blinds the rest — and never hides.

A ticker that fails is omitted from the batch, which is what keeps one bad
instrument from stopping the bot. Before rule 9 carried a threshold, that was
the whole of it: `except Exception: continue`, one WARNING, and a delisted or
renamed ticker dropped from every batch forever while the bot reported itself
healthy. Silence that reads as health is #23, and it is #39's shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from t_tech.invest.schemas import CandleInterval

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    get_candles,
    get_instrument,
)
from zarabot.models import Candle
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_CALENDAR_PAD = 14
_DEGRADED_BEFORE_ALERT = 3

# Only the broker's own failures are absorbed. Everything else — an
# AttributeError from a renamed SDK field, a ValueError from a malformed candle
# — propagates to `app.loops._supervise`, which alerts with a traceback and
# restarts under rule 21. Since v1.27 `broker.client` raises those as
# themselves rather than as BrokerUnavailable; catching Exception here put them
# straight back in the dark, which is the second half of #23.
_ABSORBED = (BrokerUnavailable, BrokerRateLimited, InstrumentNotFound)

# Consecutive degraded calls per ticker, and the tickers already reported. Both
# are process-local, like `market.session`'s cache: they measure what THIS
# process has seen, and a restart is entitled to start over rather than inherit
# a count it did not observe.
_failures: dict[str, int] = {}
_alerted: set[str] = set()


def _reject_naive(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")


def _note_success(ticker: str) -> None:
    """Clear the count AND the latch.

    Both, always, in one place. An alerted flag that is set and never reset
    fires once per process and is silent for every later incident — #32 in
    `app.loops`, then #48 in the same module hours after it was closed. The
    check that catches it is mechanical: one set-site, one reset-site, listed
    against each other.
    """
    _failures.pop(ticker, None)
    _alerted.discard(ticker)


def _note_degraded(ticker: str, reason: str) -> bool:
    """Count one degraded call. True when this one crosses the threshold.

    A failed fetch and a short one are counted here together, on one counter,
    deliberately: with a counter each, a ticker that alternates between them
    resets both on every call and crosses no threshold ever — a silence worse
    than the one the threshold was added to end.
    """
    count = _failures.get(ticker, 0) + 1
    _failures[ticker] = count
    _LOG.warning(
        "candle data degraded for %s (%s consecutive): %s", ticker, count, reason
    )
    if count < _DEGRADED_BEFORE_ALERT or ticker in _alerted:
        return False
    _alerted.add(ticker)
    return True


async def _report(degraded: list[tuple[str, str]]) -> None:
    """One alert per call, naming every ticker that crossed in it.

    The call is the unit rather than the ticker: a broker outage degrades the
    whole watchlist at once, and one message per instrument turns the one
    channel the owner reads into noise.
    """
    if not degraded:
        return
    detail = ", ".join(f"{ticker} ({reason})" for ticker, reason in sorted(degraded))
    await alert(
        f"Candle data has been unusable for {_DEGRADED_BEFORE_ALERT} calls in a "
        f"row: {detail}. A failed fetch omits the ticker from the batch "
        "entirely; a short history means every strategy whose lookback exceeds "
        "it returns nothing. Either way that instrument is not being traded as "
        "configured."
    )


async def candles_for_watchlist(
    tickers: list[str], lookback: int, now: datetime
) -> dict[str, list[Candle]]:
    _reject_naive(now)
    since = now - timedelta(days=max(lookback, 1) * 3 + _CALENDAR_PAD)
    result: dict[str, list[Candle]] = {}
    degraded: list[tuple[str, str]] = []
    for ticker in tickers:
        try:
            instrument = await get_instrument(ticker)
            candles = await get_candles(
                instrument.figi,
                CandleInterval.CANDLE_INTERVAL_DAY,
                since,
                now,
            )
        except _ABSORBED as exc:
            failure = f"{type(exc).__name__}: {exc}"
            if _note_degraded(ticker, failure):
                degraded.append((ticker, failure))
            continue
        candles = sorted(candles, key=lambda candle: candle.timestamp)
        # Returned either way. `lookback` is the longest among the enabled
        # strategies, so a series too short for that one may still satisfy a
        # shorter one — reporting insufficiency must not become dropping the
        # ticker.
        result[ticker] = candles
        # `not candles` is not redundant with the comparison: no candles at all
        # is degraded whatever was asked for, including a lookback of zero.
        if not candles or len(candles) < lookback:
            shortfall = f"{len(candles)} candles, {lookback} needed"
            if _note_degraded(ticker, shortfall):
                degraded.append((ticker, shortfall))
            continue
        _note_success(ticker)
    await _report(degraded)
    return result
