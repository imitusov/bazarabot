"""Tests for zarabot.app.startup — written from technical-spec.md §3.2."""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    PriceRejected,
)
from zarabot.db.connection import connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.models import HaltReason, Instrument, ReconciliationReport
from zarabot.state.halt import halt, is_halted

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENV = {
    "TINVEST_TOKEN": "tinvest-secret-token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
    "TELEGRAM_CHAT_ID": "42",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}
# Step 8a reads a lot size and a last price per watchlist ticker. Every value
# below is the one measured on the live account on 2026-09-07, so the
# affordability arithmetic in these tests is the arithmetic the bot will do.
LOTS = {"SBER": 1, "GAZP": 10, "MGNT": 1, "LKOH": 1}
PRICES = {
    "SBER": Decimal("279.83"),
    "GAZP": Decimal("90.40"),
    "MGNT": Decimal("1632"),
    "LKOH": Decimal("5003"),
}


def _instrument_of(ticker: str) -> Instrument:
    return Instrument(
        figi=f"FIGI-{ticker}",
        ticker=ticker,
        lot=LOTS[ticker],
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


@pytest.fixture(autouse=True)
async def _close_process_connection() -> None:
    yield
    await disconnect()


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    calls: list[str] = []
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)

    async def _refresh(days: int) -> None:
        calls.append("refresh")

    async def _resolve(moment: datetime) -> list[object]:
        calls.append("resolve")
        return []

    async def _reconcile(moment: datetime) -> ReconciliationReport:
        calls.append("reconcile")
        return ReconciliationReport(ran_at=moment, adjustments=())

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append(f"alert:{text}")

    async def _instrument(ticker: str) -> Instrument:
        return _instrument_of(ticker)

    async def _last_price(figi: str) -> Decimal:
        return PRICES[figi.removeprefix("FIGI-")]

    monkeypatch.setattr("zarabot.app.startup.get_instrument", _instrument)
    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)
    monkeypatch.setattr("zarabot.app.startup.refresh", _refresh)
    monkeypatch.setattr("zarabot.app.startup.resolve_unfinished", _resolve)
    monkeypatch.setattr("zarabot.app.startup.reconcile", _reconcile)
    monkeypatch.setattr("zarabot.app.startup.alert", _alert)
    monkeypatch.setattr("zarabot.app.startup.now", lambda: NOW)
    return calls


async def test_startup_with_valid_config_completes_and_reports_ready(
    env: list[str],
) -> None:
    from zarabot.app.startup import start

    ctx = await start()
    assert ctx.config.trading_mode == "live"
    assert any(item.startswith("alert:") and "running" in item.lower() for item in env)
    assert "resolve" in env
    assert "reconcile" in env
    assert "refresh" in env


async def test_invalid_config_aborts_before_any_broker_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import StartupError, start

    monkeypatch.setenv("TINVEST_TOKEN", "tinvest-secret-token")
    monkeypatch.setenv("TINVEST_ACCOUNT_ID", "acct")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("WATCHLIST", "SBER")
    monkeypatch.delenv("ALLOCATED_CAPITAL", raising=False)
    broker_calls: list[str] = []

    async def _portfolio() -> None:
        broker_calls.append("portfolio")

    async def _refresh(days: int) -> None:
        broker_calls.append("refresh")

    monkeypatch.setattr("zarabot.broker.client.get_portfolio", _portfolio)
    monkeypatch.setattr("zarabot.app.startup.refresh", _refresh)
    alerts: list[str] = []

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    monkeypatch.setattr("zarabot.app.startup.alert", _alert)
    with pytest.raises(StartupError):
        await start()
    assert broker_calls == []
    assert alerts


async def test_invalid_config_emits_config_invalid_and_not_startup_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: missing config is a catalogued event, not a silent abort."""
    from zarabot.app.startup import StartupError, start

    monkeypatch.setenv("TINVEST_TOKEN", "tinvest-secret-token")
    monkeypatch.setenv("TINVEST_ACCOUNT_ID", "acct")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("WATCHLIST", "SBER")
    monkeypatch.delenv("ALLOCATED_CAPITAL", raising=False)
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    monkeypatch.setattr("zarabot.app.startup.alert", _alert)
    with (
        caplog.at_level(logging.CRITICAL, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "config_invalid"
    ]
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.CRITICAL
    assert record.variable == "ALLOCATED_CAPITAL"
    assert not any(
        getattr(item, "event", None) == "startup_ok" for item in caplog.records
    )
    assert "tinvest-secret-token" not in caplog.text


async def test_config_invalid_uses_variable_attribute_not_message_split(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """config_invalid.variable is ConfigError.variable, not str(exc).split()[0]."""
    from zarabot.app.startup import StartupError, start
    from zarabot.config import ConfigError

    def _load() -> object:
        raise ConfigError(
            "Missing required setting for this environment",
            variable="ALLOCATED_CAPITAL",
        )

    monkeypatch.setattr("zarabot.app.startup.load", _load)
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    monkeypatch.setattr("zarabot.app.startup.alert", _alert)
    with (
        caplog.at_level(logging.CRITICAL, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "config_invalid"
    ]
    assert len(events) == 1
    assert events[0].variable == "ALLOCATED_CAPITAL"


async def test_ssl_tbank_verify_is_present_before_first_broker_call(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import start
    from zarabot.config import load as real_load

    monkeypatch.delenv("SSL_TBANK_VERIFY", raising=False)
    cfg = real_load()
    assert cfg.ssl_tbank_verify is True

    def _load() -> object:
        return cfg

    broker_env: list[str | None] = []

    async def _refresh(days: int) -> None:
        broker_env.append(os.environ.get("SSL_TBANK_VERIFY"))
        env.append("refresh")

    monkeypatch.setattr("zarabot.app.startup.load", _load)
    monkeypatch.setattr("zarabot.app.startup.refresh", _refresh)
    await start()
    assert broker_env == ["true"]


async def test_unresolved_order_is_resolved_before_strategy_evaluation(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import start
    from zarabot.strategies.ma_crossover import MovingAverageCrossover

    def _evaluate(self: object, ticker: str, candles: object, now: object) -> None:
        env.append("evaluate")
        return

    monkeypatch.setattr(MovingAverageCrossover, "evaluate", _evaluate)
    await start()
    assert "evaluate" not in env
    assert env.index("resolve") < env.index("reconcile")


async def test_reconciliation_runs_before_first_entry_is_permitted(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import start

    async def _open(*args: object, **kwargs: object) -> None:
        env.append("open_position")
        return

    monkeypatch.setattr("zarabot.execution.orders.open_position", _open)
    await start()
    assert "reconcile" in env
    assert "open_position" not in env
    assert env.index("resolve") < env.index("reconcile")


async def test_halted_at_shutdown_starts_halted(env: list[str]) -> None:
    from zarabot.app.startup import start

    await connect(os.environ["DB_PATH"])
    await apply(shared())
    await halt(HaltReason.MANUAL, "left halted", NOW)
    await disconnect()
    ctx = await start()
    assert await is_halted() is True
    assert ctx.halt is not None
    assert ctx.halt.halted is True
    assert ctx.halt.reason is HaltReason.MANUAL
    assert any("halt" in item.lower() for item in env if item.startswith("alert:"))


async def test_schema_failure_alerts_and_raises_startup_error(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import StartupError, start
    from zarabot.db.migrations import MigrationError

    async def _boom(conn: object) -> int:
        raise MigrationError("schema version 99 is ahead of code version 1")

    monkeypatch.setattr("zarabot.app.startup.apply", _boom)
    with pytest.raises(StartupError):
        await start()
    assert any("aborted" in item.lower() for item in env if item.startswith("alert:"))


async def test_startup_applies_reported_stop_remedies(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    from zarabot.app.startup import start
    from zarabot.models import (
        Instrument,
        Position,
        StopOrderRecord,
        StopOrderStatus,
        StopProtection,
    )

    position = Position(
        id=1,
        ticker="SBER",
        figi="BBG000000001",
        strategy="ma_crossover",
        lots=1,
        lot_size=10,
        entry_price=Decimal("100"),
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=False,
        open_order_key="open-1",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )
    instrument = Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )
    adopt_stop = StopOrderRecord(
        key="adopt-k",
        stop_order_id="s1",
        position_id=1,
        ticker="SBER",
        lots=1,
        stop_price=Decimal("95"),
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )
    orphan_stop = StopOrderRecord(
        key="k1",
        stop_order_id="o1",
        position_id=99,
        ticker="GAZP",
        lots=1,
        stop_price=Decimal("90"),
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )

    async def _reconcile(moment: datetime) -> ReconciliationReport:
        env.append("reconcile")
        return ReconciliationReport(
            ran_at=moment,
            adjustments=(
                {"type": "STOP_MISSING", "position_id": 1, "ticker": "SBER"},
                {"type": "STOP_MISPRICED", "position_id": 1, "ticker": "SBER"},
                {
                    "type": "STOP_ADOPTABLE",
                    "position_id": 1,
                    "ticker": "SBER",
                    "stop_order_id": "s1",
                },
                {
                    "type": "STOP_ORPHAN",
                    "ticker": "GAZP",
                    "stop_order_id": "o1",
                    "key": "k1",
                },
                {"type": "STOP_MISSING", "position_id": 999, "ticker": "NONE"},
                {"type": "STOP_MISSING", "position_id": "1", "ticker": "SBER"},
                {"type": "STOP_ORPHAN", "ticker": "X", "stop_order_id": "missing"},
                {"type": "STOP_ADOPTABLE", "position_id": 1, "stop_order_id": "nope"},
            ),
        )

    async def _opened() -> list[Position]:
        return [position]

    async def _stops() -> list[StopOrderRecord]:
        return [adopt_stop, orphan_stop]

    async def _instrument(ticker: str) -> Instrument:
        return instrument

    async def _last_price(figi: str) -> Decimal:
        # Step 8a prices whatever `_instrument` returned, and this stub returns
        # the step 7 position's instrument for every ticker, whose figi is not
        # in PRICES. Step 8a is not this test's subject; giving it a readable
        # price keeps it out of the way. Under the narrowed catch of #201 the
        # fixture's `KeyError` would abort startup, which is correct — a
        # KeyError is a defect, not an unreadable instrument.
        return Decimal("100")

    async def _place(pos: Position, inst: Instrument) -> Position:
        env.append("place")
        return pos

    async def _replace(pos: Position, inst: Instrument) -> Position:
        env.append("replace")
        return pos

    async def _adopt(pos: Position, stop: StopOrderRecord) -> Position:
        env.append("adopt")
        return pos

    async def _cancel(stop: StopOrderRecord) -> None:
        env.append("cancel")

    monkeypatch.setattr("zarabot.app.startup.reconcile", _reconcile)
    monkeypatch.setattr("zarabot.app.startup.list_open", _opened)
    monkeypatch.setattr("zarabot.app.startup.list_stop_orders", _stops)
    monkeypatch.setattr("zarabot.app.startup.get_instrument", _instrument)
    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)
    monkeypatch.setattr("zarabot.app.startup.place_protective_stop", _place)
    monkeypatch.setattr("zarabot.app.startup.replace_stop", _replace)
    monkeypatch.setattr("zarabot.app.startup.adopt_existing_stop", _adopt)
    monkeypatch.setattr("zarabot.app.startup.cancel_orphaned_stop", _cancel)
    await start()
    assert "place" in env
    assert "replace" in env
    assert "adopt" in env
    assert "cancel" in env


async def test_ssl_verify_false_alerts_before_broker_call_without_token(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: running without certificate verification is told to the owner.

    A log line on a server nobody watches is not a security control, so the
    alert must precede the first broker call — and must never carry the token.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("SSL_TBANK_VERIFY", "false")
    alerts_before_broker: list[list[str]] = []

    async def _refresh(days: int) -> None:
        alerts_before_broker.append([item for item in env if item.startswith("alert:")])
        env.append("refresh")

    monkeypatch.setattr("zarabot.app.startup.refresh", _refresh)
    await start()

    assert alerts_before_broker, "market.session.refresh was never reached"
    warned = [
        text
        for text in alerts_before_broker[0]
        if "certificate" in text.lower() and "verif" in text.lower()
    ]
    assert warned, alerts_before_broker[0]
    assert os.environ["SSL_TBANK_VERIFY"] == "false"
    all_alerts = [item for item in env if item.startswith("alert:")]
    assert all(REQUIRED_ENV["TINVEST_TOKEN"] not in text for text in all_alerts)
    assert all(REQUIRED_ENV["TELEGRAM_BOT_TOKEN"] not in text for text in all_alerts)


async def test_ssl_verify_true_does_not_alert_about_certificates(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert is the exception, not a line every start emits."""
    from zarabot.app.startup import start

    monkeypatch.setenv("SSL_TBANK_VERIFY", "true")
    await start()

    assert not [
        item
        for item in env
        if item.startswith("alert:") and "certificate" in item.lower()
    ]


async def test_start_connects_before_apply_and_apply_receives_shared(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from zarabot.app.startup import start
    from zarabot.db.connection import connect as real_connect
    from zarabot.db.migrations import apply as real_apply

    order: list[str] = []
    applied_on: list[object] = []

    async def _connect(path: str) -> object:
        order.append("connect")
        return await real_connect(path)

    async def _apply(conn: object) -> int:
        order.append("apply")
        applied_on.append(conn)
        assert conn is shared()
        return await real_apply(conn)

    monkeypatch.setattr("zarabot.app.startup.connect", _connect, raising=False)
    monkeypatch.setattr("zarabot.app.startup.apply", _apply)
    await start()
    assert order[:2] == ["connect", "apply"]
    assert applied_on[0] is shared()


async def test_start_emits_the_startup_ok_log_event(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deploy health gate greps the container's logs for this event.

    `scripts/deploy/update.sh` waits for `startup_ok` and rolls the deploy back
    without it. It was specified in §7.1 from the first version and emitted by
    nothing (spec v1.58).
    """
    from zarabot.app.startup import start

    # `configure` strips every root handler, caplog's included, so the record
    # would be emitted into a logger nothing is listening to.
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)
    with caplog.at_level(logging.INFO, logger="zarabot.app.startup"):
        await start()
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_ok"
    ]
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.INFO
    assert isinstance(record.version, str) and record.version
    assert record.mode == "live"
    assert record.halted is False
    assert record.adjustments_count == 0


def test_importing_app_startup_opens_no_database_file(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["DB_PATH"] = str(tmp_path / "zarabot.db")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from zarabot.db.connection import DatabaseNotOpenError, shared\n"
            "import zarabot.app.startup  # noqa: F401\n"
            "raised = False\n"
            "try:\n"
            "    shared()\n"
            "except DatabaseNotOpenError:\n"
            "    raised = True\n"
            "assert raised\n"
            "from pathlib import Path\n"
            f"db = Path({str(tmp_path / 'zarabot.db')!r})\n"
            "assert not db.exists()\n"
            f"assert list(Path({str(tmp_path)!r}).glob('*.db')) == []\n",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def _position(
    ticker: str = "SBER",
    position_id: int = 1,
    stop_order_key: str | None = None,
) -> object:
    from decimal import Decimal

    from zarabot.models import Position, StopProtection

    return Position(
        id=position_id,
        ticker=ticker,
        figi="BBG000000001",
        strategy="ma_crossover",
        lots=1,
        lot_size=10,
        entry_price=Decimal("100"),
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=False,
        open_order_key=f"open-{position_id}",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.EXCHANGE,
        stop_order_key=stop_order_key,
    )


def _stop(
    key: str,
    stop_order_id: str | None,
    position_id: int = 1,
    ticker: str = "SBER",
) -> object:
    from decimal import Decimal

    from zarabot.models import StopOrderRecord, StopOrderStatus

    return StopOrderRecord(
        key=key,
        stop_order_id=stop_order_id,
        position_id=position_id,
        ticker=ticker,
        lots=1,
        stop_price=Decimal("95"),
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )


def _report_of(*adjustments: dict[str, object]) -> object:
    return ReconciliationReport(ran_at=NOW, adjustments=tuple(adjustments))


def _install_report(
    monkeypatch: pytest.MonkeyPatch, report: object, calls: list[str]
) -> None:
    async def _reconcile(moment: datetime) -> object:
        calls.append("reconcile")
        return report

    monkeypatch.setattr("zarabot.app.startup.reconcile", _reconcile)


async def test_stop_duplicate_cancels_every_identifier_and_retains_keep(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: every identifier in `cancel` is cancelled, `keep` retained.

    Two live stops against one position is the double-sell condition. It was
    detected, reported, and dropped by an executor with no branch for it (#35).
    """
    from zarabot.app.startup import start

    # Step 8a reads every watchlist instrument, so the watchlist is deliberately
    # not the duplicate's ticker: that is what keeps `_instrument` below able to
    # assert "no lookup for SBER" and mean step 7.
    monkeypatch.setenv("WATCHLIST", "GAZP")

    keep = _stop("keep-k", "keep-id")
    dup_one = _stop("dup-1", "dup-id-1")
    dup_two = _stop("dup-2", None)
    position = _position(stop_order_key="keep-k")
    cancelled: list[str] = []

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "STOP_DUPLICATE",
                "ticker": "SBER",
                "position_id": 1,
                "keep": "keep-id",
                "cancel": ["dup-id-1", "dup-2"],
            },
            {"type": "STOP_MISSING", "ticker": "GAZP", "position_id": 2},
        ),
        env,
    )

    async def _opened() -> list[object]:
        return [position]

    async def _stops() -> list[object]:
        return [keep, dup_one, dup_two]

    async def _cancel(stop: object) -> None:
        cancelled.append(stop.key)

    async def _instrument(ticker: str) -> object:
        # Step 8a legitimately reads every watchlist instrument, and the
        # watchlist is GAZP here precisely so this assertion still means what
        # it says: no lookup for SBER, the duplicate's ticker.
        if ticker == "SBER":
            raise AssertionError("no instrument lookup for a duplicate")
        return _instrument_of(ticker)

    async def _place(pos: object, inst: object) -> object:
        raise AssertionError("a duplicate is not remedied by a new stop")

    monkeypatch.setattr("zarabot.app.startup.list_open", _opened)
    monkeypatch.setattr("zarabot.app.startup.list_stop_orders", _stops)
    monkeypatch.setattr("zarabot.app.startup.cancel_orphaned_stop", _cancel)
    monkeypatch.setattr("zarabot.app.startup.get_instrument", _instrument)
    monkeypatch.setattr("zarabot.app.startup.place_protective_stop", _place)

    await start()

    assert sorted(cancelled) == ["dup-1", "dup-2"]
    assert keep.key not in cancelled


async def test_report_with_only_a_stop_duplicate_is_still_applied(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: the remedy gate must not skip a lone `STOP_DUPLICATE`."""
    from zarabot.app.startup import start

    keep = _stop("keep-k", "keep-id")
    dup = _stop("dup-1", "dup-id-1")
    cancelled: list[str] = []
    listed: list[str] = []

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "STOP_DUPLICATE",
                "ticker": "SBER",
                "position_id": 1,
                "keep": "keep-id",
                "cancel": ["dup-id-1"],
            }
        ),
        env,
    )

    async def _opened() -> list[object]:
        listed.append("list_open")
        return [_position(stop_order_key="keep-k")]

    async def _stops() -> list[object]:
        listed.append("list_stop_orders")
        return [keep, dup]

    async def _cancel(stop: object) -> None:
        cancelled.append(stop.key)

    monkeypatch.setattr("zarabot.app.startup.list_open", _opened)
    monkeypatch.setattr("zarabot.app.startup.list_stop_orders", _stops)
    monkeypatch.setattr("zarabot.app.startup.cancel_orphaned_stop", _cancel)

    await start()

    assert listed, "the remedy gate returned before positions were listed"
    assert cancelled == ["dup-1"]


class _FakeExchange:
    """The stop orders the broker actually holds.

    Every record is shaped the way `broker.client.list_stop_orders` builds one
    — `position_id` 0, `key` the `order_request_id` the bot sent, `lots` the
    `lots_requested` read back — because that is the shape that reaches
    `app.startup`. `post_stop_loss` returns what the real one returns, so the
    stop set below is the account's, not a record of which function was called.
    """

    def __init__(self) -> None:
        from zarabot.models import StopOrderRecord

        self.live: dict[str, StopOrderRecord] = {}
        self.placed: list[int] = []
        self.cancelled: list[str] = []
        self.reject = False

    def seed(self, stop_order_id: str, key: str, lots: int) -> None:
        from decimal import Decimal

        from zarabot.models import StopOrderRecord, StopOrderStatus

        self.live[stop_order_id] = StopOrderRecord(
            key=key,
            stop_order_id=stop_order_id,
            position_id=0,
            ticker="SBER",
            lots=lots,
            stop_price=Decimal("95"),
            status=StopOrderStatus.ACTIVE,
            created_at=NOW,
            settled_at=None,
        )

    async def list_stop_orders(self) -> list[object]:
        return list(self.live.values())

    async def cancel_stop_order(self, stop_order_id: str) -> None:
        self.cancelled.append(stop_order_id)
        self.live.pop(stop_order_id, None)

    async def post_stop_loss(
        self, key: str, figi: str, lots: int, stop_price: Decimal
    ) -> object:
        from zarabot.broker.client import StopOrderRejected
        from zarabot.models import StopOrderRecord, StopOrderStatus

        self.placed.append(lots)
        if self.reject:
            raise StopOrderRejected("rejected")
        stop_order_id = f"ex-new-{len(self.placed)}"
        record = StopOrderRecord(
            key=key,
            stop_order_id=stop_order_id,
            position_id=0,
            ticker="SBER",
            lots=lots,
            stop_price=stop_price,
            status=StopOrderStatus.ACTIVE,
            created_at=NOW,
            settled_at=None,
        )
        self.live[stop_order_id] = record
        return record


async def _seed_half_protected(path: Path) -> int:
    """A position at the corrected 4 lots whose live stop still covers 2.

    This is the database #233 leaves behind: reconciliation grew the row to the
    broker's count, and the standing stop was posted for the count the row held
    when it was placed.
    """
    from decimal import Decimal

    from zarabot.db.connection import transaction
    from zarabot.db.positions import open as open_position
    from zarabot.db.positions import set_stop_protection, update_lots
    from zarabot.db.stop_orders import activate, record_placing
    from zarabot.models import OrderRecord, OrderStatus, Side, Signal, StopProtection

    await connect(str(path))
    try:
        await apply(shared())
        async with transaction() as conn:
            await conn.execute(
                """
                INSERT INTO orders (
                    key, ticker, figi, side, intent, lots, status,
                    filled_lots, filled_price, created_at, settled_at
                ) VALUES (
                    'entry-1', 'SBER', 'FIGI-SBER', 'BUY', 'ENTRY', 2,
                    'FILLED', 2, '100.00', ?, ?
                )
                """,
                (NOW.isoformat(), NOW.isoformat()),
            )
        signal = Signal(
            ticker="SBER",
            strategy="ma_crossover",
            side=Side.BUY,
            generated_at=NOW,
            reference_price=Decimal("100"),
        )
        order = OrderRecord(
            key="entry-1",
            ticker="SBER",
            figi="FIGI-SBER",
            side=Side.BUY,
            intent="ENTRY",
            lots=2,
            status=OrderStatus.FILLED,
            filled_lots=2,
            filled_price=Decimal("100"),
            commission=None,
            broker_reason=None,
            created_at=NOW,
            settled_at=NOW,
        )
        position = await open_position(
            signal,
            order,
            _instrument_of("SBER"),
            Decimal("95"),
            Decimal("110"),
            NOW,
        )
        await update_lots(position.id, 4)
        await record_placing("old-stop", position.id, "SBER", 2, Decimal("95"))
        await activate("old-stop", "ex-old")
        await set_stop_protection(position.id, StopProtection.EXCHANGE, "old-stop")
        return position.id
    finally:
        await disconnect()


def _install_exchange(
    monkeypatch: pytest.MonkeyPatch, exchange: _FakeExchange, calls: list[str]
) -> None:
    """Wire the fake account in at `broker.client`, the mocking seam.

    `execution.orders` is deliberately NOT stubbed: the remedy has to run for
    real, because the assertion is the resulting live stop set.
    """

    async def _orders_alert(text: str, urgent: bool = False) -> None:
        calls.append(f"alert:{text}")

    monkeypatch.setattr(
        "zarabot.app.startup.list_stop_orders", exchange.list_stop_orders
    )
    monkeypatch.setattr(
        "zarabot.execution.orders.cancel_stop_order", exchange.cancel_stop_order
    )
    monkeypatch.setattr(
        "zarabot.execution.orders.post_stop_loss", exchange.post_stop_loss
    )
    monkeypatch.setattr("zarabot.execution.orders.alert", _orders_alert)


async def test_stop_missized_leaves_one_live_stop_covering_the_position(
    env: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2 (v1.90, #233): the remedy restores coverage for the true size.

    The assertions are the live stop set and its lot count, never that
    `replace_stop` was called: an implementation that cancelled and placed
    nothing, or placed a second stop beside the first, fails here rather than
    on a call record.
    """
    from zarabot.app.startup import start
    from zarabot.db.positions import get as get_position

    position_id = await _seed_half_protected(tmp_path / "zarabot.db")
    exchange = _FakeExchange()
    exchange.seed("ex-old", "old-stop", 2)
    _install_exchange(monkeypatch, exchange, env)
    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "STOP_MISSIZED",
                "ticker": "SBER",
                "position_id": position_id,
                "position_lots": 4,
                "stop_lots": 2,
            }
        ),
        env,
    )

    await start()

    live = list(exchange.live.values())
    assert len(live) == 1, live
    assert live[0].lots == 4
    assert "ex-old" not in exchange.live
    assert exchange.cancelled == ["ex-old"]
    stored = await get_position(position_id)
    assert stored is not None
    assert stored.lots == 4


async def test_report_with_only_a_stop_missized_is_still_applied(
    env: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: the remedy gate must not skip a lone `STOP_MISSIZED`.

    That gate is what dropped `STOP_DUPLICATE` in #35.
    """
    from zarabot.app.startup import start

    position_id = await _seed_half_protected(tmp_path / "zarabot.db")
    exchange = _FakeExchange()
    exchange.seed("ex-old", "old-stop", 2)
    _install_exchange(monkeypatch, exchange, env)
    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "STOP_MISSIZED",
                "ticker": "SBER",
                "position_id": position_id,
                "position_lots": 4,
                "stop_lots": 2,
            }
        ),
        env,
    )

    await start()

    assert exchange.placed == [4]
    assert [stop.lots for stop in exchange.live.values()] == [4]


async def test_lots_adjusted_beside_stop_missized_replaces_the_stop_once(
    env: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: the two types are not both wired to a remedy.

    `LOTS_ADJUSTED` owns the row correction and `STOP_MISSIZED` owns the
    protection. A second replacement would cancel the stop the first just
    placed.
    """
    from zarabot.app.startup import start

    position_id = await _seed_half_protected(tmp_path / "zarabot.db")
    exchange = _FakeExchange()
    exchange.seed("ex-old", "old-stop", 2)
    _install_exchange(monkeypatch, exchange, env)
    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "LOTS_ADJUSTED",
                "ticker": "SBER",
                "position_id": position_id,
                "from": 2,
                "to": 4,
            },
            {
                "type": "STOP_MISSIZED",
                "ticker": "SBER",
                "position_id": position_id,
                "position_lots": 4,
                "stop_lots": 2,
            },
        ),
        env,
    )

    await start()

    assert exchange.placed == [4]
    assert exchange.cancelled == ["ex-old"]
    assert len(exchange.live) == 1


async def test_lots_adjusted_alone_is_recognised_and_does_not_stop_startup(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: it is still a known type after leaving the self-resolving group.

    The group it sat in is what #233 was; removing it from every group would
    turn the loud path into a false alarm on every lot correction.
    """
    from zarabot.app.startup import start

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "LOTS_ADJUSTED",
                "ticker": "SBER",
                "position_id": 1,
                "from": 2,
                "to": 4,
            }
        ),
        env,
    )

    context = await start()

    assert context is not None
    alerts = [item for item in env if item.startswith("alert:")]
    assert not [text for text in alerts if "cannot act on" in text], alerts


async def test_missized_replacement_rejected_three_times_degrades_to_local(
    env: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec §3.2 (v1.90, #233): rule 23's degrade, not a halt.

    Before the remedy: the row and the broker agree at 4, the exchange stop
    covers 2, `stop_protection` reads `EXCHANGE`, and `lifecycle.exits` fires
    nothing — nothing is watching two of the four lots, with no alert standing.
    After a failed replacement: no exchange stop, protection `LOCAL`, the bot
    watching the whole holding at the corrected size, and an alert sent.

    The last claim is asserted rather than described: the real, pure
    `lifecycle.exits.evaluate` is called on the position the database now
    holds.
    """
    from datetime import date
    from decimal import Decimal

    from zarabot.app.startup import AppContext, start
    from zarabot.db.positions import get as get_position
    from zarabot.lifecycle.exits import evaluate
    from zarabot.models import ExitTrigger, SessionInfo, StopProtection

    position_id = await _seed_half_protected(tmp_path / "zarabot.db")
    exchange = _FakeExchange()
    exchange.seed("ex-old", "old-stop", 2)
    exchange.reject = True
    _install_exchange(monkeypatch, exchange, env)
    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "STOP_MISSIZED",
                "ticker": "SBER",
                "position_id": position_id,
                "position_lots": 4,
                "stop_lots": 2,
            }
        ),
        env,
    )

    # `configure` strips every root handler, caplog's included, so the record
    # would be emitted into a logger nothing is listening to.
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)
    with caplog.at_level(logging.ERROR, logger="zarabot.execution.orders"):
        context = await start()

    assert isinstance(context, AppContext)
    assert exchange.live == {}
    stored = await get_position(position_id)
    assert stored is not None
    assert stored.status == "OPEN"
    assert stored.lots == 4
    assert stored.stop_protection is StopProtection.LOCAL
    assert stored.stop_order_key is None
    alerts = [item for item in env if item.startswith("alert:")]
    assert [text for text in alerts if "unplaceable" in text], alerts
    assert _events_in(caplog, "stop_protection_degraded")

    session = SessionInfo(
        trade_date=date(2026, 3, 16),
        start=datetime(2026, 3, 16, 6, 50, tzinfo=UTC),
        end=datetime(2026, 3, 16, 15, 50, tzinfo=UTC),
        is_trading_day=True,
    )
    assert (
        evaluate(
            stored,
            stored.stop_price - Decimal("1"),
            NOW,
            session,
            None,
            context.config,
        )
        is ExitTrigger.STOP_LOSS
    )


async def test_unrecognised_adjustment_type_alerts(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.2: a report the executor cannot act on must be loud."""
    from zarabot.app.startup import start

    _install_report(
        monkeypatch,
        _report_of({"type": "STOP_TELEPORTED", "ticker": "SBER", "position_id": 1}),
        env,
    )

    async def _opened() -> list[object]:
        return []

    async def _stops() -> list[object]:
        return []

    monkeypatch.setattr("zarabot.app.startup.list_open", _opened)
    monkeypatch.setattr("zarabot.app.startup.list_stop_orders", _stops)

    await start()

    alerts = [item for item in env if item.startswith("alert:")]
    assert [text for text in alerts if "STOP_TELEPORTED" in text], alerts


async def test_known_non_stop_adjustments_do_not_alert_as_unrecognised(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loud path is for types the executor does not know, not every type."""
    from zarabot.app.startup import start

    _install_report(
        monkeypatch,
        _report_of(
            {"type": "CLOSED_EXTERNALLY", "ticker": "SBER", "position_id": 1},
            {"type": "ADOPTED", "ticker": "GAZP", "position_id": 2},
            {"type": "LOTS_ADJUSTED", "ticker": "LKOH", "position_id": 3},
        ),
        env,
    )

    await start()

    alerts = [item for item in env if item.startswith("alert:")]
    assert not [text for text in alerts if "does not" in text.lower()], alerts


async def test_exit_unresolved_is_observed_and_does_not_stop_startup(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shares are already gone; there is no remedy to apply, and the loud
    path stays reserved for a report the executor does not understand."""
    from zarabot.app.startup import start

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "EXIT_UNRESOLVED",
                "ticker": "SBER",
                "position_id": 1,
                "reason": "no executed sale for this instrument in the window",
            }
        ),
        env,
    )

    await start()

    alerts = [item for item in env if item.startswith("alert:")]
    assert not [text for text in alerts if "cannot act on" in text], alerts


async def test_foreign_holding_refuses_to_start_naming_every_ticker(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 32: the account is the bot's alone (brief v1.8). Refuse to start."""
    from zarabot.app.startup import StartupError, start

    entries: list[str] = []

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "FOREIGN_HOLDING",
                "ticker": "LKOH",
                "lots": 3,
                "average_price": "6200.5",
            },
            {
                "type": "FOREIGN_HOLDING",
                "ticker": "GMKN",
                "lots": 1,
                "average_price": "140.0",
            },
        ),
        env,
    )

    async def _open(*args: object, **kwargs: object) -> None:
        entries.append("open_position")

    monkeypatch.setattr("zarabot.execution.orders.open_position", _open)

    with pytest.raises(StartupError) as excinfo:
        await start()

    message = str(excinfo.value)
    assert "LKOH" in message
    assert "GMKN" in message
    assert entries == []
    alerts = [item for item in env if item.startswith("alert:")]
    assert [text for text in alerts if "LKOH" in text and "GMKN" in text], alerts
    assert not [text for text in alerts if "running" in text.lower()]
    assert all(REQUIRED_ENV["TINVEST_TOKEN"] not in text for text in alerts)


async def test_foreign_holding_emits_startup_failed_reconcile(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: a FOREIGN_HOLDING refusal is startup_failed, not a silent abort."""
    from zarabot.app.startup import StartupError, start

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "FOREIGN_HOLDING",
                "ticker": "LKOH",
                "lots": 3,
                "average_price": "6200.5",
            }
        ),
        env,
    )
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)
    with (
        caplog.at_level(logging.CRITICAL, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_failed"
    ]
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.CRITICAL
    assert record.stage == "reconcile"
    assert record.reason
    assert not any(
        getattr(item, "event", None) == "startup_ok" for item in caplog.records
    )
    assert "tinvest-secret-token" not in caplog.text


async def test_allow_foreign_holdings_starts_observe_only(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 32: acknowledged holdings are named in the ready alert, not traded."""
    from zarabot.app.startup import start

    monkeypatch.setenv("ALLOW_FOREIGN_HOLDINGS", "true")
    traded: list[str] = []

    _install_report(
        monkeypatch,
        _report_of(
            {
                "type": "FOREIGN_HOLDING",
                "ticker": "LKOH",
                "lots": 3,
                "average_price": "6200.5",
            }
        ),
        env,
    )

    async def _place(pos: object, inst: object) -> object:
        traded.append("place_protective_stop")
        return pos

    async def _close(*args: object, **kwargs: object) -> None:
        traded.append("close_position")

    monkeypatch.setattr("zarabot.app.startup.place_protective_stop", _place)
    monkeypatch.setattr("zarabot.execution.orders.close_position", _close)

    ctx = await start()

    assert ctx.config.allow_foreign_holdings is True
    ready = [item for item in env if item.startswith("alert:") and "running" in item]
    assert ready, [item for item in env if item.startswith("alert:")]
    assert "LKOH" in ready[0]
    assert traded == []


# --- Step 8a: a position budget that cannot buy one lot (v1.46, rule 36) ------
#
# On 2026-08-28 the bot produced 307 signals and rejected all 307 as ZERO_LOTS,
# every one of them MGNT against a 1000 RUB budget, and said nothing about it
# for eleven days. risk.gate was right to reject; nothing was there to tell the
# owner their budget could not reach the instrument that kept signalling.


async def test_budget_below_every_lot_cost_alerts_and_still_completes(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blackout case — and, the half that matters, startup still finishes.

    Refusing to start would abandon every open position: no exit evaluation, no
    stop management, no MAX_AGE. An unaffordable budget stops new entries only,
    so the failure direction that protects money is to keep running and say so.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "MGNT,LKOH")
    monkeypatch.setenv("ALLOCATED_CAPITAL", "10000")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")

    ctx = await start()

    assert ctx.config.watchlist == ("MGNT", "LKOH")
    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    # "mode=" is the ready alert's own marker; anything else mentioning MGNT
    # is the blackout message.
    blackout = [text for text in alerts if "MGNT" in text and "mode=" not in text]
    assert blackout, alerts
    # The budget and the cheapest lot cost, so the owner can see the gap
    # without going to look it up: 1000 against MGNT's 1632.
    assert "1000" in blackout[0]
    assert "1632" in blackout[0]
    assert [text for text in alerts if "running" in text.lower()], alerts


async def test_partly_affordable_watchlist_names_names_but_does_not_escalate(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A price rising through the budget is normal, not a fault.

    Escalating it would put a recurring alert in a channel whose whole premise
    is that silence means healthy, which is how an owner is trained to ignore
    the one message that matters.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "SBER,MGNT")
    monkeypatch.setenv("ALLOCATED_CAPITAL", "10000")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")

    await start()

    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    ready = [text for text in alerts if "running" in text.lower()]
    assert ready, alerts
    assert "MGNT" in ready[0]
    assert "SBER" not in ready[0]
    # Nothing but the ready alert may mention it.
    assert [text for text in alerts if "MGNT" in text] == ready


async def test_unreadable_price_is_counted_as_neither_answer(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing data is not evidence of affordability, nor of a blackout."""
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "SBER,MGNT")
    monkeypatch.setenv("ALLOCATED_CAPITAL", "10000")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")

    async def _last_price(figi: str) -> Decimal:
        if figi == "FIGI-MGNT":
            raise PriceRejected("no price")
        return PRICES[figi.removeprefix("FIGI-")]

    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)

    await start()

    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    ready = [text for text in alerts if "running" in text.lower()]
    assert ready, alerts
    # Named, and named as unknown rather than folded into either count.
    assert "MGNT" in ready[0]
    assert "unknown" in ready[0].lower()
    assert "unaffordable=MGNT" not in ready[0]
    assert not [text for text in alerts if "mode=" not in text and "MGNT" in text]


async def test_no_readable_price_reports_inconclusive_not_a_blackout(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker outage must not manufacture the finding the check exists for.

    Failure register rule 5: a defensive fallback that turns a loud failure into
    a plausible-sounding quiet one. Reporting "nothing is affordable" off zero
    observations would be exactly that, and the owner would act on it.

    Both directions, because a test that asserted only the first would pass
    just as well against `except Exception` and would pin nothing (failure
    class 3): a *broker* failure is excluded and reported inconclusive, and a
    *programming* error in the same read is not — it reaches the rule 15
    boundary, which alerts, emits `startup_failed` and refuses to start.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "SBER,MGNT")

    async def _last_price(figi: str) -> Decimal:
        raise BrokerUnavailable("broker unreachable")

    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)

    await start()

    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    ready = [text for text in alerts if "running" in text.lower()]
    assert ready, alerts
    assert "inconclusive" in ready[0].lower()
    # Neither silent nor crying blackout.
    assert "cannot open a position" not in " ".join(alerts).lower()


async def test_programming_error_reading_a_price_reaches_the_rule_15_boundary(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other direction of failure class 5, for the price read.

    A renamed SDK field surfaces as `AttributeError`. Excluding the ticker and
    calling it "unknown" would report a plausible degraded state off a defect,
    in the module whose whole job is to establish the system is sound. It must
    propagate to the boundary instead, which names the exception type.
    """
    from zarabot.app.startup import StartupError, start

    monkeypatch.setenv("WATCHLIST", "SBER,MGNT")

    async def _last_price(figi: str) -> Decimal:
        raise AttributeError("'LastPrice' object has no attribute 'price'")

    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    with (
        caplog.at_level(logging.CRITICAL, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_failed"
    ]
    assert len(events) == 1
    assert events[0].stage == "reachability"
    assert events[0].reason == "AttributeError"
    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    assert not [text for text in alerts if "running" in text.lower()], alerts


async def test_broker_failure_in_step_8a_does_not_raise_startup_error(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diagnostic must never be the reason the bot is not running.

    Both directions, for the instrument read. Every class rule 9 enumerates for
    `get_instrument` skips its ticker and lets startup finish; the companion
    test below proves the catch does not also swallow a defect.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "SBER,GAZP,MGNT")
    failures = {
        "SBER": BrokerUnavailable("broker unreachable"),
        "GAZP": BrokerRateLimited("slow down"),
        "MGNT": InstrumentNotFound("no such ticker"),
    }

    async def _instrument(ticker: str) -> Instrument:
        raise failures[ticker]

    monkeypatch.setattr("zarabot.app.startup.get_instrument", _instrument)

    ctx = await start()

    assert ctx.config.trading_mode == "live"
    assert [item for item in env if item.startswith("alert:") and "running" in item]


async def test_programming_error_reading_an_instrument_reaches_the_boundary(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The companion direction: a defect is not a broker failure.

    `except Exception` would pass the test above and fail this one, which is
    the point of writing the pair.
    """
    from zarabot.app.startup import StartupError, start

    async def _instrument(ticker: str) -> Instrument:
        raise AttributeError("'Share' object has no attribute 'lot'")

    monkeypatch.setattr("zarabot.app.startup.get_instrument", _instrument)
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    with (
        caplog.at_level(logging.CRITICAL, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_failed"
    ]
    assert len(events) == 1
    assert events[0].stage == "reachability"
    assert events[0].reason == "AttributeError"
    assert "tinvest-secret-token" not in caplog.text


async def test_zero_price_is_unknown_rather_than_affordable(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lot costing nothing is a bad read, not a bargain.

    Written after the implementation, when coverage showed the branch bare —
    and it earns its place: without it a zero price satisfies `lot_cost <=
    budget` and the ticker is counted affordable, which is the check reporting
    health off a broken read. The other direction of the same rule the
    inconclusive case exists for.
    """
    from zarabot.app.startup import start

    monkeypatch.setenv("WATCHLIST", "SBER,MGNT")
    monkeypatch.setenv("ALLOCATED_CAPITAL", "10000")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")

    async def _last_price(figi: str) -> Decimal:
        if figi == "FIGI-SBER":
            return Decimal("0")
        return PRICES[figi.removeprefix("FIGI-")]

    monkeypatch.setattr("zarabot.app.startup.get_last_price", _last_price)

    await start()

    alerts = [item.removeprefix("alert:") for item in env if item.startswith("alert:")]
    ready = [text for text in alerts if "mode=" in text]
    assert ready, alerts
    assert "unknown=SBER" in ready[0]
    # MGNT is the only observation, and it is unaffordable — but with one real
    # observation the check is not inconclusive, and SBER is not affordable.
    assert "unaffordable=MGNT" in ready[0]
    assert "inconclusive" not in ready[0]


async def test_startup_logs_the_observed_time_in_utc_and_msk(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 29, owned by this module: the cheapest possible clock-skew detector.

    There is no runtime skew check by design, so the only symptom of a host
    whose clock or zone database has drifted is this line. It must carry both
    zones — UTC alone cannot show a wrong `Europe/Moscow` offset, and MSK alone
    cannot show a wrong instant — and the instant comes from `clock.now()`,
    which this module may not bypass with `datetime.now()`.
    """
    from zarabot.app.startup import start

    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    with caplog.at_level(logging.INFO, logger="zarabot.app.startup"):
        await start()

    records = [
        record for record in caplog.records if getattr(record, "utc", None) is not None
    ]
    assert len(records) == 1, caplog.text
    record = records[0]
    # NOW is 12:00 UTC on 2026-03-16, which is 15:00 in Moscow.
    assert record.utc == "2026-03-16T12:00:00+00:00"
    assert record.msk == "2026-03-16T15:00:00+03:00"
    # First line after the restart: nothing else from this module precedes it.
    mine = [item for item in caplog.records if item.name == "zarabot.app.startup"]
    assert mine[0] is record
    # Rule 19: no token and no account identifier, in the message or the fields.
    assert "tinvest-secret-token" not in caplog.text
    assert REQUIRED_ENV["TINVEST_ACCOUNT_ID"] not in caplog.text


async def test_observed_time_comes_from_clock_not_datetime_now(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`clock` is the sole owner of "now", so a stubbed clock moves this line.

    Without this the previous test would pass against a `datetime.now(UTC)`
    call on a host that happened to be at NOW, which is never.
    """
    from zarabot.app.startup import start

    other = datetime(2026, 12, 31, 21, 30, tzinfo=UTC)
    monkeypatch.setattr("zarabot.app.startup.now", lambda: other)
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)

    with caplog.at_level(logging.INFO, logger="zarabot.app.startup"):
        await start()

    records = [
        record for record in caplog.records if getattr(record, "utc", None) is not None
    ]
    assert len(records) == 1, caplog.text
    assert records[0].utc == "2026-12-31T21:30:00+00:00"
    # 21:30 UTC on the 31st is 00:30 on 1 January in Moscow — the date rolls,
    # which is the case a single-zone log line cannot show at all.
    assert records[0].msk == "2027-01-01T00:30:00+03:00"


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class _FakeUpdate:
    """The shape `telegram.commands` reads off a PTB update: chat id and text."""

    def __init__(self, chat_id: int, command: str) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage(f"/{command}")


async def test_start_leaves_report_answering_rather_than_unavailable(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `start()`, `/report` produces a report, not `report unavailable`.

    `telegram.commands` holds the `/report` builder in a module global that
    only `app.startup` fills, and nothing pinned that call: delete it and
    `/report` answers "report unavailable" for the life of the process while
    every other case here stays green (#36). This asserts the outcome the
    owner sees, through the real command handler and the real
    `reporter.weekly.build`, so it fails on a deleted call, on a builder wired
    to the wrong callable, and on a builder installed after the ready alert
    the loops start behind.
    """
    from zarabot.app.startup import start
    from zarabot.config import get
    from zarabot.models import Candle
    from zarabot.telegram.commands import report, set_report_builder

    # The builder is process-global, so a builder another test installed would
    # make this pass with the wiring deleted. Start from unavailable.
    get.cache_clear()
    set_report_builder(None)
    monkeypatch.setattr("zarabot.telegram.commands.now", lambda: NOW)

    async def _pnl_instrument(ticker: str) -> Instrument:
        return _instrument_of(ticker)

    async def _pnl_candles(
        figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        return [
            Candle(
                timestamp=since,
                open=Decimal("279.83"),
                high=Decimal("280.00"),
                low=Decimal("279.00"),
                close=Decimal("279.83"),
                volume=1000,
            ),
            Candle(
                timestamp=until,
                open=Decimal("280.00"),
                high=Decimal("286.00"),
                low=Decimal("279.50"),
                close=Decimal("285.00"),
                volume=1200,
            ),
        ]

    # The benchmark leg of the report reaches the broker through `pnl`; mock at
    # `broker.client`, which is where these names come from.
    monkeypatch.setattr("zarabot.pnl.get_instrument", _pnl_instrument)
    monkeypatch.setattr("zarabot.pnl.get_candles", _pnl_candles)

    update = _FakeUpdate(int(REQUIRED_ENV["TELEGRAM_CHAT_ID"]), "report")
    try:
        await start()
        await report(update, None)
    finally:
        set_report_builder(None)
        get.cache_clear()

    assert update.message.replies, "the /report handler replied nothing"
    text = update.message.replies[-1]
    assert text != "report unavailable"
    # NOW is Monday 2026-03-16 in Moscow, so the week runs to Sunday the 22nd.
    assert text.startswith("Weekly report 2026-03-16 to 2026-03-22")
    assert "Win rate:" in text


# --- step 2b/2c: the single-instance lock (spec v1.89, #22) ------------------
#
# Every case below takes the real kernel lock and calls the real `start()`.
# `flock` locks belong to the open file description, so a second acquisition
# inside this one process contends exactly as a second process would: no
# subprocess, no `sleep`, no timing window. That is why the contract chose
# `flock` over `fcntl.lockf`, whose locks belong to the process and would be
# granted twice here — a guard that cannot be made to fire reads as protection
# while being none.

_MARKER_AGE_OUT = timedelta(hours=24)


def _instance_paths() -> tuple[Path, Path, Path]:
    """The database and the two artefacts §10 says sit beside it."""
    db_path = Path(os.environ["DB_PATH"])
    return (
        db_path,
        Path(str(db_path) + ".instance-lock"),
        Path(str(db_path) + ".instance-lock.refused"),
    )


@contextmanager
def _lock_held_by_another_instance(lock_path: Path) -> Iterator[int]:
    """Hold the real lock the way a first instance holds it."""
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        with suppress(OSError):
            os.close(fd)


def _alerts_in(calls: list[str]) -> list[str]:
    return [item for item in calls if item.startswith("alert:")]


def _events_in(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if getattr(record, "event", None) == name
    ]


async def test_start_refuses_while_the_instance_lock_is_held(
    env: list[str],
) -> None:
    """A second bot against one DB_PATH refuses: no database, no order.

    The `asyncio.Lock`s in `execution.orders` serialise coroutines in one event
    loop and cannot see another process at all, so without step 2b both
    instances pass `_already_open`, both `record_submitting` with different
    UUIDs and both `post_market_order`: two real market buys. The precondition
    here is a kernel lock this test holds, so nothing about it can be satisfied
    by wiring, and the assertions are on `start()`'s outcome and on the
    database never having been opened.
    """
    from zarabot.app.startup import StartupError, start
    from zarabot.db.connection import DatabaseNotOpenError

    db_path, lock_path, _ = _instance_paths()
    with (
        _lock_held_by_another_instance(lock_path),
        pytest.raises(StartupError) as excinfo,
    ):
        await start()
    assert str(lock_path) in str(excinfo.value)
    # Step 3 never ran: `connect` creates the file, and it is not there.
    assert not db_path.exists()
    with pytest.raises(DatabaseNotOpenError):
        shared()
    # Nothing past step 2b ran either, so no order could have been placed.
    assert "refresh" not in env
    assert "resolve" not in env
    assert "reconcile" not in env


async def test_closing_the_descriptor_lets_the_next_start_proceed(
    env: list[str],
) -> None:
    """A crashed instance leaves no lock behind.

    Closing the descriptor is what the kernel does when a process ends by any
    means — `SIGKILL`, an OOM kill, `docker kill`, power loss — so this is the
    crash-release path, not a convenience. Refusing to start is strictly worse
    than the duplicate it prevents when there is nothing left to duplicate.
    """
    from zarabot.app.startup import StartupError, start

    db_path, lock_path, _ = _instance_paths()
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(StartupError):
        await start()
    os.close(fd)
    ctx = await start()
    assert ctx.config.trading_mode == "live"
    assert db_path.exists()


async def test_first_refusal_alerts_writes_the_marker_and_emits_startup_failed(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner is told the first time, and so is a reader without Telegram."""
    from zarabot.app.startup import StartupError, start

    # `configure` strips every root handler, caplog's included.
    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)
    _, lock_path, marker = _instance_paths()
    assert not marker.exists()
    with (
        _lock_held_by_another_instance(lock_path),
        caplog.at_level(logging.WARNING, logger="zarabot.app.startup"),
        pytest.raises(StartupError),
    ):
        await start()
    assert len(_alerts_in(env)) == 1
    assert str(lock_path) in _alerts_in(env)[0]
    assert datetime.fromisoformat(marker.read_text().strip()) == NOW
    events = _events_in(caplog, "startup_failed")
    assert len(events) == 1
    assert events[0].levelno == logging.CRITICAL
    assert events[0].stage == "instance"
    assert events[0].reason == "INSTANCE_LOCKED"
    assert not _events_in(caplog, "startup_ok")
    # Rule 19: the refusal names a path, never a credential.
    assert REQUIRED_ENV["TINVEST_TOKEN"] not in caplog.text
    assert REQUIRED_ENV["TINVEST_TOKEN"] not in "".join(env)


async def test_repeat_refusal_is_quiet_and_leaves_the_marker_unchanged(
    env: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The second refusal must not rewrite the marker.

    The unchanged instant is the point of this case, not a detail of it. The
    ageing window is measured from the *first* refusal of a condition;
    refreshing it on every retry would push it forward every 30 seconds under
    exactly the `restart: unless-stopped` loop the rule exists for, making the
    age-out unreachable and turning "alert once per condition" into "alert
    once, ever". The clock advances between the two refusals, so an
    implementation that rewrote the marker would write different bytes and
    fail here — while a case that only counted alerts would pass it.
    """
    from zarabot.app.startup import StartupError, start

    monkeypatch.setattr("zarabot.app.startup.configure", lambda level, secrets: None)
    moment = [NOW]
    monkeypatch.setattr("zarabot.app.startup.now", lambda: moment[0])
    _, lock_path, marker = _instance_paths()
    with (
        _lock_held_by_another_instance(lock_path),
        caplog.at_level(logging.WARNING, logger="zarabot.app.startup"),
    ):
        with pytest.raises(StartupError):
            await start()
        first = marker.read_bytes()
        # The container is restarted 30 seconds later, and again after that.
        moment[0] = NOW + timedelta(minutes=5)
        with pytest.raises(StartupError):
            await start()
        second = marker.read_bytes()
    assert second == first
    assert datetime.fromisoformat(first.decode().strip()) == NOW
    # One alert for the condition, two failures for the health gate.
    assert len(_alerts_in(env)) == 1
    assert len(_events_in(caplog, "startup_failed")) == 2
    assert not _events_in(caplog, "startup_ok")
    # The suppression is visible to an operator reading the log, and names the
    # instant the window is measured from.
    suppressed = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and NOW.isoformat() in record.getMessage()
    ]
    assert suppressed


async def test_refusal_with_an_aged_out_marker_alerts_again_and_rewrites_it(
    env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file latch ages out at 24 hours, which is the bound on the risk.

    Reset (1) — a successful acquisition — does not fire while the holder keeps
    running, so without this a genuine second incident weeks later would be
    silent forever. The clock is fixed by the test: no `sleep`, no wall clock.
    """
    from zarabot.app.startup import StartupError, start

    _, lock_path, marker = _instance_paths()
    stale = NOW - _MARKER_AGE_OUT - timedelta(minutes=1)
    marker.write_text(stale.isoformat(), encoding="utf-8")
    with (
        _lock_held_by_another_instance(lock_path),
        pytest.raises(StartupError),
    ):
        await start()
    assert len(_alerts_in(env)) == 1
    assert datetime.fromisoformat(marker.read_text().strip()) == NOW


async def test_successful_start_deletes_the_refusal_marker(
    env: list[str],
) -> None:
    """The primary reset: holding the lock proves there is only one instance.

    Every normal recovery passes through this path — the operator kills the
    stray process, the container is replaced, the host reboots — so the latch
    is cleared by the very event that ends the condition.
    """
    from zarabot.app.startup import start

    _, _lock, marker = _instance_paths()
    marker.write_text((NOW - timedelta(minutes=1)).isoformat(), encoding="utf-8")
    await start()
    assert not marker.exists()


# The three cases below have no §3.2 bullet of their own. They pin sentences of
# step 2c that would otherwise be satisfied vacuously — "unreadable and
# malformed fail *loud*: a corrupt marker must never be able to silence the
# channel, and a clock that moved backwards must not either", and "the marker
# cannot be written … alert anyway". A marker that could silence the channel by
# being corrupt is the one failure mode of an anti-spam device.


async def test_a_malformed_marker_is_treated_as_absent_and_alerts(
    env: list[str],
) -> None:
    from zarabot.app.startup import StartupError, start

    _, lock_path, marker = _instance_paths()
    marker.write_text("this is not an instant", encoding="utf-8")
    with (
        _lock_held_by_another_instance(lock_path),
        pytest.raises(StartupError),
    ):
        await start()
    assert len(_alerts_in(env)) == 1
    assert datetime.fromisoformat(marker.read_text().strip()) == NOW


async def test_a_marker_dated_in_the_future_is_treated_as_absent_and_alerts(
    env: list[str],
) -> None:
    """A clock that moved backwards must not silence the channel either."""
    from zarabot.app.startup import StartupError, start

    _, lock_path, marker = _instance_paths()
    marker.write_text((NOW + timedelta(hours=1)).isoformat(), encoding="utf-8")
    with (
        _lock_held_by_another_instance(lock_path),
        pytest.raises(StartupError),
    ):
        await start()
    assert len(_alerts_in(env)) == 1
    assert datetime.fromisoformat(marker.read_text().strip()) == NOW


async def test_an_unwritable_marker_still_alerts_and_still_refuses(
    env: list[str],
) -> None:
    """The marker is an anti-spam device, never a precondition for the refusal."""
    from zarabot.app.startup import StartupError, start

    _, lock_path, marker = _instance_paths()
    # A directory at the marker's path: every read and write of it raises
    # `IsADirectoryError`, which is what a permission failure or a full disk
    # looks like from here.
    marker.mkdir()
    with (
        _lock_held_by_another_instance(lock_path),
        pytest.raises(StartupError),
    ):
        await start()
    assert len(_alerts_in(env)) == 1
    assert marker.is_dir()
