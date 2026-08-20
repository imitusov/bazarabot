"""Tests for zarabot.risk.gate — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from zarabot.config import Config
from zarabot.models import (
    Instrument,
    PortfolioState,
    Position,
    RejectionReason,
    Side,
    Signal,
    StopProtection,
)
from zarabot.risk.gate import check

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
PRICE = Decimal("100")


def _config(
    max_open_positions: int = 10,
    position_size_pct: Decimal = Decimal("10"),
    max_position_pct: Decimal = Decimal("20"),
) -> Config:
    return Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=Decimal("100000"),
        position_size_pct=position_size_pct,
        max_position_pct=max_position_pct,
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("10"),
        max_holding_days=3,
        max_open_positions=max_open_positions,
        reentry_cooldown_minutes=120,
        daily_loss_limit_pct=Decimal("5"),
        watchlist=("SBER",),
        enabled_strategies=("ma_crossover",),
        ml_model_path=None,
        poll_interval_seconds=60,
        db_path=Path("zarabot.db"),
        backup_dir=Path("backups"),
        log_level="INFO",
        tz="Europe/Moscow",
    )


def _instrument(
    ticker: str = "SBER",
    lot: int = 10,
    trading_status: str = "NORMAL_TRADING",
) -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker=ticker,
        lot=lot,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status=trading_status,
        refreshed_at=NOW,
    )


def _signal(side: Side = Side.BUY, ticker: str = "SBER") -> Signal:
    return Signal(
        ticker=ticker,
        strategy="ma_crossover",
        side=side,
        generated_at=NOW,
        reference_price=PRICE,
    )


def _position(ticker: str, position_id: int = 1) -> Position:
    return Position(
        id=position_id,
        ticker=ticker,
        figi=f"BBG{position_id:012d}",
        strategy="ma_crossover",
        lots=1,
        lot_size=10,
        entry_price=PRICE,
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=False,
        open_order_key=f"key-{position_id}",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )


def _state(
    cash: Decimal = Decimal("100000"),
    tickers: tuple[str, ...] = (),
) -> PortfolioState:
    positions = tuple(
        _position(ticker, position_id=i + 1) for i, ticker in enumerate(tickers)
    )
    return PortfolioState(cash=cash, positions=positions)


def _check(
    signal: Signal | None = None,
    state: PortfolioState | None = None,
    instrument: Instrument | None = None,
    cooldown_active: bool = False,
    session_open: bool = True,
    halted: bool = False,
    config: Config | None = None,
) -> object:
    return check(
        signal if signal is not None else _signal(),
        state if state is not None else _state(),
        instrument if instrument is not None else _instrument(),
        cooldown_active,
        session_open,
        halted,
        NOW,
        config if config is not None else _config(),
    )


def test_clean_signal_is_approved() -> None:
    decision = _check()
    assert decision.approved is True
    assert decision.lots == 10
    assert decision.reason is None


def test_halted_rejects() -> None:
    decision = _check(halted=True)
    assert decision.approved is False
    assert decision.reason is RejectionReason.HALTED


def test_session_closed_rejects() -> None:
    decision = _check(session_open=False)
    assert decision.approved is False
    assert decision.reason is RejectionReason.SESSION_CLOSED


def test_instrument_not_trading_rejects() -> None:
    decision = _check(instrument=_instrument(trading_status="BREAK"))
    assert decision.approved is False
    assert decision.reason is RejectionReason.INSTRUMENT_NOT_TRADING


def test_duplicate_ticker_rejects() -> None:
    decision = _check(state=_state(tickers=("SBER",)))
    assert decision.approved is False
    assert decision.reason is RejectionReason.DUPLICATE_TICKER


def test_max_positions_rejects() -> None:
    tickers = tuple(f"T{i}" for i in range(10))
    decision = _check(state=_state(tickers=tickers), config=_config(max_open_positions=10))
    assert decision.approved is False
    assert decision.reason is RejectionReason.MAX_POSITIONS


def test_cooldown_active_rejects() -> None:
    decision = _check(cooldown_active=True)
    assert decision.approved is False
    assert decision.reason is RejectionReason.COOLDOWN_ACTIVE


def test_insufficient_cash_rejects() -> None:
    decision = _check(state=_state(cash=Decimal("500")))
    assert decision.approved is False
    assert decision.reason is RejectionReason.INSUFFICIENT_CASH


def test_zero_lots_rejects() -> None:
    # One lot costs 15000; 10% of 100000 is 10000; cash and cap still afford it.
    decision = _check(
        signal=Signal(
            ticker="SBER",
            strategy="ma_crossover",
            side=Side.BUY,
            generated_at=NOW,
            reference_price=Decimal("1500"),
        )
    )
    assert decision.approved is False
    assert decision.reason is RejectionReason.ZERO_LOTS


def test_position_cap_rejects() -> None:
    # size% budget can afford a lot; cap cannot.
    decision = _check(
        signal=Signal(
            ticker="SBER",
            strategy="ma_crossover",
            side=Side.BUY,
            generated_at=NOW,
            reference_price=Decimal("2500"),
        ),
        config=_config(
            position_size_pct=Decimal("50"),
            max_position_pct=Decimal("20"),
        ),
    )
    assert decision.approved is False
    assert decision.reason is RejectionReason.POSITION_CAP


def test_highest_priority_reason_when_several_apply() -> None:
    tickers = tuple(f"T{i}" for i in range(10))
    decision = _check(
        state=_state(cash=Decimal("1"), tickers=tickers + ("SBER",)),
        instrument=_instrument(trading_status="BREAK"),
        cooldown_active=True,
        session_open=False,
        halted=True,
    )
    assert decision.approved is False
    assert decision.reason is RejectionReason.HALTED


def test_exactly_at_max_open_positions_rejects() -> None:
    tickers = tuple(f"T{i}" for i in range(3))
    decision = _check(
        state=_state(tickers=tickers),
        config=_config(max_open_positions=3),
    )
    assert decision.reason is RejectionReason.MAX_POSITIONS


def test_one_below_max_open_positions_approves() -> None:
    tickers = tuple(f"T{i}" for i in range(2))
    decision = _check(
        state=_state(tickers=tickers),
        config=_config(max_open_positions=3),
    )
    assert decision.approved is True


def test_sell_is_never_approved() -> None:
    decision = _check(signal=_signal(side=Side.SELL))
    assert decision.approved is False


def test_gate_performs_no_io() -> None:
    # No broker, database, or clock collaborators — only in-memory values.
    decision = check(
        _signal(),
        _state(),
        _instrument(),
        False,
        True,
        False,
        NOW,
        _config(),
    )
    assert decision.approved is True
