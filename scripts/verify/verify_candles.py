#!/usr/bin/env python3
"""V4 - sufficient candle history, timezone-aware, without large gaps.

250 trading days is the floor because the longest strategy lookback plus a
usable backtest window cannot be satisfied by less.
"""

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, env_list, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
WATCHLIST = env_list("WATCHLIST")
CLASS_CODE = env("MOEX_CLASS_CODE", required=False, default="TQBR")

REQUIRED_DAYS = 250
MAX_GAP_CALENDAR_DAYS = 5  # a weekend plus a holiday; more implies missing data

v = Verifier("V4", "candle history depth and continuity")


async def body():
    from t_tech.invest.schemas import CandleInterval, InstrumentIdType

    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=500)

    async with client(TOKEN) as c:
        for ticker in WATCHLIST:
            try:
                share = (await c.instruments.share_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                    class_code=CLASS_CODE, id=ticker)).instrument
                response = await c.market_data.get_candles(
                    instrument_id=share.uid,
                    from_=since,
                    to=now,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                )
            except Exception as exc:  # noqa: BLE001
                v.check("{}: candles retrieved".format(ticker), False,
                        "{}: {}".format(type(exc).__name__, exc))
                continue

            candles = list(response.candles)
            times = [c_.time for c_ in candles]

            aware = all(t.tzinfo is not None for t in times)
            ordered = times == sorted(times)
            gap = max(
                ((times[i + 1] - times[i]).days for i in range(len(times) - 1)),
                default=0,
            )

            ok = (len(candles) >= REQUIRED_DAYS and aware and ordered
                  and gap <= MAX_GAP_CALENDAR_DAYS)
            v.check(
                "{}: {} candles, oldest-first={}, tz-aware={}, largest gap={}d".format(
                    ticker, len(candles), ordered, aware, gap),
                ok,
                "need >= {} candles and gaps <= {}d".format(
                    REQUIRED_DAYS, MAX_GAP_CALENDAR_DAYS),
            )


run(v, body)
