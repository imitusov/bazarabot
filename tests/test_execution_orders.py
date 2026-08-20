"""Tests for zarabot.execution.orders — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from zarabot.broker.client import (
    BrokerUnavailable,
    OrderNotFound,
    OrderRejected,
    StopOrderRejected,
)
from zarabot.db.migrations import apply
from zarabot.db.orders import list_unresolved
from zarabot.db.positions import list_open, set_stop_protection
from zarabot.db.stop_orders import list_active
from zarabot.execution.orders import (
    ExitFailed,
    adopt_existing_stop,
    cancel_orphaned_stop,
    close_executed_stop,
    close_position,
    open_position,
    place_protective_stop,
    replace_stop,
    resolve_unfinished,
)
from zarabot.lifecycle.exits import evaluate
from zarabot.models import (
    ExitTrigger,
    HaltReason,
    Instrument,
    OrderRecord,
    OrderStatus,
    SessionInfo,
    Side,
    Signal,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
)
from zarabot.state.halt import halt, is_halted

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
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
        reference_price=Decimal("100"),
    )


def _session() -> SessionInfo:
    return SessionInfo(start=NOW, end=NOW.replace(hour=20), is_trading_day=True)


class _Broker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.max_lots = 10
        self.reject_entry = False
        self.reject_exit = False
        self.stop_failures_left = 0
        self.timeout = False
        self.missing_state = False
        self.partial_fill_lots: int | None = None
        self.state: dict[str, OrderRecord] = {}

    async def get_max_lots(self, figi: str) -> int:
        return self.max_lots

    async def get_instrument(self, ticker: str) -> Instrument:
        return _instrument()

    async def post_market_order(
        self, key: str, figi: str, side: Side, lots: int
    ) -> OrderRecord:
        self.calls.append(f"post:{side.value}")
        if self.timeout:
            self.timeout = False
            raise BrokerUnavailable("timeout")
        if side is Side.BUY and self.reject_entry:
            raise OrderRejected("nope")
        if side is Side.SELL and self.reject_exit:
            self.reject_exit = False
            raise OrderRejected("exit nope")
        filled = lots
        if side is Side.BUY and self.partial_fill_lots is not None:
            filled = self.partial_fill_lots
        record = OrderRecord(
            key=key,
            ticker="SBER",
            figi=figi,
            side=side,
            intent="ENTRY" if side is Side.BUY else "EXIT",
            lots=lots,
            status=OrderStatus.FILLED,
            filled_lots=filled,
            filled_price=Decimal("100"),
            commission=Decimal("1"),
            broker_reason=None,
            created_at=NOW,
            settled_at=NOW,
        )
        self.state[key] = record
        return record

    async def get_order_state(self, key: str) -> OrderRecord:
        self.calls.append("get_order_state")
        if self.missing_state:
            raise OrderNotFound("gone")
        return self.state[key]

    async def post_stop_loss(
        self, key: str, figi: str, lots: int, stop_price: Decimal
    ) -> StopOrderRecord:
        self.calls.append("post_stop")
        if self.stop_failures_left > 0:
            self.stop_failures_left -= 1
            raise StopOrderRejected("stop rejected")
        return StopOrderRecord(
            key=key,
            stop_order_id="ex-stop",
            position_id=0,
            ticker="SBER",
            lots=lots,
            stop_price=stop_price,
            status=StopOrderStatus.ACTIVE,
            created_at=NOW,
            settled_at=None,
        )

    async def cancel_stop_order(self, stop_order_id: str) -> None:
        self.calls.append("cancel_stop")


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Broker:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    broker = _Broker()
    monkeypatch.setattr("zarabot.execution.orders.get_max_lots", broker.get_max_lots)
    monkeypatch.setattr("zarabot.execution.orders.get_instrument", broker.get_instrument)
    monkeypatch.setattr("zarabot.execution.orders.post_market_order", broker.post_market_order)
    monkeypatch.setattr("zarabot.execution.orders.get_order_state", broker.get_order_state)
    monkeypatch.setattr("zarabot.execution.orders.post_stop_loss", broker.post_stop_loss)
    monkeypatch.setattr("zarabot.execution.orders.cancel_stop_order", broker.cancel_stop_order)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    counter = {"n": 0}

    def _uuid() -> UUID:
        counter["n"] += 1
        return UUID(f"11111111-1111-4111-8111-{counter['n']:012d}")

    monkeypatch.setattr("zarabot.execution.orders.uuid4", _uuid)
    return broker


async def test_entry_writes_before_broker_and_fills(env: _Broker) -> None:
    recorded_before_post = SimpleNamespace(ok=False)
    real_post = env.post_market_order

    async def wrapped_post(*args: Any, **kwargs: Any) -> OrderRecord:
        unresolved = await list_unresolved()
        recorded_before_post.ok = bool(unresolved)
        return await real_post(*args, **kwargs)

    import zarabot.execution.orders as orders_mod

    orders_mod.post_market_order = wrapped_post  # type: ignore[method-assign]
    position = await open_position(_signal(), 2, _instrument())
    assert recorded_before_post.ok is True
    assert position.status == "OPEN"
    assert position.stop_protection is StopProtection.EXCHANGE
    assert position.stop_price == Decimal("95")
    assert position.target_price == Decimal("110")
    stops = await list_active()
    assert len(stops) == 1
    assert stops[0].stop_price == Decimal("95")
    assert await list_unresolved() == []


async def test_crash_before_broker_is_resolved_by_key_lookup(env: _Broker) -> None:
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    unresolved = await list_unresolved()
    assert len(unresolved) == 1
    env.state[unresolved[0].key] = OrderRecord(
        key=unresolved[0].key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=Decimal("100"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    recovered = await resolve_unfinished(NOW)
    assert recovered
    assert recovered[0].status is OrderStatus.FILLED
    assert len(await list_open()) == 1
    assert "get_order_state" in env.calls
    assert env.calls.count("post:BUY") == 1


async def test_timeout_then_fill_opens_position(env: _Broker) -> None:
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    key = (await list_unresolved())[0].key
    env.state[key] = OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=Decimal("100"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    await resolve_unfinished(NOW)
    opened = await list_open()
    assert len(opened) == 1
    assert opened[0].lots == 2


async def test_rejected_entry_opens_no_position(env: _Broker) -> None:
    env.reject_entry = True
    with pytest.raises(OrderRejected):
        await open_position(_signal(), 2, _instrument())
    assert await list_open() == []
    assert await list_unresolved() == []


async def test_order_not_found_settles_as_never_placed(env: _Broker) -> None:
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    env.missing_state = True
    recovered = await resolve_unfinished(NOW)
    assert recovered[0].status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}
    assert await list_open() == []
    assert await list_unresolved() == []


async def test_rejected_exit_raises_exit_failed_and_alerts(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.reject_exit = True
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ExitFailed):
            await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert await list_open()
    assert caplog.records
    await close_position((await list_open())[0], ExitTrigger.TAKE_PROFIT)
    assert await list_open() == []


async def test_take_profit_cancels_stop_before_sell(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.calls.clear()
    await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert env.calls.index("cancel_stop") < env.calls.index("post:SELL")
    assert await list_open() == []


async def test_cancel_of_already_executed_stop_is_not_an_error(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())

    async def already_gone(stop_order_id: str) -> None:
        env.calls.append("cancel_stop")

    import zarabot.execution.orders as orders_mod

    orders_mod.cancel_stop_order = already_gone  # type: ignore[method-assign]
    closed = await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert closed.status == "CLOSED"


async def test_stop_rejected_three_times_leaves_local_open(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    env.stop_failures_left = 3
    with caplog.at_level(logging.ERROR):
        position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.LOCAL
    assert position.status == "OPEN"
    assert await list_active() == []
    assert caplog.records


async def test_position_is_local_until_stop_confirmed(env: _Broker) -> None:
    seen = SimpleNamespace(protection=None)
    real_stop = env.post_stop_loss

    async def wrapped_stop(*args: Any, **kwargs: Any) -> StopOrderRecord:
        opened = await list_open()
        seen.protection = opened[0].stop_protection
        return await real_stop(*args, **kwargs)

    import zarabot.execution.orders as orders_mod

    orders_mod.post_stop_loss = wrapped_stop  # type: ignore[method-assign]
    position = await open_position(_signal(), 2, _instrument())
    assert seen.protection is StopProtection.LOCAL
    assert position.stop_protection is StopProtection.EXCHANGE


async def test_local_stop_owned_by_exits_exchange_is_not(env: _Broker) -> None:
    from zarabot.config import load

    cfg = load()
    env.stop_failures_left = 3
    local = await open_position(_signal(), 2, _instrument())
    assert (
        evaluate(local, Decimal("95"), NOW, _session(), 0, cfg) is ExitTrigger.STOP_LOSS
    )
    env.stop_failures_left = 0
    # Second ticker would be needed; reuse EXCHANGE via a fresh instrument path
    # by closing then reopening after cooldown is irrelevant — use a different ticker.
    other = Instrument(
        figi="BBG000000002",
        ticker="GAZP",
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )
    gazp = Signal(
        ticker="GAZP",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=NOW,
        reference_price=Decimal("100"),
    )
    exchange = await open_position(gazp, 1, other)
    assert exchange.stop_protection is StopProtection.EXCHANGE
    assert evaluate(exchange, Decimal("95"), NOW, _session(), 0, cfg) is None


async def test_set_stop_protection_pairing_invariant(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    from zarabot.db.positions import PositionStateError

    with pytest.raises(PositionStateError):
        await set_stop_protection(position.id, StopProtection.EXCHANGE, None)
    with pytest.raises(PositionStateError):
        await set_stop_protection(position.id, StopProtection.LOCAL, "stop-key")


async def test_lock_released_when_broker_raises(env: _Broker) -> None:
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    env.reject_entry = True
    with pytest.raises(OrderRejected):
        await open_position(_signal(), 2, _instrument())


async def test_concurrent_entries_submit_one_order(env: _Broker) -> None:
    results = await asyncio.gather(
        open_position(_signal(), 2, _instrument()),
        open_position(_signal(), 2, _instrument()),
        return_exceptions=True,
    )
    opened = [item for item in results if not isinstance(item, BaseException)]
    assert len(opened) == 1
    assert env.calls.count("post:BUY") == 1
    assert len(await list_open()) == 1


async def test_halt_does_not_block_close(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    await halt(HaltReason.MANUAL, "entries off", NOW)
    assert await is_halted() is True
    closed = await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert closed.status == "CLOSED"


async def test_partial_entry_opens_filled_lots_only(env: _Broker) -> None:
    env.partial_fill_lots = 1
    position = await open_position(_signal(), 3, _instrument())
    assert position.lots == 1
    stops = await list_active()
    assert stops[0].lots == 1


async def test_max_lots_zero_records_rejection(env: _Broker) -> None:
    env.max_lots = 0
    with pytest.raises(OrderRejected):
        await open_position(_signal(), 2, _instrument())
    assert await list_open() == []
    assert env.calls.count("post:BUY") == 0


async def test_close_executed_stop_does_not_sell(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.calls.clear()
    closed = await close_executed_stop(position, Decimal("95"))
    assert closed.status == "CLOSED"
    assert closed.exit_trigger is ExitTrigger.STOP_LOSS
    assert "post:SELL" not in env.calls
    from zarabot.db.cooldowns import is_active

    assert await is_active("SBER", NOW, 120) is True


async def test_unprotected_position_gets_a_stop(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.LOCAL
    env.stop_failures_left = 0
    protected = await place_protective_stop(position, _instrument())
    assert protected.stop_protection is StopProtection.EXCHANGE
    assert len(await list_active()) == 1


async def test_orphaned_stop_is_cancelled(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    stop = (await list_active())[0]
    env.calls.clear()
    await cancel_orphaned_stop(stop)
    assert "cancel_stop" in env.calls
    assert await list_active() == []
    assert position.status == "OPEN"


async def test_restart_adopts_existing_stop_without_placing_another(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    existing = StopOrderRecord(
        key="existing-stop",
        stop_order_id="broker-stop",
        position_id=position.id,
        ticker="SBER",
        lots=2,
        stop_price=position.stop_price,
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )
    env.calls.clear()
    adopted = await adopt_existing_stop(position, existing)
    assert adopted.stop_protection is StopProtection.EXCHANGE
    assert "post_stop" not in env.calls


async def test_mispriced_stop_is_replaced(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.calls.clear()
    replaced = await replace_stop(position, _instrument())
    assert replaced.stop_protection is StopProtection.EXCHANGE
    assert env.calls.index("cancel_stop") < env.calls.index("post_stop")
