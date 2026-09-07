"""Tests for zarabot.market.data — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import zarabot.market.data as data_mod
from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
)
from zarabot.market.data import candles_for_watchlist
from zarabot.models import Candle, Instrument

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)


def _instrument(ticker: str) -> Instrument:
    return Instrument(
        figi=f"FIGI-{ticker}",
        ticker=ticker,
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


def _candles(count: int) -> list[Candle]:
    start = NOW - timedelta(days=count)
    out: list[Candle] = []
    for i in range(count):
        price = Decimal("100") + Decimal(i)
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


@pytest.fixture(autouse=True)
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Captured alerts, and the per-ticker counters cleared between tests."""
    data_mod._failures.clear()
    data_mod._alerted.clear()
    sent: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        sent.append(text)

    monkeypatch.setattr(data_mod, "alert", _alert, raising=False)
    return sent


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> dict[str, BaseException]:
    """Maps a ticker to the exception its fetch raises. Empty means success."""
    instruments = {"SBER": _instrument("SBER"), "GAZP": _instrument("GAZP")}
    series = {"SBER": _candles(40), "GAZP": _candles(10)}
    fail: dict[str, BaseException] = {}

    async def get_instrument(ticker: str) -> Instrument:
        error = fail.get(ticker)
        if isinstance(error, InstrumentNotFound):
            raise error
        return instruments[ticker]

    async def get_candles(
        figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        ticker = figi.removeprefix("FIGI-")
        error = fail.get(ticker)
        if error is not None:
            raise error
        return series[ticker]

    monkeypatch.setattr(data_mod, "get_instrument", get_instrument)
    monkeypatch.setattr(data_mod, "get_candles", get_candles)
    return fail


async def test_watchlist_candles_are_oldest_first_and_aware(
    broker: dict[str, BaseException],
) -> None:
    result = await candles_for_watchlist(["SBER"], 20, NOW)
    candles = result["SBER"]
    assert candles
    assert candles == sorted(candles, key=lambda c: c.timestamp)
    assert candles[-1].timestamp >= candles[0].timestamp
    assert all(c.timestamp.tzinfo is not None for c in candles)


async def test_short_history_is_returned_unpadded(
    broker: dict[str, BaseException],
) -> None:
    result = await candles_for_watchlist(["GAZP"], 30, NOW)
    assert len(result["GAZP"]) == 10
    assert len(result["GAZP"]) < 30


async def test_one_failing_ticker_does_not_fail_the_batch(
    broker: dict[str, BaseException],
) -> None:
    broker["GAZP"] = BrokerUnavailable("broker unavailable: UNAVAILABLE")
    result = await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert "SBER" in result
    assert "GAZP" not in result


@pytest.mark.parametrize(
    "error",
    [
        BrokerUnavailable("broker unavailable: UNAVAILABLE"),
        BrokerRateLimited(30),
        InstrumentNotFound("no instrument found with ticker GAZP"),
    ],
)
async def test_three_consecutive_failures_alert_exactly_once(
    broker: dict[str, BaseException], alerts: list[str], error: BaseException
) -> None:
    """The threshold and the latch, together: rule 9."""
    broker["GAZP"] = error
    await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert alerts == [], "alerted before the third consecutive failure"

    await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert len(alerts) == 1
    assert "GAZP" in alerts[0]
    assert type(error).__name__ in alerts[0]

    await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert len(alerts) == 1, "a fourth failure alerted a second time"


async def test_recovery_re_arms_the_alert(
    broker: dict[str, BaseException], alerts: list[str]
) -> None:
    """A latch set once per process and never reset is #32 and #48."""
    broker["GAZP"] = BrokerUnavailable("broker unavailable: UNAVAILABLE")
    for _ in range(3):
        await candles_for_watchlist(["GAZP"], 20, NOW)
    assert len(alerts) == 1

    del broker["GAZP"]
    result = await candles_for_watchlist(["GAZP"], 20, NOW)
    assert result["GAZP"]
    assert len(alerts) == 1, "recovery itself alerted"

    broker["GAZP"] = BrokerUnavailable("broker unavailable: UNAVAILABLE")
    for _ in range(2):
        await candles_for_watchlist(["GAZP"], 20, NOW)
    assert len(alerts) == 1, "the count was not cleared by the success"

    await candles_for_watchlist(["GAZP"], 20, NOW)
    assert len(alerts) == 2


async def test_two_tickers_crossing_together_share_one_alert(
    broker: dict[str, BaseException], alerts: list[str]
) -> None:
    """The call is the unit: a watchlist-wide outage is one message."""
    broker["SBER"] = BrokerUnavailable("broker unavailable: UNAVAILABLE")
    broker["GAZP"] = BrokerUnavailable("broker unavailable: UNAVAILABLE")
    for _ in range(3):
        await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert len(alerts) == 1
    assert "SBER" in alerts[0]
    assert "GAZP" in alerts[0]


async def test_a_non_broker_exception_propagates(
    broker: dict[str, BaseException], alerts: list[str]
) -> None:
    """A renamed SDK field is a bug, not a missing instrument (#23)."""
    broker["GAZP"] = AttributeError("'Share' object has no attribute 'figi'")
    with pytest.raises(AttributeError):
        await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert alerts == []
