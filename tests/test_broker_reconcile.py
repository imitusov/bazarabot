"""Tests for zarabot.broker.reconcile — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.broker.client import BrokerUnavailable
from zarabot.broker.reconcile import reconcile
from zarabot.db.connection import (
    DatabaseNotOpenError,
    connect,
    disconnect,
    shared,
    transaction,
)
from zarabot.db.migrations import apply
from zarabot.db.positions import get, list_open, set_stop_protection
from zarabot.db.positions import open as open_position
from zarabot.models import (
    ExitTrigger,
    Instrument,
    OperationRecord,
    OrderRecord,
    OrderStatus,
    PortfolioState,
    Position,
    Side,
    Signal,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
)

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
PRICE = Decimal("100.00")
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _instrument(ticker: str = "SBER", figi: str = "BBG000000001") -> Instrument:
    return Instrument(
        figi=figi,
        ticker=ticker,
        lot=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=NOW,
    )


def _broker_position(
    ticker: str = "SBER",
    figi: str = "BBG000000001",
    lots: int = 2,
    price: Decimal = PRICE,
) -> Position:
    return Position(
        id=0,
        ticker=ticker,
        figi=figi,
        strategy="ADOPTED",
        lots=lots,
        lot_size=10,
        entry_price=price,
        entry_at=NOW,
        stop_price=price * Decimal("95") / Decimal("100"),
        target_price=price * Decimal("110") / Decimal("100"),
        status="OPEN",
        adopted=True,
        open_order_key=f"BROKER-{figi}",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )


class _Broker:
    def __init__(self) -> None:
        self.holdings: tuple[Position, ...] = ()
        self.stops: list[StopOrderRecord] = []
        self.last_price = PRICE
        self.operations: list[OperationRecord] = []
        self.operations_fail = False
        self.calls: list[str] = []
        self.alerts: list[str] = []

    async def get_portfolio(self) -> PortfolioState:
        self.calls.append("get_portfolio")
        return PortfolioState(cash=Decimal("100000"), positions=self.holdings)

    async def get_last_price(self, figi: str) -> Decimal:
        self.calls.append("get_last_price")
        return self.last_price

    async def get_instrument(self, ticker: str) -> Instrument:
        self.calls.append("get_instrument")
        return _instrument(ticker=ticker)

    async def get_operations(
        self, since: datetime, until: datetime
    ) -> list[OperationRecord]:
        self.calls.append("get_operations")
        if self.operations_fail:
            raise BrokerUnavailable("operations down")
        return list(self.operations)

    async def list_stop_orders(self) -> list[StopOrderRecord]:
        self.calls.append("list_stop_orders")
        return list(self.stops)

    async def post_market_order(self, *args: object, **kwargs: object) -> None:
        self.calls.append("post_market_order")
        raise AssertionError("reconcile must not place orders")

    async def post_stop_loss(self, *args: object, **kwargs: object) -> None:
        self.calls.append("post_stop_loss")
        raise AssertionError("reconcile must not place stop orders")

    async def cancel_stop_order(self, *args: object, **kwargs: object) -> None:
        self.calls.append("cancel_stop_order")
        raise AssertionError("reconcile must not cancel stop orders")


@pytest.fixture
async def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Broker]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, created_at, settled_at
            ) VALUES (
                'order-1', 'SBER', 'BBG000000001', 'BUY', 'ENTRY', 2,
                'FILLED', 2, '100.00', ?, ?
            )
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        await conn.commit()
    await connect(str(path))
    broker = _Broker()
    module = "zarabot.broker.reconcile"
    monkeypatch.setattr(f"{module}.get_portfolio", broker.get_portfolio)
    monkeypatch.setattr(f"{module}.get_operations", broker.get_operations)
    monkeypatch.setattr(f"{module}.get_instrument", broker.get_instrument)
    monkeypatch.setattr(f"{module}.list_stop_orders", broker.list_stop_orders)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)

    async def _alert(text: str, urgent: bool = False) -> None:
        broker.alerts.append(text)

    monkeypatch.setattr(f"{module}.alert", _alert)
    try:
        yield broker
    finally:
        await disconnect()


async def _open_local() -> Position:
    signal = Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=NOW,
        reference_price=PRICE,
    )
    order = OrderRecord(
        key="order-1",
        ticker="SBER",
        figi="BBG000000001",
        side=Side.BUY,
        intent="ENTRY",
        lots=2,
        status=OrderStatus.FILLED,
        filled_lots=2,
        filled_price=PRICE,
        commission=None,
        broker_reason=None,
        created_at=NOW,
        settled_at=NOW,
    )
    return await open_position(
        signal, order, _instrument(), Decimal("95"), Decimal("110"), NOW
    )


async def _submit_unresolved_entry(
    key: str = "order-2",
    ticker: str = "SBER",
    figi: str = "BBG000000001",
    created_at: datetime = NOW,
) -> None:
    """An entry the bot submitted and never finished resolving.

    This is the crash-recovery case `db.positions.adopt` was written for: the
    bot recognises the holding because it has its own order row for it, and the
    position row is the thing that is missing.
    """
    async with transaction() as conn:
        await conn.execute(
            """
            INSERT INTO orders (
                key, ticker, figi, side, intent, lots, status,
                filled_lots, filled_price, created_at, settled_at
            ) VALUES (?, ?, ?, 'BUY', 'ENTRY', 3, 'SUBMITTED', NULL, NULL, ?, NULL)
            """,
            (key, ticker, figi, created_at.isoformat()),
        )


def _stop(
    ticker: str = "SBER",
    price: Decimal = Decimal("95"),
    stop_id: str = "ex-stop",
    created_at: datetime = NOW,
) -> StopOrderRecord:
    return StopOrderRecord(
        key=stop_id,
        stop_order_id=stop_id,
        position_id=0,
        ticker=ticker,
        lots=2,
        stop_price=price,
        status=StopOrderStatus.ACTIVE,
        created_at=created_at,
        settled_at=None,
    )


async def _count(table: str) -> int:
    cursor = await shared().execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_agreement_produces_no_adjustments_and_no_alert(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    with caplog.at_level(logging.INFO, logger="zarabot.broker.reconcile"):
        report = await reconcile(NOW)
    assert report.adjustments == ()
    assert env.alerts == []
    assert "post_market_order" not in env.calls
    assert "cancel_stop_order" not in env.calls
    events = [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "reconciliation"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.INFO
    assert events[0].adjustments_count == 0
    assert events[0].types == []


def _operation(
    op_id: str,
    operation_type: str,
    *,
    price: Decimal | None = None,
    quantity: int | None = None,
    payment: Decimal = Decimal("0"),
    commission: Decimal = Decimal("0"),
    occurred_at: datetime = NOW,
    parent: str | None = None,
    figi: str = "BBG000000001",
) -> OperationRecord:
    return OperationRecord(
        id=op_id,
        figi=figi,
        ticker="",
        occurred_at=occurred_at,
        commission=commission,
        payment=payment,
        price=price,
        quantity=quantity,
        operation_type=operation_type,
        state="OPERATION_STATE_EXECUTED",
        parent_operation_id=parent,
    )


def _sold_at(price: Decimal, occurred_at: datetime = NOW) -> OperationRecord:
    """One executed sale of the whole position, as the broker records it."""
    return _operation(
        "op-sale",
        "OPERATION_TYPE_SELL",
        price=price,
        quantity=20,
        occurred_at=occurred_at,
    )


async def test_local_open_absent_at_broker_is_closed_at_the_sale_price(
    env: _Broker,
) -> None:
    """The exit is booked at what was traded, not at the quote at the moment of
    detection — which can be days later and on another day entirely (#11)."""
    position = await _open_local()
    env.holdings = ()
    env.last_price = Decimal("92.00")
    sold_at = NOW - timedelta(days=2)
    env.operations = [
        _operation(
            "op-sale",
            "OPERATION_TYPE_SELL",
            price=Decimal("110.00"),
            quantity=20,
            occurred_at=sold_at,
        )
    ]
    report = await reconcile(NOW)
    closed = next(
        item for item in report.adjustments if item["type"] == "CLOSED_EXTERNALLY"
    )
    assert closed["ticker"] == "SBER"
    assert closed["position_id"] == position.id
    assert closed["exit_price"] == "110.00"
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "CLOSED"
    assert stored.exit_trigger is ExitTrigger.EXTERNAL
    assert stored.exit_price == Decimal("110.00")
    assert stored.exit_at == sold_at
    assert await list_open() == []
    assert env.alerts
    assert "post_market_order" not in env.calls
    assert "get_last_price" not in env.calls


async def test_external_close_weights_the_sales_and_sums_their_fees(
    env: _Broker,
) -> None:
    position = await _open_local()
    env.holdings = ()
    later = NOW - timedelta(hours=1)
    env.operations = [
        _operation(
            "op-a",
            "OPERATION_TYPE_SELL",
            price=Decimal("100.00"),
            quantity=3,
            occurred_at=NOW - timedelta(days=1),
        ),
        _operation(
            "op-b",
            "OPERATION_TYPE_SELL",
            price=Decimal("90.00"),
            quantity=2,
            occurred_at=later,
        ),
        _operation(
            "fee-a",
            "OPERATION_TYPE_BROKER_FEE",
            commission=Decimal("1.50"),
            parent="op-a",
        ),
        _operation(
            "fee-b",
            "OPERATION_TYPE_BROKER_FEE",
            commission=Decimal("0.75"),
            parent="op-b",
        ),
        _operation(
            "fee-other",
            "OPERATION_TYPE_BROKER_FEE",
            commission=Decimal("9.99"),
            parent="somebody-elses-trade",
        ),
    ]
    report = await reconcile(NOW)
    closed = next(
        item for item in report.adjustments if item["type"] == "CLOSED_EXTERNALLY"
    )
    assert closed["exit_price"] == "96.00"
    assert closed["exit_commission"] == "2.25"
    stored = await get(position.id)
    assert stored is not None
    assert stored.exit_at == later


async def test_external_close_with_the_feed_unavailable_leaves_it_open(
    env: _Broker,
) -> None:
    """A position closed a cycle late is recoverable; one closed at a
    substituted number is not (rule 33)."""
    position = await _open_local()
    env.holdings = ()
    env.operations_fail = True
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "EXIT_UNRESOLVED" in types
    assert "CLOSED_EXTERNALLY" not in types
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "OPEN"
    assert stored.exit_price is None
    assert env.alerts


async def test_external_close_with_no_sale_in_the_feed_leaves_it_open(
    env: _Broker,
) -> None:
    """Absence and unavailability are both "unknown", never "zero"."""
    position = await _open_local()
    env.holdings = ()
    env.operations = [
        _operation(
            "op-other-figi",
            "OPERATION_TYPE_SELL",
            price=Decimal("110.00"),
            quantity=20,
            figi="BBG000000009",
        ),
        _operation(
            "op-a-buy",
            "OPERATION_TYPE_BUY",
            price=Decimal("100.00"),
            quantity=20,
        ),
    ]
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "EXIT_UNRESOLVED" in types
    stored = await get(position.id)
    assert stored is not None
    assert stored.status == "OPEN"


async def test_unknown_holding_is_reported_foreign_and_never_adopted(
    env: _Broker,
) -> None:
    """Spec §4 `broker.reconcile`: unknown at the broker → FOREIGN_HOLDING."""
    before_positions = await _count("positions")
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "FOREIGN_HOLDING" in types
    assert "ADOPTED" not in types
    foreign = next(
        item for item in report.adjustments if item["type"] == "FOREIGN_HOLDING"
    )
    assert foreign["ticker"] == "SBER"
    assert foreign["lots"] == 3
    assert foreign["average_price"] == "123.45"
    assert await list_open() == []
    assert await _count("positions") == before_positions
    assert env.alerts


async def test_holding_far_above_cost_is_reported_not_adopted_and_not_sold(
    env: _Broker,
) -> None:
    """A manual holding 40% above cost must not become inventory to liquidate."""
    env.holdings = (_broker_position(lots=1, price=Decimal("140.00")),)
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert types.count("FOREIGN_HOLDING") == 1
    assert "ADOPTED" not in types
    foreign = next(
        item for item in report.adjustments if item["type"] == "FOREIGN_HOLDING"
    )
    assert foreign["average_price"] == "140.00"
    assert await list_open() == []
    assert "post_market_order" not in env.calls
    assert "post_stop_loss" not in env.calls
    assert "cancel_stop_order" not in env.calls


async def test_recognised_holding_with_missing_row_is_adopted(env: _Broker) -> None:
    """The crash-recovery case `adopt` was written for is still adopted."""
    await _submit_unresolved_entry()
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "ADOPTED" in types
    assert "FOREIGN_HOLDING" not in types
    adopted = next(item for item in report.adjustments if item["type"] == "ADOPTED")
    assert adopted["ticker"] == "SBER"
    assert adopted["lots"] == 3
    assert adopted["average_price"] == "123.45"
    opened = await list_open()
    assert len(opened) == 1
    assert opened[0].adopted is True
    assert opened[0].strategy == "ADOPTED"


async def test_adopted_position_points_at_the_recognising_order(
    env: _Broker,
) -> None:
    """The adopted row must point at the order the bot actually submitted —
    the whole reason the foreign key exists (#42)."""
    await _submit_unresolved_entry(key="order-2")
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    await reconcile(NOW)
    opened = await list_open()
    assert opened[0].open_order_key == "order-2"


async def test_adoption_uses_the_oldest_unresolved_entry(env: _Broker) -> None:
    """The submission lock should make two impossible; the tie-break is stated
    so the behaviour is not whichever row the query happened to return first."""
    await _submit_unresolved_entry(
        key="order-late", created_at=NOW + timedelta(hours=1)
    )
    await _submit_unresolved_entry(
        key="order-early", created_at=NOW - timedelta(hours=1)
    )
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    await reconcile(NOW)
    opened = await list_open()
    assert opened[0].open_order_key == "order-early"
    assert opened[0].entry_price == Decimal("123.45")
    assert env.alerts


async def test_lot_mismatch_writes_broker_count(env: _Broker) -> None:
    position = await _open_local()
    env.holdings = (_broker_position(lots=1),)
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "LOTS_ADJUSTED" in types
    adjusted = next(
        item for item in report.adjustments if item["type"] == "LOTS_ADJUSTED"
    )
    assert adjusted["position_id"] == position.id
    assert adjusted["from"] == 2
    assert adjusted["to"] == 1
    stored = await get(position.id)
    assert stored is not None
    assert stored.lots == 1
    assert stored.status == "OPEN"
    assert env.alerts


async def test_reconcile_is_idempotent(env: _Broker) -> None:
    await _open_local()
    env.holdings = ()
    env.operations = [_sold_at(Decimal("123.45"))]
    first = await reconcile(NOW)
    assert first.adjustments
    second = await reconcile(NOW)
    write_types = {"CLOSED_EXTERNALLY", "ADOPTED", "LOTS_ADJUSTED"}
    assert not any(item["type"] in write_types for item in second.adjustments)
    assert await list_open() == []


async def test_stop_findings_are_re_reported_until_remedied(env: _Broker) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("90"))]
    first = await reconcile(NOW)
    second = await reconcile(NOW)
    first_stops = tuple(
        item for item in first.adjustments if str(item["type"]).startswith("STOP_")
    )
    second_stops = tuple(
        item for item in second.adjustments if str(item["type"]).startswith("STOP_")
    )
    assert first_stops
    assert second_stops == first_stops
    write_types = {"CLOSED_EXTERNALLY", "ADOPTED", "LOTS_ADJUSTED"}
    assert not any(item["type"] in write_types for item in first.adjustments)
    assert not any(item["type"] in write_types for item in second.adjustments)


async def test_naive_now_is_rejected(env: _Broker) -> None:
    naive = datetime(2026, 3, 16, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await reconcile(naive)


async def test_exchange_position_without_broker_stop_is_reported(env: _Broker) -> None:
    position = await _open_local()
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-key")
    env.holdings = (_broker_position(),)
    report = await reconcile(NOW)
    types = [item["type"] for item in report.adjustments]
    assert "STOP_MISSING" in types


async def test_mispriced_and_orphan_and_adoptable_stops(env: _Broker) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("90"))]
    report = await reconcile(NOW)
    assert any(item["type"] == "STOP_MISPRICED" for item in report.adjustments)

    env.stops = [_stop(price=Decimal("95"))]
    report = await reconcile(NOW)
    assert any(item["type"] == "STOP_ADOPTABLE" for item in report.adjustments)

    env.holdings = ()
    env.stops = [_stop(ticker="GAZP", stop_id="orphan")]
    report = await reconcile(NOW)
    assert any(item["type"] == "STOP_ORPHAN" for item in report.adjustments)


async def test_stop_snapped_to_the_price_increment_is_not_mispriced(
    env: _Broker,
) -> None:
    """The broker rounds a posted stop to the tick; that is not a discrepancy.

    GMKN 125.44 against a stored 125.457, on every restart, cancelled and
    re-posted a stop the broker had placed exactly as asked (spec v1.55).
    """
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("94.995"))]
    report = await reconcile(NOW)
    assert not any(item["type"] == "STOP_MISPRICED" for item in report.adjustments)


async def test_stop_a_full_increment_away_is_still_mispriced(env: _Broker) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("94.99"))]
    report = await reconcile(NOW)
    assert any(item["type"] == "STOP_MISPRICED" for item in report.adjustments)


async def test_tolerated_stop_on_a_local_position_is_adoptable(env: _Broker) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("94.995"))]
    report = await reconcile(NOW)
    assert any(item["type"] == "STOP_ADOPTABLE" for item in report.adjustments)


async def test_unreadable_increment_reports_no_misprice_and_alerts(
    env: _Broker,
) -> None:
    """An unmeasurable difference must not become a cancel-and-re-post."""
    position = await _open_local()
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "ex-stop")
    env.holdings = (_broker_position(),)
    env.stops = [
        _stop(price=Decimal("90")),
        _stop(stop_id="other", price=Decimal("90")),
    ]

    async def _unavailable(ticker: str) -> Instrument:
        raise BrokerUnavailable("instrument metadata down")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("zarabot.broker.reconcile.get_instrument", _unavailable)
    try:
        report = await reconcile(NOW)
    finally:
        monkeypatch.undo()
    types = [item["type"] for item in report.adjustments]
    assert "STOP_MISPRICED" not in types
    assert "STOP_DUPLICATE" in types
    assert any("increment" in text for text in env.alerts)


async def test_unreadable_increment_reports_no_adoptable_stop(env: _Broker) -> None:
    """The price comparison guards adoption, not only replacement.

    Suppressing the misprice finding alone routed an unjudged stop into the
    branch beneath it, and `adopt_existing_stop` would have made the exchange
    the position's sole protection at a price nobody could verify — while
    `lifecycle.exits` stopped firing STOP_LOSS for it (spec v1.56).
    """
    position = await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("90"))]

    async def _unavailable(ticker: str) -> Instrument:
        raise BrokerUnavailable("instrument metadata down")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("zarabot.broker.reconcile.get_instrument", _unavailable)
    try:
        report = await reconcile(NOW)
    finally:
        monkeypatch.undo()
    types = [item["type"] for item in report.adjustments]
    assert "STOP_ADOPTABLE" not in types
    assert "STOP_MISPRICED" not in types
    reloaded = await get(position.id)
    assert reloaded is not None
    assert reloaded.stop_protection is StopProtection.LOCAL


async def test_zero_increment_is_treated_as_unreadable(env: _Broker) -> None:
    await _open_local()
    env.holdings = (_broker_position(),)
    env.stops = [_stop(price=Decimal("90"))]

    async def _zero(ticker: str) -> Instrument:
        instrument = _instrument(ticker=ticker)
        return replace(instrument, min_price_increment=Decimal("0"))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("zarabot.broker.reconcile.get_instrument", _zero)
    try:
        report = await reconcile(NOW)
    finally:
        monkeypatch.undo()
    types = [item["type"] for item in report.adjustments]
    assert "STOP_MISPRICED" not in types
    assert "STOP_ADOPTABLE" not in types
    assert any("increment" in text for text in env.alerts)


async def test_external_close_writes_no_order_row(env: _Broker) -> None:
    await _open_local()
    env.holdings = ()
    env.operations = [_sold_at(Decimal("110.00"))]
    count_before = await _count("orders")
    report = await reconcile(NOW)
    assert any(item["type"] == "CLOSED_EXTERNALLY" for item in report.adjustments)
    assert await list_open() == []
    assert await _count("orders") == count_before
    cursor = await shared().execute(
        "SELECT close_order_key FROM positions WHERE status = 'CLOSED'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None


async def test_two_live_stops_report_duplicate_naming_keeper_and_cancels(
    env: _Broker,
) -> None:
    """§3.2: `keep` is the stop matching `stop_order_key`, `cancel` the rest."""
    position = await _open_local()
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-b")
    env.holdings = (_broker_position(),)
    env.stops = [
        _stop(stop_id="stop-a", created_at=NOW - timedelta(hours=2)),
        _stop(stop_id="stop-b", created_at=NOW - timedelta(hours=1)),
        _stop(stop_id="stop-c", created_at=NOW),
    ]
    report = await reconcile(NOW)
    dup = next(item for item in report.adjustments if item["type"] == "STOP_DUPLICATE")
    assert dup["position_id"] == position.id
    assert dup["ticker"] == "SBER"
    assert dup["keep"] == "stop-b"
    assert sorted(dup["cancel"]) == ["stop-a", "stop-c"]
    assert "cancel_stop_order" not in env.calls


async def test_duplicate_without_stop_order_key_keeps_oldest(env: _Broker) -> None:
    """§3.2: with no `stop_order_key`, the tie-break is oldest `created_at`."""
    position = await _open_local()
    assert position.stop_order_key is None
    env.holdings = (_broker_position(),)
    env.stops = [
        _stop(stop_id="stop-late", created_at=NOW),
        _stop(stop_id="stop-oldest", created_at=NOW - timedelta(days=1)),
        _stop(stop_id="stop-middle", created_at=NOW - timedelta(hours=3)),
    ]
    report = await reconcile(NOW)
    dup = next(item for item in report.adjustments if item["type"] == "STOP_DUPLICATE")
    assert dup["keep"] == "stop-oldest"
    assert sorted(dup["cancel"]) == ["stop-late", "stop-middle"]
    assert "cancel_stop_order" not in env.calls


async def test_module_never_calls_aiosqlite_connect(
    env: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.broker.reconcile as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    assert "_connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    env.holdings = (_broker_position(lots=3, price=Decimal("123.45")),)
    report = await reconcile(NOW)
    assert any(item["type"] == "FOREIGN_HOLDING" for item in report.adjustments)
    cursor = await shared().execute("SELECT ran_at, adjustments FROM reconciliations")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert calls == []


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))

    async def fake_portfolio() -> PortfolioState:
        return PortfolioState(cash=Decimal("100000"), positions=())

    monkeypatch.setattr("zarabot.broker.reconcile.get_portfolio", fake_portfolio)
    with pytest.raises(DatabaseNotOpenError):
        await reconcile(NOW)


async def test_stop_orphan_emits_stop_order_orphaned(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    env.holdings = ()
    env.stops = [_stop(ticker="GAZP", stop_id="orphan")]
    with caplog.at_level(logging.ERROR, logger="zarabot.broker.reconcile"):
        await reconcile(NOW)
    events = [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "stop_order_orphaned"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR
    assert events[0].stop_order_id == "orphan"
    assert events[0].ticker == "GAZP"


async def test_exchange_stop_close_emits_stop_order_executed(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    """An EXCHANGE-protected position gone from the book is an exchange-fired stop."""

    position = await _open_local()
    await set_stop_protection(position.id, StopProtection.EXCHANGE, "stop-key")
    env.holdings = ()
    env.operations = [_sold_at(Decimal("94.00"))]
    with caplog.at_level(logging.INFO, logger="zarabot.broker.reconcile"):
        await reconcile(NOW)
    events = [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "stop_order_executed"
    ]
    assert len(events) == 1
    assert events[0].position_id == position.id
    assert events[0].ticker == "SBER"
    assert events[0].fill_price == Decimal("94.00")
    assert events[0].gap_vs_stop == Decimal("94.00") - Decimal("95")


def _reconciliation_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "reconciliation"
    ]


async def test_reconciliation_event_names_distinct_types_and_counts(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    """A constant {count: 0, types: []} would pass the agreement-only case."""

    env.holdings = (_broker_position(),)
    env.stops = [_stop(ticker="GAZP", stop_id="orphan")]
    with caplog.at_level(logging.INFO, logger="zarabot.broker.reconcile"):
        report = await reconcile(NOW)
    names = [str(item["type"]) for item in report.adjustments]
    assert "FOREIGN_HOLDING" in names
    assert "STOP_ORPHAN" in names
    events = _reconciliation_events(caplog)
    assert len(events) == 1
    assert events[0].adjustments_count == len(report.adjustments)
    assert sorted(events[0].types) == sorted(set(names))


async def test_reconciliation_event_dedupes_repeated_types(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    env.holdings = ()
    env.stops = [
        _stop(ticker="GAZP", stop_id="orphan-a"),
        _stop(ticker="VTBR", stop_id="orphan-b"),
    ]
    with caplog.at_level(logging.INFO, logger="zarabot.broker.reconcile"):
        report = await reconcile(NOW)
    assert len(report.adjustments) == 2
    assert all(item["type"] == "STOP_ORPHAN" for item in report.adjustments)
    events = _reconciliation_events(caplog)
    assert events[0].adjustments_count == 2
    assert events[0].types == ["STOP_ORPHAN"]


async def test_reconciliation_event_is_not_emitted_when_persist_fails(
    env: _Broker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _boom(moment: datetime, adjustments: list[dict[str, object]]) -> None:
        raise RuntimeError("persist failed")

    monkeypatch.setattr("zarabot.broker.reconcile._persist", _boom)
    env.holdings = (_broker_position(),)
    with (
        caplog.at_level(logging.INFO, logger="zarabot.broker.reconcile"),
        pytest.raises(RuntimeError, match="persist failed"),
    ):
        await reconcile(NOW)
    assert _reconciliation_events(caplog) == []


async def test_stop_order_orphaned_uses_key_when_broker_id_is_missing(
    env: _Broker, caplog: pytest.LogCaptureFixture
) -> None:
    env.holdings = ()
    env.stops = [
        StopOrderRecord(
            key="local-key",
            stop_order_id=None,
            position_id=0,
            ticker="GAZP",
            lots=2,
            stop_price=Decimal("95"),
            status=StopOrderStatus.ACTIVE,
            created_at=NOW,
            settled_at=None,
        )
    ]
    with caplog.at_level(logging.ERROR, logger="zarabot.broker.reconcile"):
        await reconcile(NOW)
    events = [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "stop_order_orphaned"
    ]
    assert len(events) == 1
    assert events[0].stop_order_id == "local-key"
