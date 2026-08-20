"""Tests for zarabot.models — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from zarabot.models import (
    BacktestResult,
    Candle,
    ExitTrigger,
    HaltReason,
    HaltState,
    Instrument,
    OperationRecord,
    OrderRecord,
    OrderStatus,
    PortfolioState,
    Position,
    ReconciliationReport,
    RejectionReason,
    RiskDecision,
    SessionInfo,
    Side,
    Signal,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
    TradingCalendar,
)

AWARE = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
NAIVE = datetime(2026, 3, 15, 10, 0)  # noqa: DTZ001  # the naive input under test
PRICE = Decimal("100.50")


def _instrument(**overrides: object) -> Instrument:
    fields: dict[str, object] = {
        "figi": "BBG000000001",
        "ticker": "SBER",
        "lot": 10,
        "min_price_increment": Decimal("0.01"),
        "currency": "RUB",
        "trading_status": "NORMAL_TRADING",
        "refreshed_at": AWARE,
    }
    fields.update(overrides)
    return Instrument(**fields)  # type: ignore[arg-type]


def _signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "ticker": "SBER",
        "strategy": "ma_crossover",
        "side": Side.BUY,
        "generated_at": AWARE,
        "reference_price": PRICE,
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


def _position(**overrides: object) -> Position:
    fields: dict[str, object] = {
        "id": 1,
        "ticker": "SBER",
        "figi": "BBG000000001",
        "strategy": "ma_crossover",
        "lots": 2,
        "lot_size": 10,
        "entry_price": PRICE,
        "entry_at": AWARE,
        "stop_price": Decimal("95.48"),
        "target_price": Decimal("110.55"),
        "status": "OPEN",
        "adopted": False,
        "open_order_key": "11111111-1111-4111-8111-111111111111",
        "close_order_key": None,
        "exit_trigger": None,
        "exit_price": None,
        "exit_at": None,
        "realised_pnl": None,
        "stop_protection": StopProtection.LOCAL,
        "stop_order_key": None,
    }
    fields.update(overrides)
    return Position(**fields)  # type: ignore[arg-type]


def _order(**overrides: object) -> OrderRecord:
    fields: dict[str, object] = {
        "key": "11111111-1111-4111-8111-111111111111",
        "ticker": "SBER",
        "figi": "BBG000000001",
        "side": Side.BUY,
        "intent": "ENTRY",
        "lots": 2,
        "status": OrderStatus.SUBMITTING,
        "filled_lots": None,
        "filled_price": None,
        "commission": None,
        "broker_reason": None,
        "created_at": AWARE,
        "settled_at": None,
    }
    fields.update(overrides)
    return OrderRecord(**fields)  # type: ignore[arg-type]


def _stop_order(**overrides: object) -> StopOrderRecord:
    fields: dict[str, object] = {
        "key": "22222222-2222-4222-8222-222222222222",
        "stop_order_id": None,
        "position_id": 1,
        "ticker": "SBER",
        "lots": 2,
        "stop_price": Decimal("95.48"),
        "status": StopOrderStatus.PLACING,
        "created_at": AWARE,
        "settled_at": None,
    }
    fields.update(overrides)
    return StopOrderRecord(**fields)  # type: ignore[arg-type]


def _operation(**overrides: object) -> OperationRecord:
    fields: dict[str, object] = {
        "id": "op-1",
        "figi": "BBG000000001",
        "ticker": "SBER",
        "occurred_at": AWARE,
        "commission": Decimal("1.25"),
        "payment": Decimal("-2010.00"),
        "price": PRICE,
        "quantity": 20,
    }
    fields.update(overrides)
    return OperationRecord(**fields)  # type: ignore[arg-type]


def _session(**overrides: object) -> SessionInfo:
    fields: dict[str, object] = {
        "start": AWARE,
        "end": datetime(2026, 3, 15, 16, 0, tzinfo=UTC),
        "is_trading_day": True,
    }
    fields.update(overrides)
    return SessionInfo(**fields)  # type: ignore[arg-type]


def _halt(**overrides: object) -> HaltState:
    fields: dict[str, object] = {
        "halted": True,
        "reason": HaltReason.MANUAL,
        "detail": "owner requested",
        "halted_at": AWARE,
        "resumed_at": None,
        "resumed_by": None,
    }
    fields.update(overrides)
    return HaltState(**fields)  # type: ignore[arg-type]


def _report(**overrides: object) -> ReconciliationReport:
    fields: dict[str, object] = {
        "ran_at": AWARE,
        "adjustments": (),
    }
    fields.update(overrides)
    return ReconciliationReport(**fields)  # type: ignore[arg-type]


def _candle(**overrides: object) -> Candle:
    fields: dict[str, object] = {
        "timestamp": AWARE,
        "open": PRICE,
        "high": Decimal("101.00"),
        "low": Decimal("99.50"),
        "close": Decimal("100.75"),
        "volume": 1000,
    }
    fields.update(overrides)
    return Candle(**fields)  # type: ignore[arg-type]


def _portfolio(**overrides: object) -> PortfolioState:
    fields: dict[str, object] = {
        "cash": Decimal("100000.00"),
        "positions": (),
    }
    fields.update(overrides)
    return PortfolioState(**fields)  # type: ignore[arg-type]


def _calendar(**overrides: object) -> TradingCalendar:
    fields: dict[str, object] = {"sessions": (_session(),)}
    fields.update(overrides)
    return TradingCalendar(**fields)  # type: ignore[arg-type]


def _backtest(**overrides: object) -> BacktestResult:
    fields: dict[str, object] = {
        "trades": (),
        "pnl": Decimal("12.34"),
        "win_rate": Decimal("0.5"),
        "max_drawdown": Decimal("0.1"),
        "exit_trigger_distribution": (),
        "benchmark_return": Decimal("0.02"),
    }
    fields.update(overrides)
    return BacktestResult(**fields)  # type: ignore[arg-type]


def _all_valid_instances() -> list[Any]:
    return [
        _candle(),
        _instrument(),
        _signal(),
        _position(),
        _order(),
        _stop_order(),
        _operation(),
        _portfolio(),
        _session(),
        RiskDecision(approved=True, lots=1, reason=None),
        _halt(),
        _report(),
        _calendar(),
        _backtest(),
    ]


# --- naive datetime ----------------------------------------------------------


@pytest.mark.parametrize(
    "factory,field",
    [
        (_candle, "timestamp"),
        (_instrument, "refreshed_at"),
        (_signal, "generated_at"),
        (_position, "entry_at"),
        (_order, "created_at"),
        (_stop_order, "created_at"),
        (_operation, "occurred_at"),
        (_session, "start"),
        (_halt, "halted_at"),
        (_report, "ran_at"),
    ],
)
def test_naive_datetime_raises_value_error(
    factory: Callable[..., object], field: str
) -> None:
    with pytest.raises(ValueError):
        factory(**{field: NAIVE})


def test_position_naive_exit_at_raises() -> None:
    with pytest.raises(ValueError):
        _position(
            status="CLOSED",
            close_order_key="33333333-3333-4333-8333-333333333333",
            exit_trigger=ExitTrigger.TAKE_PROFIT,
            exit_price=Decimal("110.55"),
            exit_at=NAIVE,
            realised_pnl=Decimal("10.00"),
        )


def test_order_naive_settled_at_raises() -> None:
    with pytest.raises(ValueError):
        _order(
            status=OrderStatus.FILLED,
            filled_lots=2,
            filled_price=PRICE,
            commission=Decimal("1.00"),
            settled_at=NAIVE,
        )


def test_session_naive_end_raises() -> None:
    with pytest.raises(ValueError):
        _session(end=NAIVE)


# --- negative lots / prices / non-positive lot size --------------------------


def test_negative_lot_count_raises() -> None:
    with pytest.raises(ValueError):
        _position(lots=-1)
    with pytest.raises(ValueError):
        _order(lots=-1)
    with pytest.raises(ValueError):
        _stop_order(lots=-1)


def test_negative_price_raises() -> None:
    with pytest.raises(ValueError):
        _candle(open=Decimal("-1"))
    with pytest.raises(ValueError):
        _position(entry_price=Decimal("-0.01"))
    with pytest.raises(ValueError):
        _signal(reference_price=Decimal("-10"))
    with pytest.raises(ValueError):
        _instrument(min_price_increment=Decimal("-0.01"))


def test_non_positive_lot_size_raises() -> None:
    with pytest.raises(ValueError):
        _instrument(lot=0)
    with pytest.raises(ValueError):
        _instrument(lot=-10)
    with pytest.raises(ValueError):
        _position(lot_size=0)
    with pytest.raises(ValueError):
        _position(lot_size=-1)


# --- float money -------------------------------------------------------------


def test_monetary_field_given_float_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _candle(close=100.5)
    with pytest.raises(TypeError):
        _signal(reference_price=10.0)
    with pytest.raises(TypeError):
        _position(entry_price=100.0)
    with pytest.raises(TypeError):
        _operation(commission=1.25)
    with pytest.raises(TypeError):
        _portfolio(cash=100000.0)
    with pytest.raises(TypeError):
        _backtest(pnl=12.34)


# --- frozen ------------------------------------------------------------------


def test_every_dataclass_is_frozen() -> None:
    for instance in _all_valid_instances():
        field_name = dataclass_fields(instance)[0].name
        with pytest.raises(AttributeError):
            setattr(instance, field_name, getattr(instance, field_name))


# --- RiskDecision ------------------------------------------------------------


def test_risk_decision_cannot_be_both_approved_and_rejected() -> None:
    with pytest.raises(ValueError):
        RiskDecision(approved=True, lots=1, reason=RejectionReason.HALTED)


def test_risk_decision_cannot_be_neither() -> None:
    with pytest.raises(ValueError):
        RiskDecision(approved=False, lots=None, reason=None)
    with pytest.raises(ValueError):
        RiskDecision(approved=True, lots=None, reason=None)


def test_approved_risk_decision_with_zero_lots_raises() -> None:
    with pytest.raises(ValueError):
        RiskDecision(approved=True, lots=0, reason=None)


def test_approved_and_rejected_risk_decisions_construct() -> None:
    approved = RiskDecision(approved=True, lots=3, reason=None)
    assert approved.approved is True
    assert approved.lots == 3
    assert approved.reason is None
    rejected = RiskDecision(
        approved=False, lots=None, reason=RejectionReason.INSUFFICIENT_CASH
    )
    assert rejected.approved is False
    assert rejected.lots is None
    assert rejected.reason is RejectionReason.INSUFFICIENT_CASH


# --- enum round-trip ---------------------------------------------------------


def _public_enums() -> list[type[Enum]]:
    return [
        Side,
        OrderStatus,
        ExitTrigger,
        RejectionReason,
        HaltReason,
        StopOrderStatus,
        StopProtection,
    ]


def test_every_enum_member_round_trips_through_its_string_value() -> None:
    for enum_cls in _public_enums():
        for member in enum_cls:
            assert isinstance(member.value, str)
            restored = enum_cls(member.value)
            assert restored is member
            assert restored.value == member.value


# --- Position stop-protection pairing ----------------------------------------


def test_position_exchange_without_stop_order_key_raises() -> None:
    with pytest.raises(ValueError):
        _position(stop_protection=StopProtection.EXCHANGE, stop_order_key=None)


def test_position_local_with_stop_order_key_raises() -> None:
    with pytest.raises(ValueError):
        _position(
            stop_protection=StopProtection.LOCAL,
            stop_order_key="22222222-2222-4222-8222-222222222222",
        )


def test_position_exchange_with_key_and_local_without_construct() -> None:
    local = _position(stop_protection=StopProtection.LOCAL, stop_order_key=None)
    assert local.stop_protection is StopProtection.LOCAL
    exchange = _position(
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key="22222222-2222-4222-8222-222222222222",
    )
    assert exchange.stop_order_key is not None
