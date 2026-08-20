"""Tests for sandbox.data — derived from the sandbox load contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from t_tech.invest.schemas import CandleInterval

from sandbox.data import load
from zarabot.models import Candle, Instrument

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
START = NOW - timedelta(days=5)
END = NOW


def _candle(day: int, close: int) -> Candle:
    stamp = datetime(2026, 3, 10 + day, 15, 0, tzinfo=UTC)
    price = Decimal(str(close))
    return Candle(
        timestamp=stamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
    )


def _instrument() -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    candles = [_candle(i, 100 + i) for i in range(6)]
    fetches: list[tuple[datetime, datetime]] = []

    async def _instrument_lookup(ticker: str) -> Instrument:
        return _instrument()

    async def _get_candles(
        figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        fetches.append((since, until))
        return [c for c in candles if since <= c.timestamp <= until]

    monkeypatch.setattr("sandbox.data.get_instrument", _instrument_lookup)
    monkeypatch.setattr("sandbox.data.get_candles", _get_candles)
    monkeypatch.setattr("sandbox.data._FETCHES", fetches, raising=False)
    return tmp_path / "cache"


async def test_load_returns_oldest_first(cache_dir: Path) -> None:
    candles = await load(
        "SBER",
        START,
        END,
        CandleInterval.CANDLE_INTERVAL_DAY,
        cache_dir,
    )
    assert candles
    assert all(isinstance(c, Candle) for c in candles)
    stamps = [c.timestamp for c in candles]
    assert stamps == sorted(stamps)
    assert isinstance(candles[0].close, Decimal)


async def test_load_rejects_naive_datetimes(cache_dir: Path) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await load("SBER", naive, END, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir)
    with pytest.raises(ValueError):
        await load("SBER", START, naive, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir)


async def test_load_empty_range_returns_empty_list(cache_dir: Path) -> None:
    empty_start = datetime(2025, 1, 1, tzinfo=UTC)
    empty_end = datetime(2025, 1, 2, tzinfo=UTC)
    candles = await load(
        "SBER",
        empty_start,
        empty_end,
        CandleInterval.CANDLE_INTERVAL_DAY,
        cache_dir,
    )
    assert candles == []


async def test_second_load_uses_cache_without_refetch(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await load(
        "SBER", START, END, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir
    )
    import sandbox.data as data

    fetches: list[tuple[datetime, datetime]] = data._FETCHES
    first_calls = len(fetches)
    second = await load(
        "SBER", START, END, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir
    )
    assert second == first
    assert len(fetches) == first_calls


async def test_cache_extends_only_the_missing_span(
    cache_dir: Path,
) -> None:
    mid = datetime(2026, 3, 13, 15, 0, tzinfo=UTC)
    await load("SBER", START, mid, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir)
    import sandbox.data as data

    fetches: list[tuple[datetime, datetime]] = data._FETCHES
    fetches.clear()
    await load("SBER", START, END, CandleInterval.CANDLE_INTERVAL_DAY, cache_dir)
    assert fetches
    for since, until in fetches:
        assert since >= mid or until <= mid + timedelta(days=1)
        assert not (since <= START and until >= END)
