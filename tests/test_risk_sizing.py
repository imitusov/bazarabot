"""Tests for zarabot.risk.sizing — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import product

from zarabot.models import Instrument
from zarabot.risk.sizing import size_position

AWARE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
ALLOCATED = Decimal("100000")
SIZE_PCT = Decimal("10")
NO_OPEN_COST = Decimal("0")
NO_RESERVE = Decimal("0")
HUNDRED = Decimal("100")


def _instrument(lot: int = 10) -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=lot,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=AWARE,
    )


def test_standard_case_returns_whole_lots_at_or_below_percentage() -> None:
    # lot cost = 10 * 100 = 1000; 10% of 100000 = 10000 → 10 lots
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 10
    assert lots * 10 * Decimal("100") <= ALLOCATED * SIZE_PCT / HUNDRED


def test_open_cost_leaving_less_than_one_lot_of_headroom_returns_zero() -> None:
    # The portfolio ceiling (#16) that replaced the per-position cap (#15):
    # 99500 of the 100000 allocated is already spent, one lot costs 1000, and
    # the budget alone would happily buy ten.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        Decimal("99500"),
        NO_RESERVE,
    )
    assert lots == 0


def test_open_cost_leaving_exactly_one_lot_of_headroom_returns_one() -> None:
    # The inclusive side of the same boundary: headroom == lot cost buys one.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        Decimal("99000"),
        NO_RESERVE,
    )
    assert lots == 1


def test_headroom_binds_before_the_budget() -> None:
    # Budget would buy ten lots; only three fit under the portfolio ceiling.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        Decimal("96500"),
        NO_RESERVE,
    )
    assert lots == 3


def test_open_cost_above_allocated_returns_zero_never_negative() -> None:
    # Negative headroom must not become a negative — or a short — lot count.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        Decimal("150000"),
        NO_RESERVE,
    )
    assert lots == 0


def test_reserve_is_honoured_when_cash_equals_exactly_one_lot() -> None:
    # Cash is exactly one lot, so without a reserve this buys one. The reserve
    # holds back 1% of buying power, so the order the broker would refuse for
    # insufficient funds is never approved. It predicts no fee and records no
    # commission — it only declines to spend the last rouble.
    cash = Decimal("1000")
    assert (
        size_position(
            Decimal("100"),
            _instrument(),
            ALLOCATED,
            cash,
            SIZE_PCT,
            NO_OPEN_COST,
            NO_RESERVE,
        )
        == 1
    )
    assert (
        size_position(
            Decimal("100"),
            _instrument(),
            ALLOCATED,
            cash,
            SIZE_PCT,
            NO_OPEN_COST,
            Decimal("1"),
        )
        == 0
    )


def test_reserve_binds_before_cash() -> None:
    # 10000 cash buys ten lots outright; a 25% reserve leaves 7500, so seven.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("10000"),
        SIZE_PCT,
        NO_OPEN_COST,
        Decimal("25"),
    )
    assert lots == 7


def test_one_lot_exceeding_the_position_budget_returns_zero() -> None:
    # lot cost = 10 * 3000 = 30000 > 10% of 100000 = 10000. The expensive
    # instrument that would otherwise over-allocate a single position.
    lots = size_position(
        Decimal("3000"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 0


def test_cash_below_one_lot_returns_zero() -> None:
    # Cash is respected independently of the percentage: the budget affords ten.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("500"),
        SIZE_PCT,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 0


def test_rounding_is_always_downward() -> None:
    # lot cost = 1000; cash worth 2.9 lots → 2, never 3.
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("2900"),
        SIZE_PCT,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 2


def test_rounding_down_survives_the_decimal_precision_limit() -> None:
    # 8.999...9 (28 significant digits) divided by 3 rounds *up* to exactly 3
    # at the default context precision, so a quotient rounded after the division
    # returns three lots costing 9 against 8.999...9 of headroom — an order
    # above the portfolio ceiling. Truncating division cannot make that mistake.
    allocated = Decimal("8.999999999999999999999999999")
    lots = size_position(
        Decimal("3"),
        _instrument(lot=1),
        allocated,
        Decimal("1000000"),
        HUNDRED,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 2
    assert Decimal(lots) * Decimal("3") <= allocated


def test_zero_price_returns_zero_lots() -> None:
    lots = size_position(
        Decimal("0"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        NO_OPEN_COST,
        NO_RESERVE,
    )
    assert lots == 0


def test_full_reserve_leaves_nothing_spendable() -> None:
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("100000"),
        SIZE_PCT,
        NO_OPEN_COST,
        HUNDRED,
    )
    assert lots == 0


# The property. Not an example: the invariant must hold for every combination
# below, and each of the three bounds is the binding one somewhere in the grid.
# `hypothesis` is not a dependency of this project, so the input space is swept
# exhaustively and deterministically instead of sampled.
_PRICES = (Decimal("0.01"), Decimal("0.07"), Decimal("100"), Decimal("3333.33"))
_LOTS = (1, 10, 100)
_ALLOCATED = (Decimal("1"), Decimal("10000"), Decimal("100000"))
_CASH = (Decimal("0"), Decimal("999.99"), Decimal("100000"))
_SIZE_PCT = (Decimal("1"), Decimal("10"), Decimal("100"))
_OPEN_COST = (Decimal("0"), Decimal("9999.99"), Decimal("100000"))
_RESERVE_PCT = (Decimal("0"), Decimal("1"), Decimal("50"))


def test_cost_never_exceeds_headroom_or_cash_for_any_input() -> None:
    seen_zero = False
    seen_positive = False
    for price, lot, allocated, cash, size_pct, open_cost, reserve_pct in product(
        _PRICES, _LOTS, _ALLOCATED, _CASH, _SIZE_PCT, _OPEN_COST, _RESERVE_PCT
    ):
        lots = size_position(
            price,
            _instrument(lot),
            allocated,
            cash,
            size_pct,
            open_cost,
            reserve_pct,
        )
        assert lots >= 0
        cost = Decimal(lots) * Decimal(lot) * price
        headroom = allocated - open_cost
        # An overspent portfolio has negative headroom, where the contract's
        # inequality is satisfiable only by buying nothing — which is the
        # behaviour, since the alternative reading would be a short sale.
        assert cost <= max(headroom, Decimal(0)), (
            f"{lots} lots cost {cost}, above headroom "
            f"{headroom} (price {price}, lot {lot})"
        )
        assert cost <= cash, f"{lots} lots cost {cost}, above cash {cash}"
        seen_zero = seen_zero or lots == 0
        seen_positive = seen_positive or lots > 0
    # A grid that never buys anything would satisfy the invariant vacuously.
    assert seen_zero and seen_positive


def test_cost_never_exceeds_the_structural_ceiling_for_any_input() -> None:
    # §3.2 wrote this ceiling as MAX_POSITION_PCT of allocated capital. v1.30
    # withdrew that setting (#15) — it could not bind, because config.load()
    # refused any configuration where POSITION_SIZE_PCT exceeded it. The two
    # ceilings that replaced it are the per-position budget and the portfolio
    # headroom, and both are structural: no input evades either.
    for price, lot, allocated, cash, size_pct, open_cost, reserve_pct in product(
        _PRICES, _LOTS, _ALLOCATED, _CASH, _SIZE_PCT, _OPEN_COST, _RESERVE_PCT
    ):
        lots = size_position(
            price,
            _instrument(lot),
            allocated,
            cash,
            size_pct,
            open_cost,
            reserve_pct,
        )
        cost = Decimal(lots) * Decimal(lot) * price
        assert cost <= allocated * size_pct / HUNDRED
        assert cost <= cash * (HUNDRED - reserve_pct) / HUNDRED
