"""Pure entry gate: one deterministic rejection reason, or approved lots."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.config import Config
from zarabot.models import (
    Instrument,
    PortfolioState,
    RejectionReason,
    RiskDecision,
    Side,
    Signal,
)
from zarabot.risk.sizing import size_position

_TRADING = "NORMAL_TRADING"


def _reject(reason: RejectionReason) -> RiskDecision:
    return RiskDecision(approved=False, lots=None, reason=reason)


def _approve(lots: int) -> RiskDecision:
    return RiskDecision(approved=True, lots=lots, reason=None)


def _open_cost(state: PortfolioState) -> Decimal:
    """Summed entry cost of the open portfolio.

    Read from `state.positions`, which carry lots, lot size and entry price,
    so the gate needs no extra argument and stays pure.
    """
    return sum(
        (
            Decimal(position.lots) * Decimal(position.lot_size) * position.entry_price
            for position in state.positions
        ),
        Decimal(0),
    )


def check(
    signal: Signal,
    state: PortfolioState,
    instrument: Instrument,
    cooldown_active: bool,
    session_open: bool,
    halted: bool,
    now: datetime,
    config: Config,
) -> RiskDecision:
    """Approve an entry with a lot count, or reject it with exactly one reason.

    Reasons are evaluated in a fixed priority order, never in the order the
    conditions happen to be cheap to test, so the reason recorded against a
    signal is deterministic when several apply.
    """
    del now  # provided for call-site uniformity; the gate is timeless
    if halted:
        return _reject(RejectionReason.HALTED)
    if not session_open:
        return _reject(RejectionReason.SESSION_CLOSED)
    if instrument.trading_status != _TRADING:
        return _reject(RejectionReason.INSTRUMENT_NOT_TRADING)
    if any(position.ticker == signal.ticker for position in state.positions):
        return _reject(RejectionReason.DUPLICATE_TICKER)
    if len(state.positions) >= config.max_open_positions:
        return _reject(RejectionReason.MAX_POSITIONS)
    if cooldown_active:
        return _reject(RejectionReason.COOLDOWN_ACTIVE)

    price = signal.reference_price
    lot_cost = Decimal(instrument.lot) * price
    open_cost = _open_cost(state)
    if state.cash < lot_cost:
        return _reject(RejectionReason.INSUFFICIENT_CASH)
    if config.allocated_capital - open_cost < lot_cost:
        # The runtime exposure ceiling (#16). Before v1.30 the only bounds on
        # exposure were the duplicate-ticker check and a position count, so
        # nothing stopped a portfolio — including holdings this gate never
        # sized — from committing more than the allocated capital.
        return _reject(RejectionReason.PORTFOLIO_EXPOSURE)

    # Exits never route through the gate. The check sits here rather than
    # earlier so that a SELL cannot pre-empt a higher-priority reason and make
    # the recorded reason depend on the side of the signal.
    if signal.side is not Side.BUY:
        return _reject(RejectionReason.ZERO_LOTS)

    lots = size_position(
        price,
        instrument,
        config.allocated_capital,
        state.cash,
        config.position_size_pct,
        open_cost,
        config.cash_reserve_pct,
    )
    if lots > 0:
        return _approve(lots)
    return _reject(RejectionReason.ZERO_LOTS)
