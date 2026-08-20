"""Sunday (and on-demand) weekly report. Undefined metrics are N/A, never 0."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from zarabot.clock import moscow_date, to_moscow
from zarabot.db.positions import list_closed
from zarabot.db.signals import list_for_period
from zarabot.models import ExitTrigger, Position, RejectionReason
from zarabot.pnl import benchmark_return, realised
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_LIMIT = 4096
_CENTS = Decimal("0.01")
_OMISSION = "Least important sections omitted to fit the message limit."
_DROP_ORDER = (
    "Exit-trigger distribution",
    "Cooldown-blocked signals",
    "Worst trade",
)


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _fmt_money(value: Decimal) -> str:
    return f"{value.quantize(_CENTS)}"


def _week_bounds(moment: datetime) -> tuple[date, date]:
    local = to_moscow(moment)
    start = local.date() - timedelta(days=local.weekday())
    return start, start + timedelta(days=6)


def _in_period(position: Position, start: date, end: date) -> bool:
    if position.exit_at is None:
        return False
    day = moscow_date(position.exit_at)
    return start <= day <= end


def _is_gap(position: Position) -> bool:
    if position.exit_price is None:
        return False
    if position.exit_trigger is ExitTrigger.STOP_LOSS:
        return position.exit_price != position.stop_price
    if position.exit_trigger is ExitTrigger.TAKE_PROFIT:
        return position.exit_price != position.target_price
    return False


def _intended_price(position: Position) -> Decimal:
    if position.exit_trigger is ExitTrigger.STOP_LOSS:
        return position.stop_price
    return position.target_price


def _pnl_section(trades: list[Position], benchmark: Decimal | None) -> str:
    if not trades:
        pnl_line = "P&L: N/A\nNo closed trades this week."
    else:
        total = sum((realised(row) for row in trades), Decimal(0))
        pnl_line = f"P&L: {_fmt_money(total)}"
    if benchmark is None:
        bench_line = "Benchmark: N/A"
    else:
        bench_line = f"Benchmark: {_fmt_money(benchmark)}"
    return f"{pnl_line}\n{bench_line}"


def _strategy_section(trades: list[Position]) -> str:
    if not trades:
        return "Per-strategy:\nN/A"
    by_name: dict[str, list[Position]] = {}
    for row in trades:
        by_name.setdefault(row.strategy, []).append(row)
    lines = ["Per-strategy:"]
    for name, rows in sorted(by_name.items()):
        total = sum((realised(row) for row in rows), Decimal(0))
        lines.append(f"{name}: {_fmt_money(total)} ({len(rows)} trades)")
    return "\n".join(lines)


def _win_rate_section(trades: list[Position]) -> str:
    if not trades:
        return "Win rate: N/A"
    wins = sum(1 for row in trades if realised(row) > 0)
    rate = (Decimal(wins) / Decimal(len(trades))) * Decimal(100)
    return f"Win rate: {rate.quantize(_CENTS)}%"


def _worst_section(trades: list[Position]) -> str:
    if not trades:
        return "Worst trade: N/A"
    worst = min(trades, key=realised)
    return f"Worst trade: {worst.ticker} {_fmt_money(realised(worst))}"


def _trigger_section(trades: list[Position]) -> str:
    if not trades:
        return "Exit-trigger distribution:\nN/A"
    counts: Counter[str] = Counter(
        row.exit_trigger.value if row.exit_trigger is not None else "N/A"
        for row in trades
    )
    lines = ["Exit-trigger distribution:"]
    for name in (
        ExitTrigger.STOP_LOSS.value,
        ExitTrigger.TAKE_PROFIT.value,
        ExitTrigger.MAX_AGE.value,
        ExitTrigger.EXTERNAL.value,
    ):
        lines.append(f"{name}: {counts.get(name, 0)}")
    return "\n".join(lines)


def _cooldown_section(blocked: int) -> str:
    return f"Cooldown-blocked signals: {blocked}"


def _gap_section(trades: list[Position]) -> str:
    gaps = [row for row in trades if _is_gap(row)]
    if not gaps:
        return "Gapped exits:\nNone"
    lines = ["Gapped exits:"]
    for row in gaps:
        intended = _fmt_money(_intended_price(row))
        actual = _fmt_money(row.exit_price) if row.exit_price is not None else "N/A"
        lines.append(f"{row.ticker} intended={intended} actual={actual}")
    return "\n".join(lines)


def _compose(named: dict[str, str], omitted: list[str], header: str) -> str:
    parts = [header]
    for title, body in named.items():
        if title not in omitted:
            parts.append(body)
    if omitted:
        parts.append(_OMISSION)
    return "\n\n".join(parts)


async def build(start: date, end: date) -> str:
    """Compose the weekly report for the inclusive Moscow date range."""
    closed = [row for row in await list_closed() if _in_period(row, start, end)]
    signals = await list_for_period(start, end)
    blocked = sum(
        1
        for _signal, decision in signals
        if decision.reason is RejectionReason.COOLDOWN_ACTIVE
    )
    benchmark = await benchmark_return(start, end)
    header = f"Weekly report {start.isoformat()} to {end.isoformat()}"
    named = {
        "P&L": _pnl_section(closed, benchmark),
        "Per-strategy": _strategy_section(closed),
        "Win rate": _win_rate_section(closed),
        "Worst trade": _worst_section(closed),
        "Exit-trigger distribution": _trigger_section(closed),
        "Cooldown-blocked signals": _cooldown_section(blocked),
        "Gapped exits": _gap_section(closed),
    }
    omitted: list[str] = []
    text = _compose(named, omitted, header)
    for title in _DROP_ORDER:
        if len(text) <= _LIMIT:
            break
        omitted.append(title)
        text = _compose(named, omitted, header)
    return text


async def send(now: datetime) -> None:
    """Build this week's report and send it. Never raises."""
    _reject_naive(now)
    try:
        start, end = _week_bounds(now)
        text = await build(start, end)
        await alert(text)
    except Exception:
        _LOG.exception("weekly report failed")
        try:
            await alert("Weekly report failed to send.")
        except Exception:
            _LOG.exception("weekly report failure alert failed")
