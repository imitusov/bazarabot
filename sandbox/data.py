"""Historical candle loading for laptop research. Never imported by zarabot/."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from t_tech.invest.schemas import CandleInterval

from zarabot.broker.client import get_candles, get_instrument
from zarabot.models import Candle

_DEFAULT_CACHE = Path("sandbox/cache")


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _cache_path(cache_dir: Path, ticker: str, interval: CandleInterval) -> Path:
    name = getattr(interval, "name", str(interval))
    return cache_dir / f"{ticker}_{name}.parquet"


def _aware(stamp: object) -> datetime:
    if isinstance(stamp, datetime):
        moment = stamp
    else:
        moment = pd.Timestamp(stamp).to_pydatetime()
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _candles_from_frame(frame: pd.DataFrame) -> list[Candle]:
    candles: list[Candle] = []
    for row in frame.itertuples(index=False):
        candles.append(
            Candle(
                timestamp=_aware(row.timestamp),
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
            )
        )
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def _frame_from_candles(candles: list[Candle]) -> pd.DataFrame:
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if not candles:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "timestamp": candle.timestamp,
                "open": str(candle.open),
                "high": str(candle.high),
                "low": str(candle.low),
                "close": str(candle.close),
                "volume": candle.volume,
            }
            for candle in candles
        ]
    )


def _read_parquet(
    path: Path,
) -> tuple[list[Candle], datetime | None, datetime | None]:
    if not path.is_file():
        return [], None, None
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    covered_start = None
    covered_end = None
    if b"covered_start" in meta:
        covered_start = _aware(meta[b"covered_start"].decode())
    if b"covered_end" in meta:
        covered_end = _aware(meta[b"covered_end"].decode())
    frame = table.to_pandas()
    if frame.empty:
        return [], covered_start, covered_end
    candles = _candles_from_frame(frame)
    if covered_start is None and candles:
        covered_start = candles[0].timestamp
    if covered_end is None and candles:
        covered_end = candles[-1].timestamp
    return candles, covered_start, covered_end


def _write_parquet(
    path: Path,
    candles: list[Candle],
    covered_start: datetime,
    covered_end: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = _frame_from_candles(candles)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    table = table.replace_schema_metadata(
        {
            b"covered_start": covered_start.isoformat().encode(),
            b"covered_end": covered_end.isoformat().encode(),
        }
    )
    pq.write_table(table, path)


def _merge(existing: list[Candle], incoming: list[Candle]) -> list[Candle]:
    by_time = {candle.timestamp: candle for candle in existing}
    for candle in incoming:
        by_time[candle.timestamp] = candle
    return sorted(by_time.values(), key=lambda candle: candle.timestamp)


def _in_range(candles: list[Candle], start: datetime, end: datetime) -> list[Candle]:
    return [c for c in candles if start <= c.timestamp <= end]


async def load(
    ticker: str,
    start: datetime,
    end: datetime,
    interval: CandleInterval,
    cache_dir: Path = _DEFAULT_CACHE,
) -> list[Candle]:
    """Return candles oldest-first, extending a parquet cache on miss."""
    _reject_naive(start)
    _reject_naive(end)
    path = _cache_path(cache_dir, ticker, interval)
    cached, covered_start, covered_end = await asyncio.to_thread(_read_parquet, path)
    fetches: list[tuple[datetime, datetime]] = []
    if covered_start is None or covered_end is None:
        fetches.append((start, end))
        covered_start, covered_end = start, end
    else:
        if start < covered_start:
            fetches.append((start, covered_start))
            covered_start = start
        if end > covered_end:
            fetches.append((covered_end, end))
            covered_end = end
    if fetches:
        instrument = await get_instrument(ticker)
        incoming: list[Candle] = []
        for since, until in fetches:
            incoming.extend(await get_candles(instrument.figi, interval, since, until))
        cached = _merge(cached, incoming)
        await asyncio.to_thread(
            _write_parquet, path, cached, covered_start, covered_end
        )
    return _in_range(cached, start, end)
