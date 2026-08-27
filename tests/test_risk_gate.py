"""Tests for zarabot.risk.gate — written from technical-spec.md §3.2."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from zarabot.config import Config
from zarabot.models import (
    Instrument,
    PortfolioState,
    Position,
    RejectionReason,
    RiskDecision,
    Side,
    Signal,
    StopProtection,
)
from zarabot.risk.gate import check

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
PRICE = Decimal("100")
# One lot of the default instrument at the default price.
LOT_COST = Decimal("1000")
ALLOCATED = Decimal("100000")


def _config(
    max_open_positions: int = 10,
    position_size_pct: Decimal = Decimal("10"),
    cash_reserve_pct: Decimal = Decimal("1"),
) -> Config:
    # Every field here is one `config.load()` would emit: percentages in range,
    # `max_open_positions × position_size_pct <= 100`, take-profit above stop.
    # A test that builds a Config the loader refuses proves nothing about the
    # assembled system — that is how the withdrawn POSITION_CAP case (#15)
    # stayed green while asserting behaviour no deployment could produce.
    return Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=ALLOCATED,
        position_size_pct=position_size_pct,
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
        cash_reserve_pct=cash_reserve_pct,
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


def _signal(
    side: Side = Side.BUY,
    ticker: str = "SBER",
    price: Decimal = PRICE,
) -> Signal:
    return Signal(
        ticker=ticker,
        strategy="ma_crossover",
        side=side,
        generated_at=NOW,
        reference_price=price,
    )


def _position(
    ticker: str,
    position_id: int = 1,
    lots: int = 1,
    entry_price: Decimal = PRICE,
    adopted: bool = False,
) -> Position:
    return Position(
        id=position_id,
        ticker=ticker,
        figi=f"BBG{position_id:012d}",
        strategy="ma_crossover",
        lots=lots,
        lot_size=10,
        entry_price=entry_price,
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=adopted,
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
    positions: tuple[Position, ...] | None = None,
) -> PortfolioState:
    if positions is None:
        positions = tuple(
            _position(ticker, position_id=i + 1) for i, ticker in enumerate(tickers)
        )
    return PortfolioState(cash=cash, positions=positions)


def _adopted_portfolio(open_cost: Decimal) -> tuple[Position, ...]:
    """One adopted holding whose entry cost is `open_cost`.

    A position the bot did not size itself — `broker.reconcile` adopts holdings
    opened by hand, and lowering `ALLOCATED_CAPITAL` between runs has the same
    effect on the ones already open. Either way the portfolio can consume more
    of the allocated capital than any single sized entry would, which is the
    runtime exposure the configuration-time bound never covered (#16).
    """
    entry_price = Decimal("50")
    unit = entry_price * 10  # one lot of ten units
    lots = int(open_cost / unit)
    assert Decimal(lots) * unit == open_cost, "open_cost must be a whole lot count"
    return (
        _position(
            "GAZP",
            position_id=99,
            lots=lots,
            entry_price=entry_price,
            adopted=True,
        ),
    )


def _check(
    signal: Signal | None = None,
    state: PortfolioState | None = None,
    instrument: Instrument | None = None,
    cooldown_active: bool = False,
    session_open: bool = True,
    halted: bool = False,
    config: Config | None = None,
) -> RiskDecision:
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
    decision = _check(
        state=_state(tickers=tickers),
        config=_config(max_open_positions=10),
    )
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
    # One lot costs 15000; 10% of 100000 is 10000. Cash and headroom both
    # afford it, so only the per-position budget bites.
    decision = _check(signal=_signal(price=Decimal("1500")))
    assert decision.approved is False
    assert decision.reason is RejectionReason.ZERO_LOTS


def test_portfolio_exposure_rejects_when_headroom_is_below_one_lot() -> None:
    # 99500 of the 100000 allocated is already committed, so 500 of headroom
    # is left and one lot costs 1000. Cash (100000) and the per-position
    # budget (10000) would both happily buy ten lots: only the runtime
    # portfolio ceiling stops this order. Replaces the withdrawn POSITION_CAP
    # case, which could not bind at all (#15/#16).
    decision = _check(state=_state(positions=_adopted_portfolio(Decimal("99500"))))
    assert decision.approved is False
    assert decision.reason is RejectionReason.PORTFOLIO_EXPOSURE


def test_headroom_of_exactly_one_lot_is_approved() -> None:
    # The inclusive side of the boundary: `allocated - open_cost == lot_cost`
    # is not "less headroom than one lot", so the entry passes for one lot.
    decision = _check(state=_state(positions=_adopted_portfolio(Decimal("99000"))))
    assert decision.approved is True
    assert decision.lots == 1


def test_portfolio_exposure_is_evaluated_after_insufficient_cash() -> None:
    # Both apply: cash below one lot, and headroom below one lot. The fixed
    # priority puts cash first, so the recorded reason is deterministic.
    decision = _check(
        state=_state(
            cash=Decimal("500"),
            positions=_adopted_portfolio(Decimal("99500")),
        )
    )
    assert decision.reason is RejectionReason.INSUFFICIENT_CASH


def test_portfolio_exposure_is_evaluated_before_zero_lots() -> None:
    # Both apply: one lot at 1500 costs 15000, above the 10000 budget, and
    # headroom is 500. Cash covers the lot, so exposure is the reason.
    decision = _check(
        signal=_signal(price=Decimal("1500")),
        state=_state(positions=_adopted_portfolio(Decimal("99500"))),
    )
    assert decision.reason is RejectionReason.PORTFOLIO_EXPOSURE


def test_open_cost_sums_every_position_not_just_the_largest() -> None:
    # Eight ordinary sized holdings, none of them individually close to the
    # ceiling: 10000 apiece. Only their sum leaves too little headroom, so a
    # gate that looked at the largest position, or at the count alone, would
    # approve this order.
    positions = tuple(_position(f"T{i}", position_id=i + 1, lots=10) for i in range(8))
    decision = _check(
        signal=_signal(price=Decimal("2500")),
        state=_state(positions=positions),
        config=_config(max_open_positions=10, position_size_pct=Decimal("10")),
    )
    # open_cost = 8 × 10000 = 80000, headroom 20000, one lot costs 25000.
    assert decision.reason is RejectionReason.PORTFOLIO_EXPOSURE


def test_cash_reserve_shortfall_rejects_with_zero_lots() -> None:
    # Cash is exactly one lot, so the cash check passes, but the reserve holds
    # back 1% and `risk.sizing` returns no lots. Nothing higher applies.
    decision = _check(state=_state(cash=LOT_COST))
    assert decision.approved is False
    assert decision.reason is RejectionReason.ZERO_LOTS


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
    assert decision.lots is None


def test_sell_is_rejected_even_with_room_for_the_position() -> None:
    # Nothing else is wrong with this signal: cash, headroom and budget all
    # allow ten lots. Exits never route through the gate.
    decision = _check(
        signal=_signal(side=Side.SELL),
        state=_state(cash=Decimal("100000")),
    )
    assert decision.approved is False


def test_zero_price_rejects_with_zero_lots() -> None:
    decision = _check(signal=_signal(price=Decimal("0")))
    assert decision.approved is False
    assert decision.reason is RejectionReason.ZERO_LOTS


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


def test_gate_never_mutates_state() -> None:
    state = _state(positions=_adopted_portfolio(Decimal("50000")))
    before = replace(state)
    _check(state=state)
    assert state == before
    assert state.positions == before.positions
    assert state.cash == before.cash
