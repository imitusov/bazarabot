"""Pure exit evaluation. STOP_LOSS only when the bot owns the stop."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from zarabot.config import Config
from zarabot.models import ExitTrigger, Position, SessionInfo, StopProtection


def evaluate(
    position: Position,
    price: Decimal,
    now: datetime,
    session: SessionInfo,
    trading_days_open: int | None,
    config: Config,
) -> ExitTrigger | None:
    """Return the trigger that fires, or None. Precedence: stop, target, age.

    `trading_days_open` is `None` when the age could not be measured — the
    recorded calendar does not reach the position's entry. MAX_AGE then never
    fires, while stop and target work unchanged: they need only a price. A
    short count would read as a young position, and nothing would raise, which
    is exactly the silence #45 was (spec v1.44).
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("datetime must be timezone-aware")
    local_stop = (
        position.stop_protection is StopProtection.LOCAL
        and price <= position.stop_price
    )
    if local_stop:
        return ExitTrigger.STOP_LOSS
    if price >= position.target_price:
        return ExitTrigger.TAKE_PROFIT
    if (
        trading_days_open is not None
        and trading_days_open >= config.max_holding_days
        and session.in_closing_window(now)
    ):
        return ExitTrigger.MAX_AGE
    return None
