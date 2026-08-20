"""Tests for zarabot.market.data — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    instruments = {"SBER": _instrument("SBER"), "GAZP": _instrument("GAZP")}
    series = {"SBER": _candles(40), "GAZP": _candles(10)}
    state: dict[str, object] = {"fail": set()}

    async def get_instrument(ticker: str) -> Instrument:
        if ticker in state["fail"]:  # type: ignore[operator]
            raise RuntimeError("unavailable")
        return instruments[ticker]

    async def get_candles(
        figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        ticker = figi.removeprefix("FIGI-")
        if ticker in state["fail"]:  # type: ignore[operator]
            raise RuntimeError("unavailable")
        return series[ticker]

    monkeypatch.setattr("zarabot.market.data.get_instrument", get_instrument)
    monkeypatch.setattr("zarabot.market.data.get_candles", get_candles)
    return state


async def test_watchlist_candles_are_oldest_first_and_aware(broker: dict[str, object]) -> None:
    result = await candles_for_watchlist(["SBER"], 20, NOW)
    candles = result["SBER"]
    assert candles
    assert candles == sorted(candles, key=lambda c: c.timestamp)
    assert candles[-1].timestamp >= candles[0].timestamp
    assert all(c.timestamp.tzinfo is not None for c in candles)


async def test_short_history_is_returned_unpadded(broker: dict[str, object]) -> None:
    result = await candles_for_watchlist(["GAZP"], 30, NOW)
    assert len(result["GAZP"]) == 10
    assert len(result["GAZP"]) < 30


async def test_one_failing_ticker_does_not_fail_the_batch(
    broker: dict[str, object],
) -> None:
    fail = broker["fail"]
    assert isinstance(fail, set)
    fail.add("GAZP")
    result = await candles_for_watchlist(["SBER", "GAZP"], 20, NOW)
    assert "SBER" in result
    assert "GAZP" not in result
