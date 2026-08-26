"""Fixed startup sequence. No entries until the ready alert is sent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NoReturn

from zarabot.broker.client import get_instrument, list_stop_orders
from zarabot.broker.reconcile import reconcile
from zarabot.clock import now
from zarabot.config import Config, ConfigError, load
from zarabot.db.connection import connect, shared
from zarabot.db.migrations import MigrationError, apply
from zarabot.db.positions import list_open
from zarabot.execution.orders import (
    adopt_existing_stop,
    cancel_orphaned_stop,
    place_protective_stop,
    replace_stop,
    resolve_unfinished,
)
from zarabot.logging_setup import configure
from zarabot.market.session import refresh
from zarabot.models import HaltState, ReconciliationReport, StopOrderRecord
from zarabot.reporter.weekly import build as build_report
from zarabot.state.halt import current
from zarabot.strategies.base import Strategy
from zarabot.strategies.registry import enabled
from zarabot.telegram.commands import set_report_builder
from zarabot.telegram.notifier import alert

_VERSION = "0.1.0"
_SSL_DISABLED_TEXT = (
    "zarabot starting with SSL_TBANK_VERIFY=false: certificate verification "
    "is disabled on the connection that carries the trading token"
)
_SCHEDULE_DAYS = 14
# Types the executor acts on. `STOP_DUPLICATE` belongs here: two live stops
# against one position is the double-sell condition, and an executor with no
# branch for it detected, reported and then dropped it (#35).
_STOP_TYPES = frozenset(
    {
        "STOP_MISSING",
        "STOP_ORPHAN",
        "STOP_MISPRICED",
        "STOP_ADOPTABLE",
        "STOP_DUPLICATE",
    }
)
# Types reconciliation resolves itself. They need no remedy, but they are known,
# so they must not be reported as an adjustment the executor cannot act on.
_OBSERVED_TYPES = frozenset(
    {"CLOSED_EXTERNALLY", "ADOPTED", "LOTS_ADJUSTED", "FOREIGN_HOLDING"}
)


class StartupError(Exception):
    """Startup aborted before the bot was ready. No entry has been attempted."""


@dataclass(frozen=True)
class AppContext:
    config: Config
    strategies: tuple[Strategy, ...]
    halt: HaltState | None
    reconciliation: ReconciliationReport


def _telegram_usable() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat)


async def _abort(message: str, cause: BaseException | None = None) -> NoReturn:
    if _telegram_usable():
        await alert(message)
    if cause is None:
        raise StartupError(message)
    raise StartupError(message) from cause


def _position_id(item: dict[str, object]) -> int | None:
    raw = item.get("position_id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _find_stop(
    item: dict[str, object], stops: list[StopOrderRecord]
) -> StopOrderRecord | None:
    stop_id = item.get("stop_order_id")
    key = item.get("key")
    for stop in stops:
        if stop_id and stop.stop_order_id == stop_id:
            return stop
        if key and stop.key == key:
            return stop
    return None


def _find_by_identifier(
    identifier: str, stops: list[StopOrderRecord]
) -> StopOrderRecord | None:
    """Resolve an adjustment identifier: the broker's id, else our key."""
    for stop in stops:
        if stop.stop_order_id == identifier or stop.key == identifier:
            return stop
    return None


async def _resolve_duplicate(
    item: dict[str, object], stops: list[StopOrderRecord]
) -> None:
    """Cancel every identifier in `cancel`; retain `keep`.

    Reconciliation chose which stop survives — the one matching the position's
    `stop_order_key`, else the oldest. The executor only carries that decision
    out. A duplicate it cannot resolve stays live, so it alerts rather than
    passing: an uncancelled second stop is the double-sell condition.
    """
    keep = item.get("keep")
    raw = item.get("cancel")
    identifiers = raw if isinstance(raw, list | tuple) else ()
    for entry in identifiers:
        if not isinstance(entry, str) or entry == keep:
            continue
        stop = _find_by_identifier(entry, stops)
        if stop is None:
            await alert(
                f"duplicate stop {entry} on {item.get('ticker')} could not be "
                "matched to a live stop order and was not cancelled",
                urgent=True,
            )
            continue
        await cancel_orphaned_stop(stop)


async def _report_unhandled(report: ReconciliationReport) -> None:
    """Alert on any adjustment type this executor has no branch for.

    An adjustment that falls through every branch is silently dropped, which is
    exactly how `STOP_DUPLICATE` was lost (#35). A report the executor does not
    understand must be loud.
    """
    known = _STOP_TYPES | _OBSERVED_TYPES
    unknown = sorted(
        {
            str(item.get("type"))
            for item in report.adjustments
            if item.get("type") not in known
        }
    )
    if unknown:
        await alert(
            "reconciliation reported adjustment types this build cannot act on: "
            f"{', '.join(unknown)} — no remedy was applied for them",
            urgent=True,
        )


async def _apply_remedies(report: ReconciliationReport) -> None:
    await _report_unhandled(report)
    kinds = [item.get("type") for item in report.adjustments]
    if not any(kind in _STOP_TYPES for kind in kinds):
        return
    opened = {position.id: position for position in await list_open()}
    stops = await list_stop_orders()
    for item in report.adjustments:
        kind = item.get("type")
        position_id = _position_id(item)
        position = opened.get(position_id) if position_id is not None else None
        if kind == "STOP_MISSING":
            if position is None:
                continue
            instrument = await get_instrument(position.ticker)
            await place_protective_stop(position, instrument)
        elif kind == "STOP_MISPRICED":
            if position is None:
                continue
            instrument = await get_instrument(position.ticker)
            await replace_stop(position, instrument)
        elif kind == "STOP_ADOPTABLE":
            stop = _find_stop(item, stops)
            if position is None or stop is None:
                continue
            await adopt_existing_stop(position, stop)
        elif kind == "STOP_ORPHAN":
            stop = _find_stop(item, stops)
            if stop is None:
                continue
            await cancel_orphaned_stop(stop)
        elif kind == "STOP_DUPLICATE":
            await _resolve_duplicate(item, stops)


def _foreign_tickers(report: ReconciliationReport) -> list[str]:
    """Every ticker the broker holds that the bot has no record of."""
    seen: list[str] = []
    for item in report.adjustments:
        if item.get("type") != "FOREIGN_HOLDING":
            continue
        ticker = str(item.get("ticker"))
        if ticker not in seen:
            seen.append(ticker)
    return seen


async def _enforce_account_exclusivity(
    cfg: Config, report: ReconciliationReport
) -> None:
    """Rule 32: refuse to start on a holding the bot has no record of.

    The account is the bot's alone (brief v1.8). The bot cannot tell "someone
    bought this by hand" from "local state is wrong", and both readings forbid
    trading it. Refusing is the correct failure direction; the alternative is
    selling something the owner chose to hold, at a price they did not choose.
    With `allow_foreign_holdings` set, the holdings are named in the ready alert
    instead and are never traded — reconciliation writes no position row for
    them, so no stop is placed, no exit evaluated and no sale made.
    """
    foreign = _foreign_tickers(report)
    if not foreign or cfg.allow_foreign_holdings:
        return
    await _abort(
        "Startup aborted: the broker reports holdings the bot has no record of "
        f"({', '.join(foreign)}). The account is the bot's alone; set "
        "ALLOW_FOREIGN_HOLDINGS=true to start anyway and leave them untraded."
    )


def _ready_text(
    cfg: Config, halt: HaltState | None, report: ReconciliationReport
) -> str:
    halted = halt is not None and halt.halted
    reason = ""
    if halted and halt is not None and halt.reason is not None:
        reason = f" ({halt.reason.value})"
    n_adj = len(report.adjustments)
    foreign = _foreign_tickers(report)
    holdings = ""
    if foreign:
        holdings = f" foreign_holdings={','.join(foreign)} (not traded)"
    return (
        f"zarabot {_VERSION} running mode={cfg.trading_mode} "
        f"halted={halted}{reason} adjustments={n_adj}{holdings}"
    )


async def start() -> AppContext:
    """Load, recover, reconcile, then alert ready. Raises StartupError."""
    try:
        cfg = load()
    except ConfigError as exc:
        await _abort(f"Startup aborted: {exc}", exc)

    os.environ["SSL_TBANK_VERIFY"] = "true" if cfg.ssl_tbank_verify else "false"
    if not cfg.ssl_tbank_verify:
        await alert(_SSL_DISABLED_TEXT, urgent=True)

    configure(cfg.log_level, [cfg.tinvest_token, cfg.telegram_bot_token])
    try:
        await connect(str(cfg.db_path))
        await apply(shared())
        strategies = tuple(enabled(cfg))
        await refresh(_SCHEDULE_DAYS)
        moment = now()
        await resolve_unfinished(moment)
        report = await reconcile(moment)
        await _apply_remedies(report)
        await _enforce_account_exclusivity(cfg, report)
        halt = await current()
        set_report_builder(build_report)
        await alert(_ready_text(cfg, halt, report))
        return AppContext(
            config=cfg,
            strategies=strategies,
            halt=halt,
            reconciliation=report,
        )
    except StartupError:
        raise
    except (MigrationError, ConfigError) as exc:
        await _abort(f"Startup aborted: {exc}", exc)
    except Exception as exc:
        await _abort(f"Startup aborted: {exc}", exc)
