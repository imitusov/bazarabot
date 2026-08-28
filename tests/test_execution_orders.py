"""Tests for zarabot.execution.orders — written from technical-spec.md §3.2."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.orders import get as get_order
from zarabot.db.orders import list_unresolved
from zarabot.db.positions import (
    PositionStateError,
    list_closed,
    list_events,
    list_open,
    set_stop_protection,
)
from zarabot.db.positions import get as get_position
from zarabot.db.signals import record
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
    Position,
    RiskDecision,
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
        self.state_unavailable = False
        self.empty_stop_id = False
        self.posted_status = OrderStatus.FILLED
        self.partial_fill_lots: int | None = None
        self.exit_partial_lots: int | None = None
        self.filled_after_cancel: int | None = None
        self.cancel_unavailable = False
        self.cancelled: list[str] = []
        self.state: dict[str, OrderRecord] = {}
        self.alerts: list[str] = []

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
        status = self.posted_status
        # Since v1.27 a partial is SUBMITTED, never FILLED: the order is still
        # live at the broker. FILLED means filled_lots == lots and nothing
        # further is coming, so the fake must not produce the impossible pair.
        if side is Side.BUY and self.partial_fill_lots is not None:
            filled = self.partial_fill_lots
            status = OrderStatus.SUBMITTED
        elif side is Side.SELL and self.exit_partial_lots is not None:
            filled = self.exit_partial_lots
            status = OrderStatus.SUBMITTED
        elif status is not OrderStatus.FILLED:
            filled = 0
        record = OrderRecord(
            key=key,
            ticker="SBER",
            figi=figi,
            side=side,
            intent="ENTRY" if side is Side.BUY else "EXIT",
            lots=lots,
            status=status,
            filled_lots=filled,
            filled_price=Decimal("100") if filled else None,
            commission=Decimal("1"),
            broker_reason=None,
            created_at=NOW,
            settled_at=NOW,
        )
        self.state[key] = record
        return record

    async def cancel_order(self, key: str) -> None:
        self.calls.append("cancel_order")
        self.cancelled.append(key)
        if self.cancel_unavailable:
            raise BrokerUnavailable("cancel down")
        record = self.state[key]
        filled = self.filled_after_cancel
        if filled is None:
            filled = record.filled_lots or 0
        self.state[key] = replace(
            record,
            status=(
                OrderStatus.FILLED if filled >= record.lots else OrderStatus.CANCELLED
            ),
            filled_lots=filled,
            filled_price=Decimal("100") if filled else None,
        )

    async def get_order_state(self, key: str) -> OrderRecord:
        self.calls.append("get_order_state")
        if self.missing_state:
            raise OrderNotFound("gone")
        if self.state_unavailable:
            raise BrokerUnavailable("state down")
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
            stop_order_id="" if self.empty_stop_id else "ex-stop",
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
    module = "zarabot.execution.orders"
    monkeypatch.setattr(f"{module}.get_max_lots", broker.get_max_lots)
    monkeypatch.setattr(f"{module}.get_instrument", broker.get_instrument)
    monkeypatch.setattr(f"{module}.post_market_order", broker.post_market_order)
    monkeypatch.setattr(f"{module}.get_order_state", broker.get_order_state)
    monkeypatch.setattr(f"{module}.post_stop_loss", broker.post_stop_loss)
    monkeypatch.setattr(f"{module}.cancel_stop_order", broker.cancel_stop_order)
    monkeypatch.setattr(f"{module}.cancel_order", broker.cancel_order)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    counter = {"n": 0}

    def _uuid() -> UUID:
        counter["n"] += 1
        return UUID(f"11111111-1111-4111-8111-{counter['n']:012d}")

    monkeypatch.setattr("zarabot.execution.orders.uuid4", _uuid)

    async def _alert(text: str, urgent: bool = False) -> None:
        broker.alerts.append(text)

    monkeypatch.setattr(f"{module}.alert", _alert, raising=False)
    await connect(str(path))
    try:
        yield broker
    finally:
        await disconnect()


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


async def test_filled_entry_and_exit_persist_broker_commission(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    entry = await get_order(position.open_order_key)
    assert entry is not None
    assert entry.commission == Decimal("1")
    closed = await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert closed.close_order_key is not None
    exit_order = await get_order(closed.close_order_key)
    assert exit_order is not None
    assert exit_order.commission == Decimal("1")
    assert closed.realised_pnl == Decimal("0") - Decimal("1") - Decimal("1")


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


async def test_rejected_exit_raises_exit_failed_and_alerts(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.reject_exit = True
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert await list_open()
    assert env.alerts
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


async def test_stop_rejected_three_times_leaves_local_open(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.LOCAL
    assert position.status == "OPEN"
    assert await list_active() == []
    assert env.alerts


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


async def test_partial_entry_cancels_the_remainder_then_re_reads(
    env: _Broker,
) -> None:
    """Abandoning the remainder means cancelling it, not ignoring it (#10)."""
    env.partial_fill_lots = 1
    await open_position(_signal(), 3, _instrument())
    assert env.cancelled == [next(iter(env.state))]
    order = env.calls.index("cancel_order")
    assert env.calls.index("get_order_state") > order


async def test_partial_entry_opens_from_the_re_read_not_the_response(
    env: _Broker,
) -> None:
    """The pre-cancel response was already stale when it arrived (rule 33)."""
    env.partial_fill_lots = 1
    env.filled_after_cancel = 2
    position = await open_position(_signal(), 3, _instrument())
    assert position.lots == 2


async def test_partial_entry_opens_filled_lots_only(env: _Broker) -> None:
    env.partial_fill_lots = 1
    position = await open_position(_signal(), 3, _instrument())
    assert position.lots == 1
    stops = await list_active()
    assert stops[0].lots == 1


async def test_partial_entry_submits_no_follow_up_buy(env: _Broker) -> None:
    env.partial_fill_lots = 1
    await open_position(_signal(), 3, _instrument())
    assert env.calls.count("post:BUY") == 1


async def test_partial_entry_alerts(env: _Broker) -> None:
    env.partial_fill_lots = 1
    await open_position(_signal(), 3, _instrument())
    assert any("partial entry fill" in text for text in env.alerts)


async def test_partial_entry_with_cancel_unavailable_writes_nothing(
    env: _Broker,
) -> None:
    """An unconfirmed outcome is never written down; recovery resolves it."""
    env.partial_fill_lots = 1
    env.cancel_unavailable = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 3, _instrument())
    assert await list_open() == []
    assert await list_unresolved()


async def test_partial_entry_cancelled_with_nothing_filled_opens_nothing(
    env: _Broker,
) -> None:
    env.partial_fill_lots = 1
    env.filled_after_cancel = 0
    with pytest.raises(OrderRejected):
        await open_position(_signal(), 3, _instrument())
    assert await list_open() == []


async def test_partial_exit_submits_one_order_and_raises(env: _Broker) -> None:
    """No slicing loop: one sell, and a partial settles nothing (#10)."""
    position = await open_position(_signal(), 2, _instrument())
    env.exit_partial_lots = 1
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert env.calls.count("post:SELL") == 1


async def _unresolved_exit(lots: int) -> str:
    from zarabot.db.orders import record_submitting

    order = await record_submitting(
        "exit-partial-key-00000000001",
        "SBER",
        Side.SELL,
        lots,
        "EXIT",
        ExitTrigger.TAKE_PROFIT,
    )
    return order.key


def _broker_entry(key: str, lots: int) -> OrderRecord:
    return OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=lots,
        status=OrderStatus.FILLED,
        filled_lots=lots,
        filled_price=Decimal("100"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )


def _broker_exit(key: str, lots: int, filled: int, status: OrderStatus) -> OrderRecord:
    return OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=lots,
        status=status,
        filled_lots=filled,
        filled_price=Decimal("90"),
        commission=Decimal("1"),
        broker_reason="cancelled",
        created_at=NOW,
        settled_at=NOW,
        exit_trigger=ExitTrigger.TAKE_PROFIT,
    )


async def test_terminal_partial_exit_reduces_the_position_and_leaves_it_open(
    env: _Broker,
) -> None:
    """Closing it would leave shares at the broker with no local row (rule 32)."""
    position = await open_position(_signal(), 5, _instrument())
    key = await _unresolved_exit(5)
    env.state[key] = _broker_exit(key, 5, 2, OrderStatus.CANCELLED)
    await resolve_unfinished(NOW)
    reloaded = await get_position(position.id)
    assert reloaded is not None
    assert reloaded.status == "OPEN"
    assert reloaded.lots == 3
    assert any("partial exit" in text for text in env.alerts)


async def test_terminal_partial_exit_records_the_adjustment_not_a_close(
    env: _Broker,
) -> None:
    position = await open_position(_signal(), 5, _instrument())
    key = await _unresolved_exit(5)
    env.state[key] = _broker_exit(key, 5, 2, OrderStatus.CANCELLED)
    await resolve_unfinished(NOW)
    events = [event.event for event in await list_events(position.id)]
    assert "LOTS_ADJUSTED" in events
    assert "CLOSED" not in events


async def test_recovery_resolves_a_still_live_partial_entry(env: _Broker) -> None:
    """The same cancel-and-re-read, reached from the recovery path (v1.34)."""
    from zarabot.db.orders import record_submitting

    order = await record_submitting(
        "live-partial-key-00000000001", "SBER", Side.BUY, 3, "ENTRY"
    )
    env.state[order.key] = OrderRecord(
        key=order.key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=3,
        status=OrderStatus.SUBMITTED,
        filled_lots=1,
        filled_price=Decimal("100"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=None,
    )
    await resolve_unfinished(NOW)
    assert env.cancelled == [order.key]
    positions = await list_open()
    assert [position.lots for position in positions] == [1]


async def test_recovery_leaves_a_partial_entry_alone_when_cancel_fails(
    env: _Broker,
) -> None:
    from zarabot.db.orders import record_submitting

    order = await record_submitting(
        "live-partial-key-00000000002", "SBER", Side.BUY, 3, "ENTRY"
    )
    env.state[order.key] = OrderRecord(
        key=order.key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=3,
        status=OrderStatus.SUBMITTED,
        filled_lots=1,
        filled_price=Decimal("100"),
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=None,
    )
    env.cancel_unavailable = True
    assert await resolve_unfinished(NOW) == []
    assert await list_open() == []
    assert await list_unresolved()


async def test_terminal_partial_exit_without_an_open_position_does_nothing(
    env: _Broker,
) -> None:
    key = await _unresolved_exit(5)
    env.state[key] = _broker_exit(key, 5, 2, OrderStatus.CANCELLED)
    await resolve_unfinished(NOW)
    assert await list_open() == []


async def test_terminal_exit_with_nothing_filled_leaves_the_position_whole(
    env: _Broker,
) -> None:
    position = await open_position(_signal(), 5, _instrument())
    key = await _unresolved_exit(5)
    env.state[key] = replace(
        _broker_exit(key, 5, 0, OrderStatus.CANCELLED),
        filled_lots=0,
        filled_price=None,
    )
    await resolve_unfinished(NOW)
    reloaded = await get_position(position.id)
    assert reloaded is not None
    assert reloaded.lots == 5


async def test_terminal_full_exit_without_a_trigger_alerts_and_leaves_open(
    env: _Broker,
) -> None:
    """A missing trigger is a data defect, not a value to guess at."""
    from zarabot.config import load

    position = await open_position(_signal(), 5, _instrument())
    key = "defective-full-exit-key-000001"
    # `record_submitting` refuses an EXIT with no trigger, so the row can only
    # arise from corruption. That is exactly the case this guard is for.
    async with aiosqlite.connect(load().db_path) as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, commission, broker_reason,
                created_at, settled_at, exit_trigger
            ) VALUES (
                ?, 'SBER', '', 'SELL', 'EXIT', 5, 'SUBMITTING',
                NULL, NULL, NULL, NULL, ?, NULL, NULL
            )
            """,
            (key, NOW.isoformat()),
        )
        await conn.commit()
    env.state[key] = _broker_exit(key, 5, 5, OrderStatus.CANCELLED)
    await resolve_unfinished(NOW)
    reloaded = await get_position(position.id)
    assert reloaded is not None
    assert reloaded.status == "OPEN"
    assert any("has no trigger" in text for text in env.alerts)


async def test_terminal_exit_reaching_the_full_count_closes(env: _Broker) -> None:
    """The race where a cancel lands after a full fill still books the exit."""
    position = await open_position(_signal(), 5, _instrument())
    key = await _unresolved_exit(5)
    env.state[key] = _broker_exit(key, 5, 5, OrderStatus.CANCELLED)
    await resolve_unfinished(NOW)
    reloaded = await get_position(position.id)
    assert reloaded is not None
    assert reloaded.status == "CLOSED"


async def test_max_lots_zero_records_rejection(env: _Broker) -> None:
    env.max_lots = 0
    with pytest.raises(OrderRejected):
        await open_position(_signal(), 2, _instrument())
    assert await list_open() == []
    assert env.calls.count("post:BUY") == 0


async def test_close_executed_stop_does_not_sell(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.calls.clear()
    closed = await close_executed_stop(position, _stop_fill(Decimal("95")))
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


async def test_restart_adopts_existing_stop_without_placing_another(
    env: _Broker,
) -> None:
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


async def test_replace_stop_on_missing_position(env: _Broker) -> None:
    ghost = _ghost_position()
    assert await replace_stop(ghost, _instrument()) is ghost


async def test_resolve_fill_skips_when_already_open(env: _Broker) -> None:
    from zarabot.db.orders import record_submitting

    await open_position(_signal(), 2, _instrument())
    extra = await record_submitting(
        "dup-entry-key-000000000001", "SBER", Side.BUY, 1, "ENTRY"
    )
    env.state[extra.key] = OrderRecord(
        key=extra.key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=1,
        status=OrderStatus.FILLED,
        filled_lots=1,
        filled_price=Decimal("100"),
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    await resolve_unfinished(NOW)
    assert len(await list_open()) == 1


def _ghost_position() -> Position:
    return Position(
        id=999,
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
        open_order_key="ghost",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )


async def test_max_lots_clamps_requested_size(env: _Broker) -> None:
    env.max_lots = 1
    position = await open_position(_signal(), 5, _instrument())
    assert position.lots == 1


async def test_unknown_entry_outcome_leaves_unresolved(env: _Broker) -> None:
    """A SUBMITTED order with nothing filled is not cancelled: nothing is held,
    and cancelling a merely pending market order turns every slow fill into a
    missed entry."""
    env.posted_status = OrderStatus.SUBMITTED
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    assert await list_unresolved()
    assert env.cancelled == []


async def test_empty_stop_id_degrades_to_local(env: _Broker) -> None:
    env.empty_stop_id = True
    position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.LOCAL


async def test_place_protective_stop_is_idempotent_when_exchange(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.calls.clear()
    again = await place_protective_stop(position, _instrument())
    assert again.stop_protection is StopProtection.EXCHANGE
    assert "post_stop" not in env.calls


async def test_place_protective_stop_missing_position(env: _Broker) -> None:
    ghost = _ghost_position()
    assert await place_protective_stop(ghost, _instrument()) is ghost


async def test_adopt_stop_requires_broker_id(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    existing = StopOrderRecord(
        key="no-id",
        stop_order_id=None,
        position_id=position.id,
        ticker="SBER",
        lots=2,
        stop_price=position.stop_price,
        status=StopOrderStatus.ACTIVE,
        created_at=NOW,
        settled_at=None,
    )
    with pytest.raises(StopOrderRejected):
        await adopt_existing_stop(position, existing)


async def test_orphan_without_broker_id_still_settles(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    stop = (await list_active())[0]
    orphan = StopOrderRecord(
        key=stop.key,
        stop_order_id=None,
        position_id=stop.position_id,
        ticker=stop.ticker,
        lots=stop.lots,
        stop_price=stop.stop_price,
        status=stop.status,
        created_at=stop.created_at,
        settled_at=None,
    )
    await cancel_orphaned_stop(orphan)
    assert await list_active() == []
    del position


async def test_close_stop_loss_on_local_position_sells(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.LOCAL
    env.calls.clear()
    closed = await close_position(position, ExitTrigger.STOP_LOSS)
    assert closed.status == "CLOSED"
    assert closed.exit_trigger is ExitTrigger.STOP_LOSS
    assert "post:SELL" in env.calls
    assert await list_open() == []


async def test_close_stop_loss_on_exchange_position_raises(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    assert position.stop_protection is StopProtection.EXCHANGE
    env.calls.clear()
    with pytest.raises(ValueError, match="STOP_LOSS"):
        await close_position(position, ExitTrigger.STOP_LOSS)
    assert "post:SELL" not in env.calls
    assert await list_open()


async def test_close_missing_position(env: _Broker) -> None:
    with pytest.raises(PositionStateError):
        await close_position(_ghost_position(), ExitTrigger.TAKE_PROFIT)


async def test_exit_unreachable_raises_exit_failed(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.timeout = True
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.TAKE_PROFIT)
    assert await list_open()


async def test_unknown_exit_outcome_raises_exit_failed(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.posted_status = OrderStatus.SUBMITTED
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.TAKE_PROFIT)


async def test_close_executed_stop_on_local_position(env: _Broker) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    closed = await close_executed_stop(position, _stop_fill(Decimal("95")))
    assert closed.exit_trigger is ExitTrigger.STOP_LOSS


async def test_close_executed_missing_position(env: _Broker) -> None:
    with pytest.raises(PositionStateError):
        await close_executed_stop(_ghost_position(), _stop_fill(Decimal("95")))


async def test_resolve_naive_datetime_rejected(env: _Broker) -> None:
    naive = datetime(2026, 3, 16, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await resolve_unfinished(naive)


async def test_resolve_skips_when_state_unavailable(env: _Broker) -> None:
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    env.state_unavailable = True
    assert await resolve_unfinished(NOW) == []
    assert await list_unresolved()


async def test_resolve_rejected_state(env: _Broker) -> None:
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
        status=OrderStatus.REJECTED,
        filled_lots=0,
        filled_price=None,
        commission=None,
        broker_reason="nope",
        created_at=NOW,
        settled_at=NOW,
    )
    recovered = await resolve_unfinished(NOW)
    assert recovered[0].status is OrderStatus.REJECTED
    assert await list_open() == []


async def test_resolve_unknown_status_left_unresolved(env: _Broker) -> None:
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
        status=OrderStatus.SUBMITTED,
        filled_lots=None,
        filled_price=None,
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=None,
    )
    assert await resolve_unfinished(NOW) == []
    assert await list_unresolved()


async def test_resolve_zero_fill_does_not_open(env: _Broker) -> None:
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
        filled_lots=0,
        filled_price=Decimal("100"),
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    await resolve_unfinished(NOW)
    assert await list_open() == []


async def test_resolve_uses_recorded_signal_strategy(env: _Broker) -> None:
    await record(_signal(), RiskDecision(approved=True, lots=2, reason=None))
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
    assert opened[0].strategy == "ma_crossover"


async def test_recovered_entry_without_a_signal_is_unattributed(
    env: _Broker,
) -> None:
    """ma_crossover is a real strategy whose figures decide whether it stays
    enabled; absorbing every recovered trade biased it in one direction (#11)."""
    from zarabot.reporter.weekly import _strategy_section

    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    key = (await list_unresolved())[0].key
    env.state[key] = _broker_entry(key, 2)
    await resolve_unfinished(NOW)
    opened = await list_open()
    assert opened[0].strategy == "UNATTRIBUTED"

    position = await close_position(opened[0], ExitTrigger.TAKE_PROFIT)
    section = _strategy_section([position])
    assert "UNATTRIBUTED" in section
    assert "ma_crossover" not in section


async def test_recovered_entry_finds_a_signal_from_the_previous_moscow_day(
    env: _Broker,
) -> None:
    """An order filled at 23:58 MSK and recovered at 00:05 is the case where
    recovery matters most, and a single-date lookup failed exactly there."""
    late = datetime(2026, 3, 16, 20, 58, tzinfo=UTC)  # 23:58 MSK
    after_midnight = datetime(2026, 3, 16, 21, 5, tzinfo=UTC)  # 00:05 MSK
    # A strategy that is not the old hardcoded default, so finding the signal
    # and falling back are distinguishable outcomes.
    await record(
        replace(_signal(), strategy="rsi_reversion", generated_at=late),
        RiskDecision(approved=True, lots=2, reason=None),
    )
    env.timeout = True
    with pytest.raises(BrokerUnavailable):
        await open_position(_signal(), 2, _instrument())
    key = (await list_unresolved())[0].key
    env.state[key] = replace(_broker_entry(key, 2), created_at=late)
    await resolve_unfinished(after_midnight)
    opened = await list_open()
    assert opened[0].strategy == "rsi_reversion"


async def test_resolve_exit_fill_closes_position(env: _Broker) -> None:
    position = await open_position(_signal(), 2, _instrument())
    env.timeout = True
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.TAKE_PROFIT)
    key = (await list_unresolved())[0].key
    env.state[key] = OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=Decimal("110"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    await resolve_unfinished(NOW)
    assert await list_open() == []
    closed = await list_closed()
    assert closed[0].exit_trigger is ExitTrigger.TAKE_PROFIT


async def test_resolved_exit_fill_uses_order_row_trigger(
    env: _Broker,
) -> None:
    env.stop_failures_left = 3
    position = await open_position(_signal(), 2, _instrument())
    env.timeout = True
    with pytest.raises(ExitFailed):
        await close_position(position, ExitTrigger.STOP_LOSS)
    key = (await list_unresolved())[0].key
    env.state[key] = OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=Decimal("95"),
        commission=Decimal("1"),
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
        exit_trigger=ExitTrigger.STOP_LOSS,
    )
    await resolve_unfinished(NOW)
    assert await list_open() == []
    closed = await list_closed()
    assert closed[0].exit_trigger is ExitTrigger.STOP_LOSS


async def test_resolved_exit_without_trigger_alerts_and_leaves_position(
    env: _Broker,
) -> None:
    from zarabot.config import load

    position = await open_position(_signal(), 2, _instrument())
    key = "defective-exit-key-00000000001"
    async with aiosqlite.connect(load().db_path) as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, commission, broker_reason,
                created_at, settled_at, exit_trigger
            ) VALUES (
                ?, 'SBER', '', 'SELL', 'EXIT', 2, 'SUBMITTING',
                NULL, NULL, NULL, NULL, ?, NULL, NULL
            )
            """,
            (key, NOW.isoformat()),
        )
        await conn.commit()
    env.state[key] = OrderRecord(
        key=key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=Decimal("95"),
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
        exit_trigger=None,
    )
    await resolve_unfinished(NOW)
    opened = await list_open()
    assert opened
    assert opened[0].id == position.id
    assert env.alerts


async def test_resolve_exit_fill_without_position(env: _Broker) -> None:
    from zarabot.db.orders import record_submitting

    extra = await record_submitting(
        "exit-orphan-key-000000000001",
        "SBER",
        Side.SELL,
        1,
        "EXIT",
        ExitTrigger.TAKE_PROFIT,
    )
    env.state[extra.key] = OrderRecord(
        key=extra.key,
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=1,
        status=OrderStatus.FILLED,
        filled_lots=1,
        filled_price=Decimal("110"),
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    await resolve_unfinished(NOW)
    assert await list_open() == []


async def test_database_write_failure_halts(
    env: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise aiosqlite.Error("disk")

    monkeypatch.setattr("zarabot.execution.orders.record_submitting", boom)
    with pytest.raises(aiosqlite.Error):
        await open_position(_signal(), 2, _instrument())
    assert await is_halted() is True


async def test_halt_failure_after_db_error_is_logged(
    env: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise aiosqlite.Error("disk")

    async def halt_boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("halt failed")

    monkeypatch.setattr("zarabot.execution.orders.record_submitting", boom)
    monkeypatch.setattr("zarabot.execution.orders.halt", halt_boom)
    with pytest.raises(aiosqlite.Error):
        await open_position(_signal(), 2, _instrument())


def _stop_fill(
    price: Decimal | None,
    lots: int = 2,
    commission: Decimal | None = Decimal("1.25"),
) -> OrderRecord:
    """The broker's record of an exchange stop execution (spec §4, rule 33)."""
    return OrderRecord(
        key="exch-1",
        ticker="SBER",
        figi="BBG000000001",
        side=Side.SELL,
        intent="EXIT",
        lots=lots,
        status=OrderStatus.FILLED,
        filled_lots=lots,
        filled_price=price,
        commission=commission,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )


async def test_executed_stop_books_the_brokers_price_not_a_quote(
    env: _Broker,
) -> None:
    """#4: a fill at 95.00 is recorded as 95.00 even if the market says 92.00."""
    position = await open_position(_signal(), 2, _instrument())
    closed = await close_executed_stop(position, _stop_fill(Decimal("95.00")))
    assert closed.exit_price == Decimal("95.00")


async def test_executed_stop_records_the_brokers_commission(env: _Broker) -> None:
    """The exit commission is the broker's, never zero and never estimated."""
    from zarabot.db.orders import get as get_order

    position = await open_position(_signal(), 2, _instrument())
    closed = await close_executed_stop(position, _stop_fill(Decimal("95.00")))
    assert closed.close_order_key is not None
    exit_order = await get_order(closed.close_order_key)
    assert exit_order is not None
    assert exit_order.commission == Decimal("1.25")


async def test_executed_stop_without_a_price_refuses_to_book(env: _Broker) -> None:
    """No fallback price exists: an unpriced stop exit is not bookable."""
    position = await open_position(_signal(), 2, _instrument())
    with pytest.raises(ValueError):
        await close_executed_stop(position, _stop_fill(None))
    still = await get_position(position.id)
    assert still is not None
    assert still.status == "OPEN"
