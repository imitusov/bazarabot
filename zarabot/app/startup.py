"""Fixed startup sequence. No entries until the ready alert is sent."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn

from zarabot.broker.client import (
    get_instrument,
    get_last_price,
    list_stop_orders,
)
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
from zarabot.risk.sizing import position_budget
from zarabot.state.halt import current
from zarabot.strategies.base import Strategy
from zarabot.strategies.registry import enabled
from zarabot.telegram.commands import set_report_builder
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
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
    {
        "CLOSED_EXTERNALLY",
        "ADOPTED",
        "LOTS_ADJUSTED",
        "FOREIGN_HOLDING",
        # A position the broker no longer holds whose sale could not be found in
        # the operations feed. The shares are already gone, so there is nothing
        # for execution.orders to do; it is here so it does not trip the alert
        # reserved for a report this build genuinely cannot read (#11).
        "EXIT_UNRESOLVED",
    }
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


def _env_secrets() -> list[str]:
    return [
        value
        for value in (
            os.environ.get("TINVEST_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TINVEST_ACCOUNT_ID", "").strip(),
        )
        if value
    ]


async def _abort(
    message: str,
    cause: BaseException | None = None,
    *,
    stage: str | None = None,
    reason: str | None = None,
) -> NoReturn:
    if stage is not None:
        _LOG.critical(
            "startup_failed",
            extra={
                "event": "startup_failed",
                "stage": stage,
                "reason": reason if reason is not None else message,
            },
        )
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


@dataclass(frozen=True)
class _Reachability:
    """What one position budget can and cannot buy on the watchlist.

    `unknown` is deliberately a third bucket rather than a default into either
    of the other two. A ticker whose price could not be read is not evidence
    that the budget reaches it, and it is not evidence that the budget does
    not.
    """

    budget: Decimal
    affordable: tuple[str, ...]
    unaffordable: tuple[str, ...]
    unknown: tuple[str, ...]
    cheapest: Decimal | None
    cheapest_ticker: str | None

    @property
    def inconclusive(self) -> bool:
        """No price was readable, so the check observed nothing at all."""
        return not self.affordable and not self.unaffordable

    @property
    def blackout(self) -> bool:
        """Nothing on the watchlist can be bought, and that was observed."""
        return not self.affordable and bool(self.unaffordable)


async def _reachability(cfg: Config) -> _Reachability:
    """Step 8a: compare each watchlist lot cost against one position budget.

    Every read is guarded individually. A ticker that cannot be priced joins
    `unknown` and the rest of the watchlist is still judged, because a single
    unreadable instrument is not a reason to give up the diagnostic for the
    other nine.
    """
    budget = position_budget(cfg.allocated_capital, cfg.position_size_pct)
    affordable: list[str] = []
    unaffordable: list[str] = []
    unknown: list[str] = []
    cheapest: Decimal | None = None
    cheapest_ticker: str | None = None
    for ticker in cfg.watchlist:
        try:
            instrument = await get_instrument(ticker)
            price = await get_last_price(instrument.figi)
            lot_cost = Decimal(instrument.lot) * price
        except Exception:
            unknown.append(ticker)
            continue
        if lot_cost <= 0:
            unknown.append(ticker)
            continue
        if cheapest is None or lot_cost < cheapest:
            cheapest = lot_cost
            cheapest_ticker = ticker
        (affordable if lot_cost <= budget else unaffordable).append(ticker)
    return _Reachability(
        budget=budget,
        affordable=tuple(affordable),
        unaffordable=tuple(unaffordable),
        unknown=tuple(unknown),
        cheapest=cheapest,
        cheapest_ticker=cheapest_ticker,
    )


async def _report_reachability(reach: _Reachability) -> None:
    """Rule 36. Alert only on the total blackout; never raise.

    The partial case is folded into the ready alert by `_ready_text` instead:
    an instrument's price rising through the budget is a normal operating
    condition, and escalating it would put a recurring message into a channel
    whose premise is that silence means healthy.

    Reported once per start rather than per cycle or per signal. At one poll a
    minute the per-signal alternative is several hundred identical messages a
    day, and an alert that repeats forever is equivalent to no alert.
    """
    if not reach.blackout:
        return
    # The cheapest name as well as the cheapest cost: the owner's next question
    # after "nothing is affordable" is "what is the closest thing to it".
    cheapest = "unknown"
    if reach.cheapest is not None:
        cheapest = f"{reach.cheapest} ({reach.cheapest_ticker})"
    await alert(
        "zarabot cannot open a position in anything it is watching: one "
        f"position budget is {reach.budget} and the cheapest lot on the "
        f"watchlist costs {cheapest}. Every signal will be rejected ZERO_LOTS "
        "until the budget rises or the watchlist changes. The bot stays up "
        "and keeps managing what it already holds.",
        urgent=True,
    )


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
        "ALLOW_FOREIGN_HOLDINGS=true to start anyway and leave them untraded.",
        stage="reconcile",
        reason="FOREIGN_HOLDING",
    )


def _reachability_text(reach: _Reachability) -> str:
    """The ready alert's account of step 8a.

    Silence here would be indistinguishable from a confirmed-healthy check, so
    an inconclusive result says so rather than saying nothing.
    """
    parts = []
    if reach.inconclusive:
        parts.append(" budget_check=inconclusive (no price could be read)")
    elif reach.unaffordable:
        parts.append(f" unaffordable={','.join(reach.unaffordable)}")
    if reach.unknown and not reach.inconclusive:
        parts.append(f" unknown={','.join(reach.unknown)}")
    return "".join(parts)


def _ready_text(
    cfg: Config,
    halt: HaltState | None,
    report: ReconciliationReport,
    reach: _Reachability,
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
        f"{_reachability_text(reach)}"
    )


async def start() -> AppContext:
    """Load, recover, reconcile, then alert ready. Raises StartupError."""
    try:
        cfg = load()
    except ConfigError as exc:
        configure("INFO", _env_secrets())
        _LOG.critical(
            "config_invalid",
            extra={
                "event": "config_invalid",
                "variable": exc.variable,
            },
        )
        await _abort(f"Startup aborted: {exc}", exc)

    os.environ["SSL_TBANK_VERIFY"] = "true" if cfg.ssl_tbank_verify else "false"
    if not cfg.ssl_tbank_verify:
        await alert(_SSL_DISABLED_TEXT, urgent=True)

    configure(
        cfg.log_level,
        [cfg.tinvest_token, cfg.telegram_bot_token, cfg.tinvest_account_id],
    )
    stage = "database"
    try:
        await connect(str(cfg.db_path))
        await apply(shared())
        stage = "strategies"
        strategies = tuple(enabled(cfg))
        stage = "session"
        await refresh(_SCHEDULE_DAYS)
        moment = now()
        stage = "recovery"
        await resolve_unfinished(moment)
        stage = "reconcile"
        report = await reconcile(moment)
        await _apply_remedies(report)
        await _enforce_account_exclusivity(cfg, report)
        stage = "halt"
        halt = await current()
        # 8a. Never raises, never prevents startup: an unaffordable budget
        # stops new entries only, and refusing to start would additionally
        # abandon every open position. A diagnostic must not become the reason
        # the bot is down.
        stage = "reachability"
        reach = await _reachability(cfg)
        await _report_reachability(reach)
        set_report_builder(build_report)
        stage = "ready"
        await alert(_ready_text(cfg, halt, report, reach))
        # The same four facts as the alert, for a reader that cannot read
        # Telegram: `scripts/deploy/update.sh` waits for this event and rolls
        # the deploy back without it (§7.1, spec v1.58).
        _LOG.info(
            "startup complete",
            extra={
                "event": "startup_ok",
                "version": _VERSION,
                "mode": cfg.trading_mode,
                "halted": halt is not None and halt.halted,
                "adjustments_count": len(report.adjustments),
            },
        )
        return AppContext(
            config=cfg,
            strategies=strategies,
            halt=halt,
            reconciliation=report,
        )
    except StartupError:
        raise
    except (MigrationError, ConfigError) as exc:
        await _abort(
            f"Startup aborted: {exc}",
            exc,
            stage=stage,
            reason=type(exc).__name__,
        )
    except Exception as exc:
        await _abort(
            f"Startup aborted: {exc}",
            exc,
            stage=stage,
            reason=type(exc).__name__,
        )

