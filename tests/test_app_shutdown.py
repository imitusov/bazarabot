"""Tests for zarabot.app.shutdown — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.app.startup import AppContext
from zarabot.config import Config
from zarabot.db.connection import (
    DatabaseNotOpenError,
    connect,
    disconnect,
    shared,
)
from zarabot.models import (
    OrderRecord,
    OrderStatus,
    Position,
    ReconciliationReport,
    Side,
    StopProtection,
)

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _close_process_connection() -> None:
    yield
    await disconnect()


def _config() -> Config:
    return Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=Decimal("100000"),
        position_size_pct=Decimal("10"),
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("10"),
        max_holding_days=3,
        max_open_positions=10,
        reentry_cooldown_minutes=120,
        daily_loss_limit_pct=Decimal("5"),
        watchlist=("SBER",),
        enabled_strategies=("ma_crossover",),
        ml_model_path=None,
        poll_interval_seconds=60,
        db_path=Path("zarabot.db"),
        backup_dir=Path("backups"),
        log_level="INFO",
        tz="Europe/Moscow",
    )


def _ctx() -> AppContext:
    return AppContext(
        config=_config(),
        strategies=(),
        halt=None,
        reconciliation=ReconciliationReport(ran_at=NOW, adjustments=()),
    )


def _order(status: OrderStatus = OrderStatus.SUBMITTING) -> OrderRecord:
    return OrderRecord(
        key="in-flight-key",
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=1,
        status=status,
        filled_lots=None,
        filled_price=None,
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=None,
    )


def _position() -> Position:
    return Position(
        id=1,
        ticker="SBER",
        figi="BBG000000001",
        strategy="ma_crossover",
        lots=2,
        lot_size=10,
        entry_price=Decimal("100"),
        entry_at=NOW,
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        status="OPEN",
        adopted=False,
        open_order_key="open-k",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )


async def test_shutdown_waits_for_in_flight_order_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.shutdown import shutdown

    calls: list[str] = []
    pending = [_order()]
    import zarabot.app.shutdown as shutdown_mod

    async def _unresolved() -> list[OrderRecord]:
        calls.append("unresolved")
        return list(pending)

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        calls.append("resolve")
        pending.clear()
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append("alert")

    async def _open() -> list[object]:
        return []

    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    await shutdown(_ctx(), signal.SIGTERM)
    assert "resolve" in calls
    assert calls.index("unresolved") < calls.index("resolve")


async def test_shutdown_stops_entries_before_it_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain that runs first has already looked past the position the next
    cycle opens. The contract said this and nothing implemented it (#21)."""
    from zarabot.app.shutdown import shutdown

    calls: list[str] = []
    import zarabot.app.shutdown as shutdown_mod

    def _stop() -> None:
        calls.append("stop_entries")

    async def _unresolved() -> list[OrderRecord]:
        calls.append("unresolved")
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    async def _open() -> list[object]:
        return []

    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "stop_entries", _stop)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    await shutdown(_ctx(), signal.SIGTERM)
    assert calls.index("stop_entries") < calls.index("unresolved")


async def test_shutdown_neither_cancels_nor_liquidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.shutdown import shutdown

    forbidden: list[str] = []
    opened = [_position()]
    import zarabot.app.shutdown as shutdown_mod

    async def _unresolved() -> list[OrderRecord]:
        return []

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        return []

    async def _open() -> list[Position]:
        return list(opened)

    async def _cancel(*_a: object, **_k: object) -> None:
        forbidden.append("cancel")

    async def _close(*_a: object, **_k: object) -> None:
        forbidden.append("close")

    async def _post(*_a: object, **_k: object) -> None:
        forbidden.append("post")

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr("zarabot.broker.client.cancel_stop_order", _cancel)
    monkeypatch.setattr("zarabot.execution.orders.close_position", _close)
    monkeypatch.setattr("zarabot.broker.client.post_market_order", _post)
    await shutdown(_ctx(), signal.SIGINT)
    assert forbidden == []
    assert (await _open())[0].status == "OPEN"


async def test_shutdown_leaves_submitting_orders_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zarabot.app.shutdown import shutdown

    resolves = 0
    import zarabot.app.shutdown as shutdown_mod

    async def _unresolved() -> list[OrderRecord]:
        return [_order()]

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        nonlocal resolves
        resolves += 1
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    async def _open() -> list[object]:
        return []

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await shutdown(_ctx(), signal.SIGTERM)
    assert resolves >= 1


async def test_shutdown_closes_the_broker_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process holds one AsyncClient (#18); shutdown is what closes it."""
    import zarabot.app.shutdown as shutdown_mod
    from zarabot.app.shutdown import shutdown

    closes: list[str] = []

    async def _broker_close() -> None:
        closes.append("broker_close")

    async def _unresolved() -> list[OrderRecord]:
        return []

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        return []

    async def _open() -> list[object]:
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    monkeypatch.setattr("zarabot.broker.client.close", _broker_close)
    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    await shutdown(_ctx(), signal.SIGTERM)
    assert closes == ["broker_close"]


async def test_shutdown_closes_the_broker_only_after_settling_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement talks to the broker, so the channel must outlive the drain."""
    import zarabot.app.shutdown as shutdown_mod
    from zarabot.app.shutdown import shutdown

    calls: list[str] = []
    broker_closed = False
    pending = [_order()]

    async def _broker_close() -> None:
        nonlocal broker_closed
        broker_closed = True
        calls.append("broker_close")

    async def _unresolved() -> list[OrderRecord]:
        assert not broker_closed, "queried orders after closing the broker channel"
        calls.append("unresolved")
        return list(pending)

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        assert not broker_closed, "settled an order after closing the broker channel"
        calls.append("resolve")
        pending.clear()
        return []

    async def _open() -> list[object]:
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        calls.append("alert")

    async def _disconnect() -> None:
        calls.append("disconnect")

    monkeypatch.setattr("zarabot.broker.client.close", _broker_close)
    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr(shutdown_mod, "disconnect", _disconnect)
    await shutdown(_ctx(), signal.SIGTERM)
    assert calls.index("broker_close") > calls.index("resolve")
    assert calls.index("broker_close") > calls.index("alert")
    assert calls.index("broker_close") > calls.index("disconnect")
    assert calls[-1] == "broker_close"


async def test_shutdown_closes_the_broker_when_orders_stay_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out drain still releases the channel; rows stay SUBMITTING."""
    import zarabot.app.shutdown as shutdown_mod
    from zarabot.app.shutdown import shutdown

    closes: list[str] = []
    stuck = [_order()]

    async def _broker_close() -> None:
        closes.append("broker_close")

    async def _unresolved() -> list[OrderRecord]:
        return list(stuck)

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        return []

    async def _open() -> list[object]:
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("zarabot.broker.client.close", _broker_close)
    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await shutdown(_ctx(), signal.SIGTERM)
    assert closes == ["broker_close"]
    assert stuck[0].status is OrderStatus.SUBMITTING


async def test_shutdown_closing_the_broker_does_not_touch_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing the channel is not an excuse to flatten the book."""
    import zarabot.app.shutdown as shutdown_mod
    from zarabot.app.shutdown import shutdown

    forbidden: list[str] = []
    closes: list[str] = []
    opened = [_position()]

    async def _broker_close() -> None:
        closes.append("broker_close")

    async def _unresolved() -> list[OrderRecord]:
        return []

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        return []

    async def _open() -> list[Position]:
        return list(opened)

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    async def _cancel(*_a: object, **_k: object) -> None:
        forbidden.append("cancel")

    async def _post(*_a: object, **_k: object) -> None:
        forbidden.append("post")

    async def _close_position(*_a: object, **_k: object) -> None:
        forbidden.append("close_position")

    monkeypatch.setattr("zarabot.broker.client.close", _broker_close)
    monkeypatch.setattr("zarabot.broker.client.cancel_stop_order", _cancel)
    monkeypatch.setattr("zarabot.broker.client.post_market_order", _post)
    monkeypatch.setattr("zarabot.execution.orders.close_position", _close_position)
    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    await shutdown(_ctx(), signal.SIGINT)
    assert closes == ["broker_close"]
    assert forbidden == []
    assert opened[0].status == "OPEN"


async def test_shutdown_disconnects_so_shared_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.app.shutdown as shutdown_mod
    from zarabot.app.shutdown import shutdown
    from zarabot.db import connection as db_connection

    await connect(str(tmp_path / "zarabot.db"))
    shared()

    calls: list[str] = []
    real_disconnect = db_connection.disconnect

    async def _disconnect() -> None:
        calls.append("disconnect")
        await real_disconnect()

    async def _unresolved() -> list[OrderRecord]:
        return []

    async def _resolve(moment: datetime) -> list[OrderRecord]:
        return []

    async def _open() -> list[object]:
        return []

    async def _alert(text: str, urgent: bool = False) -> None:
        return None

    monkeypatch.setattr(db_connection, "disconnect", _disconnect)
    monkeypatch.setattr(shutdown_mod, "disconnect", _disconnect, raising=False)
    monkeypatch.setattr(shutdown_mod, "now", lambda: NOW)
    monkeypatch.setattr(shutdown_mod, "list_unresolved", _unresolved)
    monkeypatch.setattr(shutdown_mod, "resolve_unfinished", _resolve)
    monkeypatch.setattr(shutdown_mod, "list_open", _open)
    monkeypatch.setattr(shutdown_mod, "alert", _alert)
    await shutdown(_ctx(), signal.SIGTERM)
    assert calls == ["disconnect"]
    with pytest.raises(DatabaseNotOpenError):
        shared()
