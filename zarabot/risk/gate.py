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

_HUNDRED = Decimal("100")
_TRADING = "NORMAL_TRADING"


def _reject(reason: RejectionReason) -> RiskDecision:
    return RiskDecision(approved=False, lots=None, reason=reason)


def _approve(lots: int) -> RiskDecision:
    return RiskDecision(approved=True, lots=lots, reason=None)


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
    if signal.side is Side.SELL:
        return _reject(RejectionReason.ZERO_LOTS)

    price = signal.reference_price
    lot_cost = Decimal(instrument.lot) * price
    lots = size_position(
        price,
        instrument,
        config.allocated_capital,
        state.cash,
        config.position_size_pct,
        config.max_position_pct,
    )
    if lots > 0:
        return _approve(lots)

    if state.cash < lot_cost:
        return _reject(RejectionReason.INSUFFICIENT_CASH)
    budget = config.allocated_capital * config.position_size_pct / _HUNDRED
    if budget < lot_cost:
        return _reject(RejectionReason.ZERO_LOTS)
    cap = config.allocated_capital * config.max_position_pct / _HUNDRED
    if cap < lot_cost:
        return _reject(RejectionReason.POSITION_CAP)
    return _reject(RejectionReason.ZERO_LOTS)
