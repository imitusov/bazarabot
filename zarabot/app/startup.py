"""Fixed startup sequence. No entries until the ready alert is sent."""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    PriceRejected,
    get_instrument,
    get_last_price,
    list_stop_orders,
)
from zarabot.broker.reconcile import reconcile
from zarabot.clock import now, to_moscow
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
# Step 2b/2c (spec v1.89, #22). The lock and its alert marker sit beside
# DB_PATH in the bind-mounted data directory (§10); neither is backed up.
_LOCK_SUFFIX = ".instance-lock"
_MARKER_SUFFIX = ".instance-lock.refused"
_MARKER_AGE_OUT = timedelta(hours=24)
_INSTANCE_LOCKED = "INSTANCE_LOCKED"
# The descriptor holding the single-instance lock. It is kept for the lifetime
# of the process and is **never closed by application code** — not by
# `app.shutdown`, not by an exception handler, not by a context manager. The
# kernel releases the lock when the process ends by any means, including
# `SIGKILL`, an OOM kill, `docker kill` and power loss, so a crashed instance
# never leaves a lock a human has to clear. That is the whole reason this is a
# `flock` and not a PID file, a `runtime_state` row or a heartbeat: those record
# liveness, and a liveness record written by a process that then dies is a latch
# with no reset — on a live account, holding open positions, at 3am.
#
# It is bound to a module global rather than to a local for exactly that
# reason: a local would be garbage-collected when `start()` returns, closing
# the descriptor and releasing the lock while the bot traded on.
_lock_fd: int | None = None
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
    notify: bool = True,
) -> NoReturn:
    """Emit `startup_failed`, alert, and raise. The failure is always emitted.

    `notify=False` is step 2c's once-per-condition rule and nothing else: a
    repeat single-instance refusal is logged and exits non-zero exactly as the
    first one does, and only the Telegram message is withheld. Suppressing the
    alert is not suppressing the failure.
    """
    if stage is not None:
        _LOG.critical(
            "startup_failed",
            extra={
                "event": "startup_failed",
                "stage": stage,
                "reason": reason if reason is not None else message,
            },
        )
    if notify and _telegram_usable():
        await alert(message)
    if cause is None:
        raise StartupError(message)
    raise StartupError(message) from cause


def _instance_lock_paths(cfg: Config) -> tuple[Path, Path]:
    """The lock and its refusal marker, both beside `DB_PATH` (§10)."""
    base = str(cfg.db_path)
    return Path(base + _LOCK_SUFFIX), Path(base + _MARKER_SUFFIX)


def _try_lock(lock_path: Path) -> int | None:
    """Take an exclusive non-blocking `flock`, or return `None` if it is held.

    `flock` (`LOCK_EX | LOCK_NB`), deliberately, and not `fcntl.lockf`: `flock`
    locks belong to the *open file description*, so two acquisitions within one
    process contend exactly as two processes do, which is what makes this guard
    testable at all — in one process, with no subprocess and no timing window.
    POSIX record locks belong to the process, would be granted twice inside one
    process, and are dropped by *any* `close()` of *any* descriptor on the file.

    Closing our own descriptor on the contended path releases nothing of the
    holder's, for the same reason.

    The lock is on a file beside `DB_PATH` and never on the database itself:
    `PRAGMA locking_mode = EXCLUSIVE` is incompatible with WAL and would lock
    out `scripts/deploy/export_health.py` and `update.sh`'s read-only
    in-flight-order query.

    Synchronous on purpose, and called from an `async` function: these are two
    local filesystem calls made once, before the loops exist, and the contract
    describes them in exactly these terms. Only `BlockingIOError` — the
    contended case — is handled; any other `OSError` is a real filesystem fault
    and belongs to the rule 15 boundary, which alerts and names it.
    """
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _read_marker(marker_path: Path) -> datetime | None:
    """The instant of the first refusal of this condition, or `None`.

    Absent, unreadable, malformed and naive all read as `None`, which is the
    loud direction (step 2c): a corrupt marker must never be able to silence
    the channel, and neither must a clock that moved backwards — an instant in
    the future is rejected by the window check in `_claim_refusal_alert`.
    """
    try:
        raw = marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        recorded = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return recorded if recorded.tzinfo is not None else None


def _claim_refusal_alert(marker_path: Path, moment: datetime) -> bool:
    """Decide whether this refusal alerts, and record the first one if it does.

    **The marker is deliberately not rewritten on a repeat refusal.** The
    ageing window is measured from the *first* refusal of a condition, not the
    most recent one. Refreshing it on every retry would push the instant
    forward every 30 seconds under exactly the `restart: unless-stopped` loop
    this rule exists for, making the age-out unreachable and turning "alert
    once per condition" into "alert once, ever" — a latch whose reset is
    written down and cannot fire.

    A marker that cannot be written alerts anyway: it is an anti-spam device
    and is never a precondition for the refusal.
    """
    recorded = _read_marker(marker_path)
    if recorded is not None and timedelta() <= moment - recorded <= _MARKER_AGE_OUT:
        _LOG.warning(
            "instance lock refused again; owner alert suppressed, first refusal "
            "of this condition recorded at %s",
            recorded.isoformat(),
            extra={"first_refusal_at": recorded.isoformat()},
        )
        return False
    try:
        marker_path.write_text(moment.isoformat(), encoding="utf-8")
    except OSError as exc:
        _LOG.warning(
            "could not write the instance lock refusal marker at %s: %s",
            marker_path,
            exc,
        )
    return True


def _clear_marker(marker_path: Path) -> None:
    """Reset (1): holding the lock is the proof that there is only one instance.

    The primary reset, and reachable by construction — every normal recovery
    passes through it: the operator kills the stray process, the container is
    replaced, the host reboots. `scripts/ci/check_latches.py` inspects
    module-level booleans and is structurally blind to a file latch, so a green
    run there says nothing whatever about this; the reset is enforced by the
    contract and by the §3.2 cases that exercise it, and by nothing else.

    No `except` here: the directory has just accepted an `os.open` with
    `O_CREAT`, so a failure to unlink is a real filesystem fault and belongs to
    the rule 15 boundary rather than to a swallow.
    """
    marker_path.unlink(missing_ok=True)


async def _take_instance_lock(cfg: Config) -> None:
    """Steps 2b and 2c: refuse to be the second bot on this `DB_PATH`.

    The submission locks in `execution.orders` are `asyncio.Lock` objects
    rebuilt per event loop; they serialise coroutines inside one loop and are
    invisible to a second process. Two instances against one `DB_PATH`
    therefore both pass `_already_open`, both `record_submitting` with two
    different `uuid4()` keys, and both `post_market_order`: **two real market
    buys reach the exchange**, and only then does the second `open_row` violate
    `idx_positions_one_open` and halt, after the money has moved. The
    idempotency key cannot close this — it deduplicates a retry of one intent,
    and two instances form two independent intents.

    Runs after `logging_setup.configure`, so the refusal is redacted and
    structured and `startup_failed` is emittable, and before step 3, so a
    refused instance opens no database, makes no broker call and places no
    order. This is a held lock and not a periodic check: a check at startup
    would say nothing about an instance that starts a minute later.
    """
    lock_path, marker_path = _instance_lock_paths(cfg)
    fd = _try_lock(lock_path)
    if fd is None:
        # The marker is claimed before the alert, so the instant recorded is
        # the one the ageing window is measured from.
        notify = _claim_refusal_alert(marker_path, now())
        await _abort(
            "Startup aborted: another zarabot instance is already running "
            f"against this data directory and holds {lock_path}. This instance "
            "has opened no database, made no broker call and placed no order. "
            "Stop the other instance before starting this one.",
            stage="instance",
            reason=_INSTANCE_LOCKED,
            notify=notify,
        )
    global _lock_fd
    _lock_fd = fd
    _clear_marker(marker_path)


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


# Rule 9's classes for `get_instrument` and `get_last_price`, plus the quote
# rejection `get_last_price` raises on an implausible price. These are what
# "the instrument or price cannot be read" means; anything else is a defect.
_UNREADABLE = (
    BrokerUnavailable,
    BrokerRateLimited,
    InstrumentNotFound,
    PriceRejected,
)


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
        except _UNREADABLE:
            # §4 `app.startup` step 8a: "a ticker whose instrument or price
            # cannot be read is excluded from the judgement and named
            # separately", and "this step never raises `StartupError`, and
            # never prevents startup". A diagnostic that refused to run would
            # abandon every open position, its exits and its stops. The
            # tickers it excludes are reported as `unknown`, never folded into
            # either count.
            #
            # Exactly as broad as that sentence, and no broader (#201). "Cannot
            # be read" is a broker condition, and `_UNREADABLE` is the full set
            # of them these two calls raise. An `AttributeError` from a renamed
            # SDK field is not one: swallowing it would report a wrong
            # cheapest-lot figure as though the check had run, in the module
            # whose job is to establish the system is sound. It propagates to
            # the rule 15 boundary below, which alerts and names the type.
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


def _log_observed_time() -> None:
    """Rule 29: the observed host time, in UTC and MSK, before anything else.

    Clock accuracy is a deployment requirement (V9 confirms NTP), so there is
    deliberately no runtime skew check — the broker exposes no server
    wall-clock, and the one timestamp available lags arbitrarily in a quiet
    market. This line is the whole detector: a host whose clock or zone
    database has drifted produces trading decisions at the wrong moment with
    no other symptom, and both zones are needed because UTC alone cannot show
    a wrong Moscow offset and MSK alone cannot show a wrong instant.

    Emitted immediately after `logging_setup.configure`, which is the earliest
    point at which a log line is redacted and structured, so it is the first
    line this module writes after every restart. The instant comes from
    `clock.now()`; this module may not call `datetime.now()`. It carries no
    token and no account identifier (rule 19).
    """
    moment = now()
    _LOG.info(
        "observed system time utc=%s msk=%s",
        moment.isoformat(),
        to_moscow(moment).isoformat(),
        extra={"utc": moment.isoformat(), "msk": to_moscow(moment).isoformat()},
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
    _log_observed_time()
    # 2b/2c. Before the database, so a refused instance never opens it.
    stage = "instance"
    try:
        await _take_instance_lock(cfg)
        stage = "database"
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
    except Exception as exc:  # noqa: BLE001 — the startup boundary, rule 15
        # Rule 15: startup refuses to start, alerts, sleeps and exits
        # non-zero. This is the outermost frame of the startup path, so
        # nothing above it could act on a narrower class; letting an exception
        # escape here would leave the container restarting with no alert.
        await _abort(
            f"Startup aborted: {exc}",
            exc,
            stage=stage,
            reason=type(exc).__name__,
        )

