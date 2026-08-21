"""Fixed startup sequence. No entries until the ready alert is sent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NoReturn

import aiosqlite

from zarabot.broker.client import get_instrument, list_stop_orders
from zarabot.broker.reconcile import reconcile
from zarabot.clock import now
from zarabot.config import Config, ConfigError, load
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
_SCHEDULE_DAYS = 14
_STOP_TYPES = frozenset(
    {"STOP_MISSING", "STOP_ORPHAN", "STOP_MISPRICED", "STOP_ADOPTABLE"}
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


async def _apply_remedies(report: ReconciliationReport) -> None:
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


def _ready_text(
    cfg: Config, halt: HaltState | None, report: ReconciliationReport
) -> str:
    halted = halt is not None and halt.halted
    reason = ""
    if halted and halt is not None and halt.reason is not None:
        reason = f" ({halt.reason.value})"
    n_adj = len(report.adjustments)
    return (
        f"zarabot {_VERSION} running mode={cfg.trading_mode} "
        f"halted={halted}{reason} adjustments={n_adj}"
    )


async def start() -> AppContext:
    """Load, recover, reconcile, then alert ready. Raises StartupError."""
    try:
        cfg = load()
    except ConfigError as exc:
        await _abort(f"Startup aborted: {exc}", exc)

    os.environ["SSL_TBANK_VERIFY"] = "true" if cfg.ssl_tbank_verify else "false"

    configure(cfg.log_level, [cfg.tinvest_token, cfg.telegram_bot_token])
    try:
        async with aiosqlite.connect(cfg.db_path) as conn:
            await apply(conn)
        strategies = tuple(enabled(cfg))
        await refresh(_SCHEDULE_DAYS)
        moment = now()
        await resolve_unfinished(moment)
        report = await reconcile(moment)
        await _apply_remedies(report)
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
