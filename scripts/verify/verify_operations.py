#!/usr/bin/env python3
"""V12 - the operations feed, which is load-bearing for money.

`broker.reconcile` books an external close from this feed: the sale's price,
its timestamp and its commission all come from here, and nothing downstream
can tell an invented number from a real one (rule 33). `execution.orders`
reads commission from the order state, not from here - so this script is
about the reconcile path.

Five assumptions were written from the SDK's type stubs and have never been
measured. Each one is a check below, and each fails quietly in production if
wrong:

  1. `quantity` - lots or instrument units? `_resolve_sale` computes a
     weighted average (`gross / units`), which is unit-invariant, so this is
     recorded rather than asserted. Read the note, do not "fix" the parse.
  2. `parent_operation_id` - fee rows point at their trade. If a fee's parent
     is outside the queried window, `sum(... if item.parent_operation_id in
     sale_ids)` sums an empty set to Decimal(0) - a commission of zero that
     looks like a measurement.
  3. `_FEE_TYPES` / the executed-state name are STRING sets compared against
     `_enum_name(...)`. An SDK wheel that renames a member produces zero
     commission with no error.
  4. Non-executed rows are dropped. If everything is dropped the position is
     left open and alerted, which is loud - confirm it stays loud.
  5. `price` is booked directly as the exit price.

Read-only. It calls `get_operations` and nothing else.

Not in `run_all.sh`, and deliberately. The SELL check FAILS today: the account
has never sold, so `_resolve_sale` - the whole reason this script exists - has
no data to run against. That is the finding, not a defect in the script, and a
suite that stops at the first failure would be stopped by it forever. Run this
on demand, and expect 6/6 only once a real sale has settled. Do not place a
trade to make it green, and do not soften the check to a warning: the FAIL is
the record that the reconcile path is still unmeasured (#44).
"""

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
ACCOUNT = env("TINVEST_ACCOUNT_ID", secret=True)
WINDOW_DAYS = int(env("V12_WINDOW_DAYS", required=False, default="90"))

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

v = Verifier("V12", "operations feed shape and fee attribution")


def _name(raw):
    """The enum member name, however the wheel spells it."""
    if raw is None:
        return ""
    return getattr(raw, "name", None) or str(raw).rsplit(".", 1)[-1]


async def body():
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=WINDOW_DAYS)

    async with client(TOKEN) as c:
        response = await c.operations.get_operations(
            account_id=ACCOUNT, from_=since, to=now
        )
    ops = list(response.operations)

    # An empty dump is not evidence. Say so and fail rather than reporting
    # five green checks about nothing.
    if not v.check(
        "the window contains operations",
        ops,
        "{} rows over {} days".format(len(ops), WINDOW_DAYS),
    ):
        v.note("nothing to measure; widen V12_WINDOW_DAYS or trade first")
        return

    states = sorted({_name(o.state) for o in ops})
    types = sorted({_name(o.operation_type) for o in ops})
    v.note("states seen: {}".format(", ".join(states)))
    v.note("types seen: {}".format(", ".join(types)))

    # 3. The string sets the parser compares against must match reality.
    v.check(
        "executed-state name matches the parser's constant",
        EXECUTED in states or not states,
        "parser expects {}; feed has {}".format(EXECUTED, states),
    )
    fee_like = {t for t in types if "FEE" in t or "COMMISSION" in t}
    unknown_fees = sorted(fee_like - FEE_TYPES)
    v.check(
        "every fee-shaped type is in the parser's FEE_TYPES",
        not unknown_fees,
        "unrecognised: {}".format(unknown_fees) if unknown_fees else "none",
    )

    executed = [o for o in ops if _name(o.state) == EXECUTED]
    v.note("{} of {} rows are executed".format(len(executed), len(ops)))

    # 2. Fee rows must point at a trade inside the same window, or the
    #    commission sum is an empty set reported as zero.
    ids = {o.id for o in executed}
    fees = [o for o in executed if _name(o.operation_type) in FEE_TYPES]
    orphan_fees = [
        o
        for o in fees
        if o.parent_operation_id and o.parent_operation_id not in ids
    ]
    parentless = [o for o in fees if not o.parent_operation_id]
    v.note("{} fee rows; {} carry no parent id".format(len(fees), len(parentless)))
    v.check(
        "every fee's parent resolves inside the window",
        not orphan_fees,
        "{} fee(s) point outside".format(len(orphan_fees))
        if orphan_fees
        else "all resolve",
    )

    # 1 and 5. Recorded, not asserted: the weighted average is unit-invariant,
    #    so the unit cannot be inferred from the feed alone.
    # Fee rows carry a zero price object, so filter on the type: only a
    # BUY or SELL row says anything about how a trade is reported.
    trades = [
        o
        for o in executed
        if _name(o.operation_type) in {"OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL"}
    ]
    sells = [o for o in trades if _name(o.operation_type) == "OPERATION_TYPE_SELL"]
    v.check(
        "the window contains a SELL — the path reconcile books from",
        sells,
        "{} sell row(s); `_resolve_sale` is unmeasured without one".format(len(sells)),
    )
    if trades:
        sample = trades[0]
        v.note(
            "sample trade: type={} quantity={} price={} payment={}".format(
                _name(sample.operation_type),
                getattr(sample, "quantity", None),
                getattr(sample.price, "units", sample.price),
                getattr(sample.payment, "units", sample.payment),
            )
        )
        v.note(
            "quantity unit (lots vs instrument units) is NOT decidable here; "
            "`_resolve_sale` divides gross by units so it is unit-invariant"
        )
    v.check(
        "at least one BUY/SELL row to inspect",
        trades,
        "{} trade rows".format(len(trades)),
    )


run(v, body)
