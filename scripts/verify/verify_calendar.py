#!/usr/bin/env python3
"""V5 - the trading calendar is queryable rather than inferred.

The session guard depends on asking the broker which days and hours are
tradeable. If this has to be hardcoded, the bot will eventually try to trade a
closed exchange on a Russian public holiday.
"""

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
EXCHANGE_HINT = env("MOEX_EXCHANGE", required=False, default="MOEX")

v = Verifier("V5", "exchange trading calendar")


async def body():
    now = dt.datetime.now(dt.timezone.utc)

    async with client(TOKEN) as c:
        # The broker measures the horizon from the START OF THE DAY, and says so
        # when you get it wrong: "The required period should not exceed 14 days".
        # from_=now with a 14-day span is rejected with INVALID_ARGUMENT/30002,
        # which is the defect that kept the bot from ever seeing a session (#39).
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        response = await c.instruments.trading_schedules(
            exchange=EXCHANGE_HINT,
            from_=day_start, to=day_start + dt.timedelta(days=14))

        exchanges = list(response.exchanges)
        v.check("trading schedules are retrievable", bool(exchanges),
                "{} exchange(s) returned".format(len(exchanges)))

        matches = [e for e in exchanges if EXCHANGE_HINT in e.exchange.upper()]
        if not v.check("an exchange matching '{}' is present".format(EXCHANGE_HINT),
                       bool(matches),
                       "available: {}".format(
                           ", ".join(sorted(e.exchange for e in exchanges))[:200])):
            return

        schedule = matches[0]
        days = list(schedule.days)
        v.check("14-day horizon is populated", len(days) >= 10,
                "{} days for {}".format(len(days), schedule.exchange))

        trading = [d for d in days if d.is_trading_day]
        closed = [d for d in days if not d.is_trading_day]
        v.check("trading and non-trading days are distinguished",
                bool(trading) and bool(closed),
                "{} trading, {} closed".format(len(trading), len(closed)))

        if trading:
            day = trading[0]
            aware = (day.start_time.tzinfo is not None
                     and day.end_time.tzinfo is not None)
            v.check("session times are timezone-aware", aware,
                    "{} .. {}".format(day.start_time, day.end_time))
            msk = dt.timezone(dt.timedelta(hours=3))
            v.note("next session in MSK: {} .. {}".format(
                day.start_time.astimezone(msk).strftime("%Y-%m-%d %H:%M"),
                day.end_time.astimezone(msk).strftime("%H:%M")))


run(v, body)
