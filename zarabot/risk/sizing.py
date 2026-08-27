"""Pure position sizing: whole lots, always rounded down."""

from __future__ import annotations

from decimal import Decimal

from zarabot.models import Instrument

_HUNDRED = Decimal("100")


def size_position(
    price: Decimal,
    instrument: Instrument,
    allocated: Decimal,
    cash: Decimal,
    size_pct: Decimal,
    open_cost: Decimal,
    reserve_pct: Decimal,
) -> int:
    """Return whole lots to buy, bounded by budget, headroom and spendable cash.

    The smallest of the three bounds wins:

    * **budget** — ``size_pct%`` of ``allocated``, the intended size of one
      position;
    * **headroom** — ``allocated - open_cost``, so the summed cost of the
      portfolio never exceeds the allocated capital (#16);
    * **spendable** — ``cash`` less ``reserve_pct%``, a buying-power reserve.

    ``reserve_pct`` holds back a slice of cash so that fees, price movement
    between sizing and fill, and lot rounding cannot turn an approved order
    into one the broker refuses for insufficient funds. It is a reserve, not
    an estimate of commission: nothing here predicts a fee, and nothing
    derived from it is ever recorded as one. Commission stays whatever the
    broker reports it charged.
    """
    lot_cost = Decimal(instrument.lot) * price
    if lot_cost <= 0:
        return 0
    budget = allocated * size_pct / _HUNDRED
    headroom = allocated - open_cost
    spendable = cash * (_HUNDRED - reserve_pct) / _HUNDRED
    affordable = min(budget, headroom, spendable)
    if affordable < lot_cost:
        return 0
    # Truncating division, never a rounded quotient: at the context precision
    # `affordable / lot_cost` can round *up* to the next integer, which would
    # return one lot more than the money on hand can pay for.
    return int(affordable // lot_cost)
