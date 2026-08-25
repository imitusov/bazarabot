"""Tests for zarabot.app.startup — written from technical-spec.md §3.2."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zarabot.db.connection import connect, disconnect, shared
from zarabot.db.migrations import apply
from zarabot.models import HaltReason, ReconciliationReport
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
        alerts_before_broker.append(
            [item for item in env if item.startswith("alert:")]
        )
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
