"""Tests for zarabot.ops.commissions — written from technical-spec.md."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.broker.client import OrderNotFound
from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.orders import get as get_order
from zarabot.db.orders import record_submitting, settle
from zarabot.db.positions import close, get, open
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OrderRecord,
    OrderStatus,
    Side,
    Signal,
)
from zarabot.ops.commissions import backfill

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
KEY = "11111111-1111-4111-8111-111111111111"
EXIT_KEY = "22222222-2222-4222-8222-222222222222"
PRICE = Decimal("100.00")
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _instrument() -> Instrument:
    return Instrument(
        figi="BBG000000001",
        ticker="SBER",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


def _signal() -> Signal:
    return Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=NOW,
        reference_price=PRICE,
    )


def _state(
    key: str,
    commission: Decimal | None,
    *,
    side: Side = Side.BUY,
    intent: str = "ENTRY",
    trigger: ExitTrigger | None = None,
) -> OrderRecord:
    return OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=side,
        intent=intent,
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=PRICE,
        commission=commission,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
        exit_trigger=trigger,
    )


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr("zarabot.db.orders.now", lambda: NOW)
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    alerts: list[str] = []
    broker_calls: list[str] = []
    states: dict[str, OrderRecord | BaseException] = {}

    async def _alert(text: str, urgent: bool = False) -> None:
        alerts.append(text)

    async def _get_order_state(key: str) -> OrderRecord:
        broker_calls.append(f"state:{key}")
        state = states.get(key)
        if isinstance(state, BaseException):
            raise state
        if state is None:
            raise OrderNotFound("gone")
        return state

    async def _get_order_state_by_broker_id(broker_order_id: str) -> OrderRecord:
        broker_calls.append(f"broker:{broker_order_id}")
        state = states.get(broker_order_id)
        if isinstance(state, BaseException):
            raise state
        if state is None:
            raise OrderNotFound("gone")
        return state

    monkeypatch.setattr("zarabot.ops.commissions.alert", _alert, raising=False)
    monkeypatch.setattr(
        "zarabot.ops.commissions.get_order_state", _get_order_state, raising=False
    )
    monkeypatch.setattr(
        "zarabot.ops.commissions.get_order_state_by_broker_id",
        _get_order_state_by_broker_id,
        raising=False,
    )
    await connect(str(path))
    try:
        yield {
            "alerts": alerts,
            "broker_calls": broker_calls,
            "states": states,
            "monkeypatch": monkeypatch,
        }
    finally:
        await disconnect()


async def _round_trip() -> int:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    entry = await settle(KEY, OrderStatus.FILLED, 2, PRICE, None, None)
    position = await open(
        _signal(),
        entry,
        _instrument(),
        Decimal("95"),
        Decimal("110"),
        NOW,
    )
    await record_submitting(
        EXIT_KEY, "SBER", Side.SELL, 2, "EXIT", ExitTrigger.TAKE_PROFIT
    )
    exit_order = await settle(
        EXIT_KEY, OrderStatus.FILLED, 2, Decimal("110.00"), None, None
    )
    await close(
        position.id, ExitTrigger.TAKE_PROFIT, Decimal("110.00"), NOW, exit_order
    )
    return position.id


async def test_backfill_records_commission_and_recomputes(
    env: dict[str, object],
) -> None:
    position_id = await _round_trip()
    states = env["states"]
    assert isinstance(states, dict)
    states[KEY] = _state(KEY, Decimal("1.50"))
    states[EXIT_KEY] = _state(
        EXIT_KEY,
        Decimal("1.50"),
        side=Side.SELL,
        intent="EXIT",
        trigger=ExitTrigger.TAKE_PROFIT,
    )
    updated = await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert updated == 2
    entry = await get_order(KEY)
    exit_order = await get_order(EXIT_KEY)
    assert entry is not None
    assert exit_order is not None
    assert entry.commission == Decimal("1.50")
    assert exit_order.commission == Decimal("1.50")
    closed = await get(position_id)
    assert closed is not None
    assert closed.realised_pnl == Decimal("197.00")
    calls = env["broker_calls"]
    assert isinstance(calls, list)
    assert all(str(call).startswith("state:") for call in calls)
    assert env["alerts"] == []


async def test_backfill_alerts_only_after_24h(env: dict[str, object]) -> None:
    await _round_trip()
    states = env["states"]
    assert isinstance(states, dict)
    states[KEY] = _state(KEY, None)
    states[EXIT_KEY] = replace(_state(EXIT_KEY, None), key=EXIT_KEY)
    monkeypatch = env["monkeypatch"]
    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("zarabot.ops.commissions.now", lambda: NOW + timedelta(hours=1))
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0
    assert env["alerts"] == []
    monkeypatch.setattr(
        "zarabot.ops.commissions.now", lambda: NOW + timedelta(hours=24)
    )
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0
    assert env["alerts"] == []
    monkeypatch.setattr(
        "zarabot.ops.commissions.now", lambda: NOW + timedelta(hours=25)
    )
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0
    alerts = env["alerts"]
    assert isinstance(alerts, list)
    assert any(KEY in text for text in alerts)
    assert any(EXIT_KEY in text for text in alerts)


async def _stop_exit_round_trip(broker_order_id: str) -> int:
    """A stop the exchange fired: the local key is a UUID the broker never saw."""
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    entry = await settle(KEY, OrderStatus.FILLED, 2, PRICE, Decimal("1.50"), None)
    position = await open(
        _signal(), entry, _instrument(), Decimal("95"), Decimal("110"), NOW
    )
    await record_submitting(
        EXIT_KEY, "SBER", Side.SELL, 2, "EXIT", ExitTrigger.STOP_LOSS
    )
    exit_order = await settle(
        EXIT_KEY,
        OrderStatus.FILLED,
        2,
        Decimal("95.00"),
        None,
        "stop executed",
        broker_order_id,
    )
    await close(position.id, ExitTrigger.STOP_LOSS, Decimal("95.00"), NOW, exit_order)
    return position.id


async def test_backfill_resolves_a_stop_exit_by_its_broker_id(
    env: dict[str, object],
) -> None:
    """get_order_state on the invented key can only ever return OrderNotFound,
    which is why this commission was unrecoverable (#8)."""
    position_id = await _stop_exit_round_trip("exch-77")
    states = env["states"]
    assert isinstance(states, dict)
    states["exch-77"] = _state(
        "exch-77",
        Decimal("0.40"),
        side=Side.SELL,
        intent="EXIT",
        trigger=ExitTrigger.STOP_LOSS,
    )
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 1
    exit_order = await get_order(EXIT_KEY)
    assert exit_order is not None
    assert exit_order.commission == Decimal("0.40")
    closed = await get(position_id)
    assert closed is not None
    # Gross -100, minus entry 1.50, minus the recovered exit fee 0.40.
    assert closed.realised_pnl == Decimal("-101.90")
    calls = env["broker_calls"]
    assert isinstance(calls, list)
    assert "broker:exch-77" in calls
    assert f"state:{EXIT_KEY}" not in calls


async def test_backfill_alerts_once_not_once_per_run(
    env: dict[str, object],
) -> None:
    """backfill runs daily and again before every weekly report; an alert that
    repeats forever is equivalent to no alert (#8)."""
    await _stop_exit_round_trip("exch-77")
    monkeypatch = env["monkeypatch"]
    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(
        "zarabot.ops.commissions.now", lambda: NOW + timedelta(hours=25)
    )
    since, until = NOW - timedelta(days=1), NOW + timedelta(days=2)
    await backfill(since, until)
    alerts = env["alerts"]
    assert isinstance(alerts, list)
    assert len(alerts) == 1
    await backfill(since, until)
    assert len(alerts) == 1


async def test_backfill_keeps_trying_an_alerted_row(
    env: dict[str, object],
) -> None:
    """The terminal state is on the telling, not the trying."""
    position_id = await _stop_exit_round_trip("exch-77")
    monkeypatch = env["monkeypatch"]
    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(
        "zarabot.ops.commissions.now", lambda: NOW + timedelta(hours=25)
    )
    since, until = NOW - timedelta(days=1), NOW + timedelta(days=2)
    await backfill(since, until)
    states = env["states"]
    assert isinstance(states, dict)
    states["exch-77"] = _state(
        "exch-77",
        Decimal("0.40"),
        side=Side.SELL,
        intent="EXIT",
        trigger=ExitTrigger.STOP_LOSS,
    )
    assert await backfill(since, until) == 1
    closed = await get(position_id)
    assert closed is not None
    assert closed.realised_pnl == Decimal("-101.90")


async def test_backfill_empty_period_returns_zero(env: dict[str, object]) -> None:
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0
    assert env["broker_calls"] == []


async def test_backfill_skips_broker_errors(env: dict[str, object]) -> None:
    await record_submitting(KEY, "SBER", Side.BUY, 2, "ENTRY")
    await settle(KEY, OrderStatus.FILLED, 2, PRICE, None, None)
    states = env["states"]
    assert isinstance(states, dict)
    states[KEY] = OrderNotFound("gone")
    assert await backfill(NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0
    loaded = await get_order(KEY)
    assert loaded is not None
    assert loaded.commission is None


async def test_backfill_rejects_naive(env: dict[str, object]) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await backfill(naive, NOW)
