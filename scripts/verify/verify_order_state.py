#!/usr/bin/env python3
"""V6 - order recovery by client idempotency key, across a real process boundary.

This is the verification the crash-safety design rests on. It deliberately uses
two separate OS processes: process A places an order and exits, taking its
memory with it; process B starts knowing only the idempotency key from the
database and must determine what happened. That is exactly the situation after
a crash mid-submission.

Both recovery mechanisms are checked:
  1. get_order_state(order_id=<our key>, order_id_type=ORDER_ID_TYPE_REQUEST)
  2. re-posting the same key, which must return the existing order

SANDBOX ONLY. Places a real (unfillable) order.
"""

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env  # noqa: E402

STATE = pathlib.Path(tempfile.gettempdir()) / "zarabot_v6_state.json"
TOKEN = env("TINVEST_TOKEN", secret=True)
WATCHLIST_RAW = env("WATCHLIST")
CLASS_CODE = env("MOEX_CLASS_CODE", required=False, default="TQBR")


async def sandbox_account(c):
    accounts = list((await c.sandbox.get_sandbox_accounts()).accounts)
    if accounts:
        return accounts[0].id
    return (await c.sandbox.open_sandbox_account()).account_id


async def phase_one():
    """Place an order, persist only the key, then exit — as a crash would."""
    from t_tech.invest.schemas import (
        InstrumentIdType,
        OrderDirection,
        OrderType,
    )
    from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

    ticker = WATCHLIST_RAW.split(",")[0].strip().upper()
    async with client(TOKEN, sandbox=True) as c:
        account_id = await sandbox_account(c)
        share = (await c.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=CLASS_CODE, id=ticker)).instrument
        last = (await c.market_data.get_last_prices(
            instrument_id=[share.uid])).last_prices[0]
        price = (quotation_to_decimal(last.price) * Decimal("0.5")).quantize(
            Decimal("0.01"))

        key = str(uuid.uuid4())
        # The order of these two operations is the entire point: in the real
        # system the key is durably recorded BEFORE the broker is called.
        STATE.write_text(json.dumps({
            "key": key, "account_id": account_id,
            "uid": share.uid, "price": str(price), "ticker": ticker,
        }))

        posted = await c.orders.post_order(
            instrument_id=share.uid, quantity=1,
            price=decimal_to_quotation(price),
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=account_id, order_type=OrderType.ORDER_TYPE_LIMIT,
            order_id=key, confirm_margin_trade=False,
        )
        print("   .   placed {} with key {} (exchange id {})".format(
            ticker, key, posted.order_id))
    return 0


async def phase_two():
    """Start cold. Recover the order knowing only the key."""
    from t_tech.invest.schemas import (
        OrderDirection,
        OrderIdType,
        OrderType,
    )
    from t_tech.invest.utils import decimal_to_quotation

    state = json.loads(STATE.read_text())
    key = state["key"]
    account_id = state["account_id"]
    results = {}

    async with client(TOKEN, sandbox=True) as c:
        try:
            found = await c.orders.get_order_state(
                account_id=account_id, order_id=key,
                order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)
            exchange_id = found.order_id
            results["lookup_ok"] = bool(exchange_id)
            results["lookup_detail"] = "resolved to exchange id {}, status {}".format(
                exchange_id, getattr(found.execution_report_status, "name",
                                     found.execution_report_status))
        except Exception as exc:  # noqa: BLE001
            results["lookup_ok"] = False
            results["lookup_detail"] = "{}: {}".format(type(exc).__name__, exc)
            exchange_id = None

        try:
            again = await c.orders.post_order(
                instrument_id=state["uid"], quantity=1,
                price=decimal_to_quotation(Decimal(state["price"])),
                direction=OrderDirection.ORDER_DIRECTION_BUY,
                account_id=account_id, order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=key, confirm_margin_trade=False,
            )
            same = exchange_id is not None and again.order_id == exchange_id
            results["idempotent_ok"] = same
            results["idempotent_detail"] = (
                "resubmit returned the same order" if same
                else "resubmit returned {}, expected {}".format(
                    again.order_id, exchange_id))
        except Exception as exc:  # noqa: BLE001
            results["idempotent_ok"] = False
            results["idempotent_detail"] = "{}: {}".format(type(exc).__name__, exc)

        if exchange_id:
            try:
                await c.orders.cancel_order(
                    account_id=account_id, order_id=exchange_id)
                results["cleanup_ok"] = True
                results["cleanup_detail"] = "test order cancelled"
            except Exception as exc:  # noqa: BLE001
                results["cleanup_ok"] = False
                results["cleanup_detail"] = "{}: {}".format(type(exc).__name__, exc)
        else:
            results["cleanup_ok"] = False
            results["cleanup_detail"] = "nothing to cancel - lookup failed"

    STATE.write_text(json.dumps(dict(state, results=results)))
    for label in ("lookup", "idempotent", "cleanup"):
        print("   .   {}: {}".format(label, results.get(label + "_detail")))
    return 0 if all(results.get(k) for k in
                    ("lookup_ok", "idempotent_ok", "cleanup_ok")) else 1


def orchestrate():
    v = Verifier("V6", "order recovery by idempotency key across processes")
    script = str(pathlib.Path(__file__).resolve())

    one = subprocess.run([sys.executable, script, "--phase", "1"])
    if not v.check("process A placed an order and exited", one.returncode == 0):
        v.finish()

    two = subprocess.run([sys.executable, script, "--phase", "2"])
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    results = state.get("results", {})

    v.check("process B recovered the order using only the client key",
            results.get("lookup_ok"), results.get("lookup_detail", ""))
    v.check("re-submitting the same key returned the existing order "
            "rather than creating a second one",
            results.get("idempotent_ok"), results.get("idempotent_detail", ""))
    v.check("test order cleaned up", results.get("cleanup_ok"),
            results.get("cleanup_detail", ""))
    v.check("process B exited cleanly", two.returncode == 0)
    v.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, default=0)
    args = parser.parse_args()
    if args.phase == 1:
        sys.exit(asyncio.run(phase_one()))
    elif args.phase == 2:
        sys.exit(asyncio.run(phase_two()))
    else:
        orchestrate()
