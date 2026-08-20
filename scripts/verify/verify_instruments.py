#!/usr/bin/env python3
"""V3 - every watchlist instrument resolves with the metadata sizing needs.

Position sizing is meaningless without lot size, so a single unresolved ticker
fails the whole script. The bot must not start against a watchlist it cannot
fully price.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, env_list, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
WATCHLIST = env_list("WATCHLIST")
CLASS_CODE = env("MOEX_CLASS_CODE", required=False, default="TQBR")

v = Verifier("V3", "watchlist instrument metadata")


async def body():
    from t_tech.invest.schemas import InstrumentIdType
    from t_tech.invest.utils import quotation_to_decimal

    async with client(TOKEN) as c:
        for ticker in WATCHLIST:
            try:
                response = await c.instruments.share_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                    class_code=CLASS_CODE,
                    id=ticker,
                )
            except Exception as exc:  # noqa: BLE001
                v.check("{}: resolves".format(ticker), False,
                        "{}: {}".format(type(exc).__name__, exc))
                continue

            share = response.instrument
            increment = quotation_to_decimal(share.min_price_increment)
            ok = (
                bool(share.figi)
                and share.lot > 0
                and share.currency.lower() == "rub"
                and share.api_trade_available_flag
                and increment > 0
            )
            v.check(
                "{}: lot={} currency={} api_trade={} step={}".format(
                    ticker, share.lot, share.currency,
                    share.api_trade_available_flag, increment),
                ok,
                "figi {}".format(share.figi),
            )


run(v, body)
