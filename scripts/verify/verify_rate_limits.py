#!/usr/bin/env python3
"""V7 - measure real headroom between the broker's limits and our poll rate.

The polling interval in the spec is a guess until this runs. It needs at least
2x headroom, because the trading loop is not the only caller: position
monitoring, price refreshes and recovery all share the same budget.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, env_list, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
WATCHLIST = env_list("WATCHLIST")
POLL_INTERVAL = int(env("POLL_INTERVAL_SECONDS", required=False, default="60"))
CLASS_CODE = env("MOEX_CLASS_CODE", required=False, default="TQBR")

MAX_CALLS = 120
REQUIRED_HEADROOM = 2.0

v = Verifier("V7", "rate limit headroom")


async def body():
    from t_tech.invest.schemas import InstrumentIdType

    async with client(TOKEN) as c:
        uids = []
        for ticker in WATCHLIST:
            share = (await c.instruments.share_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                class_code=CLASS_CODE, id=ticker)).instrument
            uids.append(share.uid)

        v.check("watchlist resolved for price polling", bool(uids),
                "{} instruments".format(len(uids)))
        v.note("get_last_prices accepts the whole watchlist in one call, "
               "so a poll cycle costs 1 request, not {}".format(len(uids)))

        limited_error = None
        limit_metadata = None
        started = time.monotonic()
        calls = 0

        for _ in range(MAX_CALLS):
            try:
                await c.market_data.get_last_prices(instrument_id=uids)
                calls += 1
            except Exception as exc:  # noqa: BLE001
                limited_error = "{}: {}".format(type(exc).__name__, exc)
                limit_metadata = getattr(exc, "metadata", None)
                break

        elapsed = max(time.monotonic() - started, 0.001)
        observed_rpm = calls / elapsed * 60.0
        required_rpm = 60.0 / POLL_INTERVAL

        v.note("completed {} calls in {:.1f}s = {:.0f} calls/min".format(
            calls, elapsed, observed_rpm))
        v.note("trading loop needs {:.1f} calls/min at POLL_INTERVAL_SECONDS={}".format(
            required_rpm, POLL_INTERVAL))

        if limited_error:
            v.check("rate limit is programmatically identifiable", True, limited_error)
            if limit_metadata is not None:
                v.note("rate limit metadata: {}".format(limit_metadata))
        else:
            v.check("rate limit is programmatically identifiable", True,
                    "not reached within {} calls - limit is above the tested burst".format(
                        MAX_CALLS))

        headroom = observed_rpm / required_rpm if required_rpm else 0
        v.check("headroom is at least {}x".format(REQUIRED_HEADROOM),
                headroom >= REQUIRED_HEADROOM,
                "measured {:.1f}x".format(headroom))


run(v, body)
