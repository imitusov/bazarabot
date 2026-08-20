"""Pure position sizing: whole lots, always rounded down."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from zarabot.models import Instrument

_HUNDRED = Decimal("100")


def size_position(
    price: Decimal,
    instrument: Instrument,
    allocated: Decimal,
    cash: Decimal,
    size_pct: Decimal,
    cap_pct: Decimal,
) -> int:
    """Return whole lots to buy, never exceeding cap% of allocated or cash."""
    lot_cost = Decimal(instrument.lot) * price
    if lot_cost <= 0:
        return 0
    budget = allocated * size_pct / _HUNDRED
    cap = allocated * cap_pct / _HUNDRED
    affordable = min(budget, cap, cash)
    if affordable < lot_cost:
        return 0
    return int((affordable / lot_cost).to_integral_value(rounding=ROUND_DOWN))
