"""Domain types shared across every module. Validation only, never I/O."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ExitTrigger(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MAX_AGE = "MAX_AGE"
    EXTERNAL = "EXTERNAL"


class RejectionReason(StrEnum):
    HALTED = "HALTED"
    SESSION_CLOSED = "SESSION_CLOSED"
    INSTRUMENT_NOT_TRADING = "INSTRUMENT_NOT_TRADING"
    DUPLICATE_TICKER = "DUPLICATE_TICKER"
    MAX_POSITIONS = "MAX_POSITIONS"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    ZERO_LOTS = "ZERO_LOTS"
    PORTFOLIO_EXPOSURE = "PORTFOLIO_EXPOSURE"
    BROKER_LOT_LIMIT = "BROKER_LOT_LIMIT"


class HaltReason(StrEnum):
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MANUAL = "MANUAL"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


class StopOrderStatus(StrEnum):
    PLACING = "PLACING"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXECUTED = "EXECUTED"
    ORPHANED = "ORPHANED"
    FAILED = "FAILED"


class StopProtection(StrEnum):
    EXCHANGE = "EXCHANGE"
    LOCAL = "LOCAL"


_PRICE_FIELDS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "min_price_increment",
        "reference_price",
        "entry_price",
        "stop_price",
        "target_price",
        "exit_price",
        "filled_price",
        "price",
        "commission",
        "cash",
    }
)
_SIGNED_MONEY_FIELDS = frozenset(
    {
        "realised_pnl",
        "payment",
        "pnl",
        "win_rate",
        "max_drawdown",
        "benchmark_return",
    }
)
_MONEY_FIELDS = _PRICE_FIELDS | _SIGNED_MONEY_FIELDS
_TIMESTAMP_FIELDS = frozenset(
    {
        "timestamp",
        "refreshed_at",
        "generated_at",
        "entry_at",
        "exit_at",
        "created_at",
        "settled_at",
        "occurred_at",
        "start",
        "end",
        "halted_at",
        "resumed_at",
        "ran_at",
    }
)
_LOT_COUNT_FIELDS = frozenset({"lots", "filled_lots", "quantity"})
_LOT_SIZE_FIELDS = frozenset({"lot", "lot_size"})


def _reject_naive(value: datetime) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")


def _reject_float_money(name: str, value: object) -> None:
    if isinstance(value, float):
        raise TypeError(f"{name} must be Decimal, not float")


def _validate_money(name: str, value: object) -> None:
    _reject_float_money(name, value)
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, not {type(value).__name__}")
    if name in _PRICE_FIELDS and value < 0:
        raise ValueError(f"{name} must not be negative")


def _validate_common(instance: Any) -> None:
    for field in fields(instance):
        value = getattr(instance, field.name)
        if value is None:
            continue
        if field.name in _TIMESTAMP_FIELDS and isinstance(value, datetime):
            _reject_naive(value)
        elif field.name in _MONEY_FIELDS:
            _validate_money(field.name, value)
        elif field.name in _LOT_COUNT_FIELDS and isinstance(value, int) and value < 0:
            raise ValueError(f"{field.name} must not be negative")
        elif field.name in _LOT_SIZE_FIELDS and isinstance(value, int) and value <= 0:
            raise ValueError(f"{field.name} must be positive")


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        _validate_common(self)
        if self.volume < 0:
            raise ValueError("volume must not be negative")


@dataclass(frozen=True)
class Instrument:
    figi: str
    ticker: str
    lot: int
    min_price_increment: Decimal
    currency: str
    trading_status: str
    refreshed_at: datetime

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class Signal:
    ticker: str
    strategy: str
    side: Side
    generated_at: datetime
    reference_price: Decimal

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class Position:
    id: int
    ticker: str
    figi: str
    strategy: str
    lots: int
    lot_size: int
    entry_price: Decimal
    entry_at: datetime
    stop_price: Decimal
    target_price: Decimal
    status: str
    adopted: bool
    open_order_key: str
    close_order_key: str | None
    exit_trigger: ExitTrigger | None
    exit_price: Decimal | None
    exit_at: datetime | None
    realised_pnl: Decimal | None
    stop_protection: StopProtection
    stop_order_key: str | None

    def __post_init__(self) -> None:
        _validate_common(self)
        if self.lots <= 0:
            raise ValueError("lots must be positive")
        if self.stop_protection is StopProtection.EXCHANGE and not self.stop_order_key:
            raise ValueError("EXCHANGE stop protection requires a stop order key")
        if self.stop_protection is StopProtection.LOCAL and self.stop_order_key:
            raise ValueError("LOCAL stop protection forbids a stop order key")


@dataclass(frozen=True)
class OrderRecord:
    key: str
    ticker: str
    figi: str
    side: Side
    intent: str
    lots: int
    status: OrderStatus
    filled_lots: int | None
    filled_price: Decimal | None
    commission: Decimal | None
    broker_reason: str | None
    created_at: datetime
    settled_at: datetime | None
    exit_trigger: ExitTrigger | None = None

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class StopOrderRecord:
    key: str
    stop_order_id: str | None
    position_id: int
    ticker: str
    lots: int
    stop_price: Decimal
    status: StopOrderStatus
    created_at: datetime
    settled_at: datetime | None

    def __post_init__(self) -> None:
        _validate_common(self)
        if self.lots <= 0:
            raise ValueError("lots must be positive")


@dataclass(frozen=True)
class OperationRecord:
    id: str
    figi: str
    ticker: str
    occurred_at: datetime
    commission: Decimal
    payment: Decimal
    price: Decimal | None
    quantity: int | None
    # The broker's own names. Without them a sale was identifiable only by the
    # sign of `payment` and a fee only by a substring of a name this record did
    # not carry — neither is a thing to build a money figure on (#11).
    operation_type: str = ""
    state: str = ""
    parent_operation_id: str | None = None

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class PortfolioState:
    cash: Decimal
    positions: tuple[Position, ...]

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class SessionInfo:
    start: datetime | None
    end: datetime | None
    is_trading_day: bool

    def __post_init__(self) -> None:
        _validate_common(self)

    def in_closing_window(self, now: datetime, minutes: int = 15) -> bool:
        """True during the final `minutes` of this session. Pure; no I/O."""
        _reject_naive(now)
        if not self.is_trading_day or self.end is None:
            return False
        return self.end - timedelta(minutes=minutes) <= now < self.end


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    lots: int | None
    reason: RejectionReason | None

    def __post_init__(self) -> None:
        if self.approved:
            if self.reason is not None:
                raise ValueError("approved decision cannot carry a rejection reason")
            if self.lots is None:
                raise ValueError("approved decision requires a lot count")
            if self.lots <= 0:
                raise ValueError("approved decision requires a positive lot count")
        else:
            if self.reason is None:
                raise ValueError("rejected decision requires a rejection reason")
            if self.lots is not None:
                raise ValueError("rejected decision cannot carry a lot count")


@dataclass(frozen=True)
class HaltState:
    halted: bool
    reason: HaltReason | None
    detail: str | None
    halted_at: datetime | None
    resumed_at: datetime | None
    resumed_by: str | None

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class ReconciliationReport:
    ran_at: datetime
    adjustments: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[SessionInfo, ...]

    def __post_init__(self) -> None:
        _validate_common(self)


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Position, ...]
    pnl: Decimal
    win_rate: Decimal
    max_drawdown: Decimal
    exit_trigger_distribution: tuple[tuple[ExitTrigger, int], ...]
    benchmark_return: Decimal | None

    def __post_init__(self) -> None:
        _validate_common(self)
