#!/usr/bin/env python3
"""V2 - the token can place and cancel an order. SANDBOX ONLY.

Never runs against the live account: it places a real order. The limit price is
set far below the market so it cannot fill even if the endpoint were
misconfigured.
"""

import pathlib
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, env_list, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
WATCHLIST = env_list("WATCHLIST")
CLASS_CODE = env("MOEX_CLASS_CODE", required=False, default="TQBR")

v = Verifier("V2", "order placement and cancellation (sandbox)")


async def ensure_sandbox_account(c, v):
    accounts = list((await c.sandbox.get_sandbox_accounts()).accounts)
    if not accounts:
        opened = await c.sandbox.open_sandbox_account()
        v.note("opened sandbox account {}".format(opened.account_id))
        return opened.account_id
    return accounts[0].id


async def body():
    from t_tech.invest.schemas import (
        InstrumentIdType,
        MoneyValue,
        OrderDirection,
        OrderType,
    )
    from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

    async with client(TOKEN, sandbox=True) as c:
        account_id = await ensure_sandbox_account(c, v)
        v.check("sandbox account available", bool(account_id), account_id)

        try:
            await c.sandbox.sandbox_pay_in(
                account_id=account_id,
                amount=MoneyValue(currency="rub", units=500000, nano=0))
            v.note("sandbox account funded")
        except Exception as exc:  # noqa: BLE001
            v.note("sandbox pay-in skipped ({})".format(type(exc).__name__))

        ticker = WATCHLIST[0]
        share = (await c.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=CLASS_CODE, id=ticker)).instrument

        last = (await c.market_data.get_last_prices(
            instrument_id=[share.uid])).last_prices[0]
        market_price = quotation_to_decimal(last.price)
        safe_price = (market_price * Decimal("0.5")).quantize(Decimal("0.01"))
        v.note("{} market {} - placing unfillable limit at {}".format(
            ticker, market_price, safe_price))

        key = str(uuid.uuid4())
        v.check("idempotency key fits the 36-character limit", len(key) <= 36,
                "{} chars".format(len(key)))

        posted = await c.orders.post_order(
            instrument_id=share.uid,
            quantity=1,
            price=decimal_to_quotation(safe_price),
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=account_id,
            order_type=OrderType.ORDER_TYPE_LIMIT,
            order_id=key,
            confirm_margin_trade=False,
        )
        v.check("order accepted by the broker", bool(posted.order_id),
                "exchange id {}".format(posted.order_id))

        await c.orders.cancel_order(account_id=account_id, order_id=posted.order_id)
        state = await c.orders.get_order_state(
            account_id=account_id, order_id=posted.order_id)
        status = getattr(state.execution_report_status, "name",
                         str(state.execution_report_status))
        v.check("order reaches a cancelled terminal state",
                "CANCELLED" in status.upper() or "REJECTED" in status.upper(),
                status)


run(v, body)
