#!/usr/bin/env python3
"""V14 - the order-state to operations join `ops.commissions` rests on (#246).

Spec v1.91 settles that a zero `executed_commission` at fill is an ABSENCE and
not a measurement: the live account reports zero on 22 of 22 real fills while
22 fee operations totalling 14.67 exist in the same feed for the same trades.
`broker.client` therefore records `None`, and `ops.commissions` resolves the
real number from the operations feed - joined by an identifier the broker
issued, never by matching on instrument, time and quantity, which is ambiguous
exactly when two similar orders are close together and was declined once
already for #8.

THE JOIN IS THE ONE PART THAT IS NOT MEASURED. It is inferred from the SDK's
own messages: `OrderState.stages[].trade_id` and `Operation.trades[].trade_id`
are the same named field on the two sides of the same API, so a match is a
match by construction. What nobody has confirmed is that a real fill populates
`stages` at all, or that the same identifier reaches the operations feed. That
is exactly the class of assumption that produced #39 and #43, and this script
is its gate.

Its failure mode is benign, which is why the code ships ahead of the
measurement: no join means the commission stays unknown and the once-per-order
alert fires. It cannot produce a wrong number, only a missing one.

Read-only. It calls `get_operations` and `get_order_state` and nothing else,
and it places, cancels and modifies nothing.

Not in `run_all.sh`. V12 and V13 set the precedent for a numbered check outside
the suite. RUN THIS BEFORE THE ONE-OFF HISTORY REPAIR in `ops/RUNBOOK.md`: a
repair run against a join that does not resolve alerts once per order and fixes
nothing.
"""

import datetime as dt
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
ACCOUNT = env("TINVEST_ACCOUNT_ID", secret=True)
DB_PATH = env("DB_PATH", required=False, default="zarabot.db")
WINDOW_DAYS = int(env("V14_WINDOW_DAYS", required=False, default="200"))

EXECUTED = "OPERATION_STATE_EXECUTED"
FEE_TYPES = frozenset(
    {
        "OPERATION_TYPE_ADVICE_FEE",
        "OPERATION_TYPE_BROKER_FEE",
        "OPERATION_TYPE_CASH_FEE",
        "OPERATION_TYPE_MARGIN_FEE",
        "OPERATION_TYPE_OTHER_FEE",
        "OPERATION_TYPE_OUT_FEE",
        "OPERATION_TYPE_SERVICE_FEE",
        "OPERATION_TYPE_SUCCESS_FEE",
        "OPERATION_TYPE_TRACK_MFEE",
        "OPERATION_TYPE_TRACK_PFEE",
    }
)

v = Verifier("V14", "order state to operations join by trade_id")


def _name(raw):
    if raw is None:
        return ""
    return getattr(raw, "name", None) or str(raw).rsplit(".", 1)[-1]


def _orders():
    """Filled orders the bot recorded, newest first, with their broker id.

    Read-only, and outside the repository layer deliberately: this is a
    verification script, not application code, and it must be runnable against
    a copy of the database with nothing else started.
    """
    conn = sqlite3.connect("file:{}?mode=ro".format(DB_PATH), uri=True)
    try:
        rows = conn.execute(
            """
            SELECT key, broker_order_id, settled_at
              FROM orders
             WHERE status = 'FILLED'
             ORDER BY settled_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return rows


async def body():
    from t_tech.invest.schemas import OrderIdType

    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=WINDOW_DAYS)

    try:
        orders = _orders()
    except sqlite3.Error as exc:
        v.check("the bot's database is readable", False, "{}".format(exc))
        return
    if not v.check(
        "the database holds filled orders to check",
        orders,
        "{} filled order row(s) in {}".format(len(orders), DB_PATH),
    ):
        v.note("nothing to join; run this after the account has traded")
        return

    async with client(TOKEN) as c:
        response = await c.operations.get_operations(
            account_id=ACCOUNT, from_=since, to=now
        )
        operations = [o for o in response.operations if _name(o.state) == EXECUTED]

        states = []
        for key, broker_order_id, _settled in orders:
            try:
                if broker_order_id:
                    found = await c.orders.get_order_state(
                        account_id=ACCOUNT,
                        order_id=broker_order_id,
                        order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
                    )
                else:
                    found = await c.orders.get_order_state(
                        account_id=ACCOUNT,
                        order_id=key,
                        order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
                    )
            except Exception as exc:  # noqa: BLE001
                # An order the broker will not resolve is a #8 finding, not a
                # finding about the join. Recorded and skipped.
                v.note("{}: not resolvable ({})".format(key, type(exc).__name__))
                continue
            states.append((key, found))

    v.note("{} executed operations in {} days".format(len(operations), WINDOW_DAYS))
    v.check(
        "at least one recorded order resolves at the broker",
        states,
        "{} of {} resolved".format(len(states), len(orders)),
    )

    # 1. Does a filled order carry execution stages at all? This is the first
    #    thing that can be false, and everything else rests on it.
    with_stages = [
        (key, found) for key, found in states if list(getattr(found, "stages", []) or [])
    ]
    v.check(
        "a filled order state carries `stages`",
        with_stages,
        "{} of {} order states have stages".format(len(with_stages), len(states)),
    )

    # 2. Do those stage trade ids appear on the operations feed?
    op_trade_ids = {}
    for op in operations:
        for trade in list(getattr(op, "trades", []) or []):
            if getattr(trade, "trade_id", None):
                op_trade_ids[str(trade.trade_id)] = op
    v.note("{} trade ids across the feed's operations".format(len(op_trade_ids)))

    joined = []
    unjoined = []
    for key, found in with_stages:
        ids = {
            str(stage.trade_id)
            for stage in found.stages
            if getattr(stage, "trade_id", None)
        }
        parents = {op_trade_ids[i].id for i in ids if i in op_trade_ids}
        if parents:
            joined.append((key, parents))
        else:
            unjoined.append(key)
    v.check(
        "an order's stage trade ids resolve to an operation on the feed",
        joined,
        "{} joined, {} did not".format(len(joined), len(unjoined)),
    )
    if unjoined:
        v.note("unjoined: {}".format(", ".join(unjoined[:5])))

    # 3. And does the joined trade have the fee this whole mechanism is for?
    #    A joined trade with NO fee child is REPORTED, not failed: spec v1.91
    #    calls that a measured zero, and this check cannot tell a
    #    commission-free trade from a broken join on a single row.
    with_fee = 0
    without_fee = []
    for key, parents in joined:
        fees = [
            op
            for op in operations
            if _name(op.operation_type) in FEE_TYPES
            and str(getattr(op, "parent_operation_id", "")) in parents
        ]
        if fees:
            with_fee += 1
        else:
            without_fee.append(key)
    v.check(
        "every joined trade carries a fee child",
        joined and not without_fee,
        "{} of {} joined orders have a fee; {} do not".format(
            with_fee, len(joined), len(without_fee)
        ),
    )
    if without_fee:
        v.note(
            "no fee child (a measured zero under v1.91, or a broken join): {}".format(
                ", ".join(without_fee[:5])
            )
        )


run(v, body)
