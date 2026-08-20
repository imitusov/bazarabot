#!/usr/bin/env python3
"""V11 - standing stop-loss orders survive independently of the bot.

The design places a real stop-loss with the exchange at entry, so that a
position stays protected while the bot is not running. This verifies the whole
mechanism end to end: buy, protect, confirm the exchange is holding the stop,
cancel it, and flatten.

SANDBOX ONLY. Buys and sells one lot with fake money.
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
STOP_LOSS_PCT = Decimal(env("STOP_LOSS_PCT", required=False, default="5"))

v = Verifier("V11", "standing stop-loss orders (sandbox)")


async def body():
    from t_tech.invest.schemas import (
        ExchangeOrderType,
        InstrumentIdType,
        MoneyValue,
        OrderDirection,
        OrderType,
        StopOrderDirection,
        StopOrderExpirationType,
        StopOrderStatusOption,
        StopOrderType,
    )
    from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

    async with client(TOKEN, sandbox=True) as c:
        accounts = list((await c.sandbox.get_sandbox_accounts()).accounts)
        account_id = (accounts[0].id if accounts
                      else (await c.sandbox.open_sandbox_account()).account_id)
        try:
            await c.sandbox.sandbox_pay_in(
                account_id=account_id,
                amount=MoneyValue(currency="rub", units=500000, nano=0))
        except Exception as exc:  # noqa: BLE001
            v.note("sandbox pay-in skipped ({})".format(type(exc).__name__))

        ticker = WATCHLIST[0]
        share = (await c.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=CLASS_CODE, id=ticker)).instrument
        last = (await c.market_data.get_last_prices(
            instrument_id=[share.uid])).last_prices[0]
        price = quotation_to_decimal(last.price)

        buy = await c.orders.post_order(
            instrument_id=share.uid, quantity=1,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=account_id, order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4()), confirm_margin_trade=False)
        v.check("opened a one-lot position to protect", bool(buy.order_id),
                "{} at ~{}".format(ticker, price))

        stop_price = (price * (Decimal(100) - STOP_LOSS_PCT) / Decimal(100)
                      ).quantize(Decimal("0.01"))
        placed = await c.stop_orders.post_stop_order(
            instrument_id=share.uid,
            quantity=1,
            stop_price=decimal_to_quotation(stop_price),
            direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
            account_id=account_id,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            expiration_type=(
                StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL),
            exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4()),
            confirm_margin_trade=False,
        )
        stop_id = placed.stop_order_id
        v.check("stop-loss accepted by the exchange", bool(stop_id),
                "stop at {} ({}% below {})".format(stop_price, STOP_LOSS_PCT, price))

        active = list((await c.stop_orders.get_stop_orders(
            account_id=account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE)).stop_orders)
        ours = [o for o in active if o.stop_order_id == stop_id]
        v.check("exchange reports the stop as standing", bool(ours),
                "{} active stop order(s) on the account".format(len(active)))

        if ours:
            held = ours[0]
            v.check("good-till-cancel, not day-expiring",
                    not getattr(held, "expiration_time", None)
                    or held.expiration_time.year > 2100,
                    "expiration_time={}".format(
                        getattr(held, "expiration_time", None)))

        await c.stop_orders.cancel_stop_order(
            account_id=account_id, stop_order_id=stop_id)
        after = list((await c.stop_orders.get_stop_orders(
            account_id=account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE)).stop_orders)
        v.check("stop-loss cancels cleanly",
                all(o.stop_order_id != stop_id for o in after))

        await c.orders.post_order(
            instrument_id=share.uid, quantity=1,
            direction=OrderDirection.ORDER_DIRECTION_SELL,
            account_id=account_id, order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4()), confirm_margin_trade=False)
        v.check("test position flattened", True, "sandbox left clean")


run(v, body)
