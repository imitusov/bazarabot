"""Tests for zarabot.risk.sizing — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from zarabot.models import Instrument
from zarabot.risk.sizing import size_position

AWARE = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
ALLOCATED = Decimal("100000")
SIZE_PCT = Decimal("10")
CAP_PCT = Decimal("20")


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
        Decimal("100"), _instrument(), ALLOCATED, Decimal("100000"), SIZE_PCT, CAP_PCT
    )
    assert lots == 10
    assert lots * 10 * Decimal("100") <= ALLOCATED * SIZE_PCT / Decimal("100")


def test_one_lot_exceeding_cap_returns_zero() -> None:
    # lot cost = 10 * 3000 = 30000 > 20% of 100000 = 20000
    lots = size_position(
        Decimal("3000"), _instrument(), ALLOCATED, Decimal("100000"), SIZE_PCT, CAP_PCT
    )
    assert lots == 0


def test_cash_below_one_lot_returns_zero() -> None:
    lots = size_position(
        Decimal("100"), _instrument(), ALLOCATED, Decimal("500"), SIZE_PCT, CAP_PCT
    )
    assert lots == 0


def test_rounding_is_always_downward() -> None:
    # lot cost = 1000; 10% of 100000 = 10000 would be 10 lots, but use cash/budget
    # worth 2.9 lots: cap and size large, cash = 2900 → 2 lots
    lots = size_position(
        Decimal("100"),
        _instrument(),
        ALLOCATED,
        Decimal("2900"),
        SIZE_PCT,
        CAP_PCT,
    )
    assert lots == 2


def test_returned_lots_never_exceed_cap_of_allocated() -> None:
    cases = [
        (Decimal("50"), 10, Decimal("100000"), Decimal("10"), Decimal("20")),
        (Decimal("1"), 1, Decimal("10000"), Decimal("10"), Decimal("20")),
        (Decimal("999.99"), 10, Decimal("50000"), Decimal("15"), Decimal("20")),
        (Decimal("250"), 100, Decimal("100000"), Decimal("10"), Decimal("20")),
        (Decimal("0.01"), 1, Decimal("1"), Decimal("10"), Decimal("20")),
    ]
    for price, lot, allocated, size_pct, cap_pct in cases:
        instrument = _instrument(lot)
        cash = allocated
        lots = size_position(price, instrument, allocated, cash, size_pct, cap_pct)
        assert lots >= 0
        cost = Decimal(lots) * Decimal(lot) * price
        cap = allocated * cap_pct / Decimal("100")
        assert cost <= cap
        assert cost <= cash
