#!/usr/bin/env python3
"""V13 - the trading-status endpoint, which the instruments cache must not cache.

Spec v1.85 gives `broker.client` an instruments cache with a 24-hour window,
and carves one field out of it: `trading_status` is never served from the
table. `risk.gate` rejects on that field, so a stored `NORMAL_TRADING` is an
entry sized into a volatility halt. Every call therefore reads it live, from
the market-data service, on its own - and that read is the first thing in this
project to depend on that endpoint.

This is the check that stops it being written against a mock that agrees with
the guess (failure class 11). It proves three things, for every watchlist
ticker without exception:

  1. the endpoint exists on the pinned SDK and resolves the ticker's FIGI;
  2. the status it returns is a `SECURITY_TRADING_STATUS_` name, and the
     string derived from it equals the string `broker.client` already derives
     from `share_by` for the same instrument in the same minute - the cache is
     only safe if the two sources agree, since the cached row's other fields
     come from `share_by`;
  3. one call per watchlist ticker per poll fits the market-data budget with
     at least 2x headroom, measured the way V7 measures the candle endpoint.

Read-only. Not in `run_all.sh`: it gates one §4 branch, not the project, and
the suite stops at the first failure. V12 is the precedent.
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

_STATUS_PREFIX = "SECURITY_TRADING_STATUS_"
MAX_CALLS = 60
REQUIRED_HEADROOM = 2.0

v = Verifier("V13", "trading status read, live and uncached")


def _name(raw):
    """The enum member's name, however the SDK chooses to express it."""
    if raw is None:
        return ""
    return getattr(raw, "name", None) or str(raw).rsplit(".", 1)[-1]


def _derive(raw):
    """`broker.client._trading_status`, reproduced exactly (client.py:317)."""
    name = _name(raw)
    if name.startswith(_STATUS_PREFIX):
        return name[len(_STATUS_PREFIX) :]
    return str(name)


async def body():
    from t_tech.invest.schemas import InstrumentIdType

    async with client(TOKEN) as c:
        service = getattr(c.market_data, "get_trading_status", None)
        v.check(
            "the market-data service exposes get_trading_status on SDK 1.49.1",
            callable(service),
            "absent - the cache's live-status read has no source"
            if not callable(service)
            else "present",
        )
        if not callable(service):
            return

        shares = {}
        for ticker in WATCHLIST:
            share = (
                await c.instruments.share_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                    class_code=CLASS_CODE,
                    id=ticker,
                )
            ).instrument
            shares[ticker] = share

        v.check(
            "every watchlist ticker resolves through share_by",
            len(shares) == len(WATCHLIST),
            "{} of {}".format(len(shares), len(WATCHLIST)),
        )

        resolved = 0
        disagreements = []
        for ticker, share in shares.items():
            status = await c.market_data.get_trading_status(figi=share.figi)
            live = _derive(getattr(status, "trading_status", None))
            cached = _derive(share.trading_status)
            if live:
                resolved += 1
            v.note("{:6} share_by={:24} market_data={}".format(ticker, cached, live))
            if live != cached:
                disagreements.append((ticker, cached, live))

        v.check(
            "the endpoint resolves every watchlist FIGI",
            resolved == len(shares),
            "{} of {} returned a status".format(resolved, len(shares)),
        )
        v.check(
            "both sources derive the same string for the same instrument",
            not disagreements,
            "disagree: {}".format(disagreements)
            if disagreements
            else "all {} agree".format(len(shares)),
        )

        # Headroom, V7's method: hammer one FIGI, measure the achievable rate,
        # compare against what one call per ticker per poll actually needs.
        figi = next(iter(shares.values())).figi
        started = time.monotonic()
        calls = 0
        for _ in range(MAX_CALLS):
            await c.market_data.get_trading_status(figi=figi)
            calls += 1
        elapsed = max(time.monotonic() - started, 0.001)
        observed_rpm = calls / elapsed * 60.0
        required_rpm = len(WATCHLIST) * (60.0 / POLL_INTERVAL)
        headroom = observed_rpm / required_rpm if required_rpm else 0

        v.note(
            "{} calls in {:.1f}s = {:.0f} req/min observed".format(
                calls, elapsed, observed_rpm
            )
        )
        v.note(
            "{} tickers every {}s = {:.1f} req/min required".format(
                len(WATCHLIST), POLL_INTERVAL, required_rpm
            )
        )
        v.check(
            "at least {}x headroom over one status read per ticker per poll".format(
                REQUIRED_HEADROOM
            ),
            headroom >= REQUIRED_HEADROOM,
            "{:.0f}x".format(headroom),
        )


run(v, body)
