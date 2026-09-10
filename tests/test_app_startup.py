"""Tests for zarabot.app.startup — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
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
