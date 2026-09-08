"""Authorised-chat command handlers. Never mutates a risk limit."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from zarabot import config
from zarabot.broker.client import get_last_price
from zarabot.clock import moscow_date, now, to_moscow
from zarabot.db.positions import list_closed, list_open
from zarabot.db.snapshots import list_for_period
from zarabot.market.session import current_session, is_open, next_open
from zarabot.models import HaltReason, Position
from zarabot.pnl import benchmark_return, realised, unrealised
from zarabot.state.halt import current
from zarabot.state.halt import halt as persist_halt
from zarabot.state.halt import resume as persist_resume
from zarabot.strategies.registry import enabled

_LOG = logging.getLogger(__name__)
_LIMIT = 4096
_ATTEMPTS = 3
_CENTS = Decimal("0.01")
_UNAVAILABLE = "report unavailable"
_NOTHING_HALTED = "Nothing was halted."

ReportBuilder = Callable[[date, date], Awaitable[str]]

_report_builder: ReportBuilder | None = None

_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/status", "Halt state, today's P&L, orders today, open count, next session"),
    ("/positions", "Open positions with entry, current price, unrealised P&L"),
    ("/history", "Most recent closed trades and outcomes"),
    ("/pnl", "Today, this week, and inception P&L versus buy-and-hold"),
    ("/halt", "Stop opening new positions; existing positions stay open"),
    ("/resume", "Clear a halt and confirm current risk-limit state"),
    ("/strategies", "Enabled strategies and performance to date"),
    ("/report", "Weekly report on demand"),
    ("/help", "List commands"),
)


def set_report_builder(builder: ReportBuilder | None) -> None:
    """Inject `reporter.weekly.build`. None means `/report` is unavailable."""
    global _report_builder
    _report_builder = builder


def _fmt_money(value: Decimal) -> str:
    return f"{value.quantize(_CENTS)}"


def _fmt_moscow(moment: datetime) -> str:
    return to_moscow(moment).strftime("%Y-%m-%d %H:%M MSK")


def _duration(moment: datetime, event: datetime) -> str:
    seconds = max(int((event - moment).total_seconds()), 0)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def _week_bounds(moment: datetime) -> tuple[date, date]:
    local = to_moscow(moment)
    start = local.date() - timedelta(days=local.weekday())
    return start, start + timedelta(days=6)


def _on_or_between(position: Position, start: date, end: date) -> bool:
    if position.exit_at is None:
        return False
    day = moscow_date(position.exit_at)
    return start <= day <= end


def _join_truncated(header: str, entries: list[str]) -> str:
    if not entries:
        text = header.rstrip()
        return text if len(text) <= _LIMIT else text[:_LIMIT]
    kept: list[str] = []
    for index, entry in enumerate(entries):
        rest = len(entries) - index - 1
        note = f"\n({rest} entries omitted)" if rest else ""
        candidate = header + "\n".join([*kept, entry]) + note
        if len(candidate) <= _LIMIT:
            kept.append(entry)
            continue
        omitted = len(entries) - len(kept)
        body = header + "\n".join(kept)
        if omitted:
            body += f"\n({omitted} entries omitted)"
        return body if len(body) <= _LIMIT else body[:_LIMIT]
    return header + "\n".join(kept)


def _command_name(update: Update) -> str:
    message = update.message
    text = getattr(message, "text", None) if message is not None else None
    if not text:
        return "unknown"
    token = str(text).strip().split()[0]
    return token.lstrip("/").split("@")[0]


def _authorised(update: Update) -> bool:
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    if chat_id == config.get().telegram_chat_id:
        return True
    _LOG.info(
        "unauthorised_command",
        extra={
            "event": "unauthorised_command",
            "chat_id": chat_id,
            "command": _command_name(update),
        },
    )
    return False


async def _reply(update: Update, text: str) -> None:
    message = update.message
    if message is None:
        return
    last_error: Exception | None = None
    for _attempt in range(_ATTEMPTS):
        try:
            await message.reply_text(text)
            return
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        _LOG.error("telegram send failed after retries")


async def _today_snapshot_pnl() -> tuple[Decimal, int]:
    today = moscow_date(now())
    rows = await list_for_period(today, today)
    if not rows:
        return Decimal(0), 0
    snap = rows[0]
    return snap.realised_pnl + snap.unrealised_pnl, snap.orders_placed


async def _status_text() -> str:
    moment = now()
    state = await current()
    if state is not None and state.halted:
        reason = state.reason.value if state.reason is not None else "unknown"
        trading = f"Trading: halted ({reason})"
    else:
        trading = "Trading: active"
    today_pnl, orders = await _today_snapshot_pnl()
    opens = await list_open()
    if is_open(moment):
        session = current_session(moment)
        event = session.end if session is not None else None
        label = "until session close"
    else:
        event = next_open(moment)
        label = "until session open"
    if event is None or event == moment:
        until = "next session unknown"
    else:
        until = f"{_duration(moment, event)} {label}"
    return (
        f"{trading}\n"
        f"Today's P&L: {_fmt_money(today_pnl)}\n"
        f"Orders placed today: {orders}\n"
        f"Open positions: {len(opens)}\n"
        f"Next session event: {until}"
    )


async def _position_line(position: Position) -> str:
    try:
        price = await get_last_price(position.figi)
        mark = _fmt_money(price)
        pnl_text = _fmt_money(unrealised(position, price))
    except Exception:
        mark = "N/A"
        pnl_text = "N/A"
    return (
        f"{position.ticker}  lots={position.lots}  "
        f"entry={_fmt_money(position.entry_price)}  current={mark}  "
        f"unrealised={pnl_text}  {position.strategy}"
    )


async def _positions_text() -> str:
    opens = await list_open()
    if not opens:
        return "No open positions."
    lines = [await _position_line(position) for position in opens]
    return _join_truncated("Open positions:\n", lines)


def _history_line(position: Position) -> str:
    trigger = position.exit_trigger.value if position.exit_trigger else "N/A"
    if position.exit_price is not None:
        price = _fmt_money(position.exit_price)
    else:
        price = "N/A"
    pnl_text = (
        _fmt_money(realised(position)) if position.realised_pnl is not None else "N/A"
    )
    when = _fmt_moscow(position.exit_at) if position.exit_at is not None else "N/A"
    return f"{position.ticker}  {trigger}  exit={price}  pnl={pnl_text}  {when}"


async def _history_text() -> str:
    closed = await list_closed()
    if not closed:
        return "No closed trades."
    return _join_truncated("Recent trades:\n", [_history_line(row) for row in closed])


def _sum_realised(rows: list[Position]) -> Decimal:
    total = Decimal(0)
    for row in rows:
        total += realised(row)
    return total


def _fmt_benchmark(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return _fmt_money(value)


async def _pnl_text() -> str:
    moment = now()
    today = moscow_date(moment)
    week_start, week_end = _week_bounds(moment)
    closed = await list_closed()
    today_pnl, _orders = await _today_snapshot_pnl()
    week_pnl = _sum_realised(
        [row for row in closed if _on_or_between(row, week_start, week_end)]
    )
    inception_pnl = _sum_realised(closed)
    if closed:
        dates = [moscow_date(row.exit_at) for row in closed if row.exit_at is not None]
        inception_start = min(dates) if dates else today
    else:
        inception_start = today
    today_bm = await benchmark_return(today, today)
    week_bm = await benchmark_return(week_start, week_end)
    inception_bm = await benchmark_return(inception_start, today)
    return (
        f"Today: {_fmt_money(today_pnl)}  benchmark {_fmt_benchmark(today_bm)}\n"
        f"This week: {_fmt_money(week_pnl)}  benchmark {_fmt_benchmark(week_bm)}\n"
        f"Since inception: {_fmt_money(inception_pnl)}  "
        f"benchmark {_fmt_benchmark(inception_bm)}"
    )


def _risk_limit_text() -> str:
    """The limits that can actually stop an order, and only those.

    Every line here has a control behind it: the daily loss limit halts entries,
    the position size and the cash reserve bound what one order may spend, the
    exposure ceiling and the position count bound the portfolio, the cooldown
    bounds repetition, and the exit rules bound a single holding. The withdrawn
    `MAX_POSITION_PCT` is absent because it never bound anything, and a limit an
    operator believes but that cannot bind is worse than no limit at all (#15).
    """
    cfg = config.get()
    return (
        "Risk limits (no command can change them):\n"
        f"Daily loss limit: {cfg.daily_loss_limit_pct}% of allocated capital "
        "— halts new entries\n"
        f"Position size on entry: {cfg.position_size_pct}% of allocated capital\n"
        "Portfolio exposure ceiling: open positions cost at most "
        f"{_fmt_money(cfg.allocated_capital)} in total\n"
        f"Cash reserve: {cfg.cash_reserve_pct}% of cash held back from every order\n"
        f"Maximum open positions: {cfg.max_open_positions}\n"
        f"Re-entry cooldown: {cfg.reentry_cooldown_minutes} minutes per instrument\n"
        f"Exits: stop {cfg.stop_loss_pct}%, target {cfg.take_profit_pct}%, "
        f"maximum holding {cfg.max_holding_days} trading days"
    )


async def _strategies_text() -> str:
    closed = await list_closed()
    by_name: dict[str, list[Position]] = {}
    for row in closed:
        by_name.setdefault(row.strategy, []).append(row)
    lines: list[str] = []
    for strategy in enabled(config.get()):
        trades = by_name.get(strategy.name, [])
        if not trades:
            lines.append(f"{strategy.name}: N/A")
            continue
        total = _sum_realised(trades)
        lines.append(f"{strategy.name}: pnl={_fmt_money(total)} trades={len(trades)}")
    return _join_truncated("Enabled strategies:\n", lines)


async def _report_text() -> str:
    builder = _report_builder
    if builder is None:
        return _UNAVAILABLE
    start, end = _week_bounds(now())
    return await builder(start, end)


def _help_text() -> str:
    return "\n".join(f"{name} — {blurb}" for name, blurb in _COMMANDS)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _status_text())


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _positions_text())


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _history_text())


async def pnl(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _pnl_text())


async def halt(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await persist_halt(HaltReason.MANUAL, "manual halt via /halt", now())
    await _reply(update, "Entries halted. Open positions are left untouched.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    if not await persist_resume("telegram", now()):
        await _reply(update, _NOTHING_HALTED)
        return
    await _reply(update, f"Resumed.\n{_risk_limit_text()}")


async def strategies(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _strategies_text())


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, await _report_text())


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> None:
    if not _authorised(update):
        return
    await _reply(update, _help_text())


def build_application() -> Application[Any, Any, Any, Any, Any, Any]:
    """PTB application with every brief command registered."""
    application = Application.builder().token(config.get().telegram_bot_token).build()
    for name, handler in (
        ("status", status),
        ("positions", positions),
        ("history", history),
        ("pnl", pnl),
        ("halt", halt),
        ("resume", resume),
        ("strategies", strategies),
        ("report", report),
        ("help", help),
    ):
        application.add_handler(CommandHandler(name, handler))
    return application
