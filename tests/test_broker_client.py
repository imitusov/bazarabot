"""Tests for zarabot.broker.client — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import (  # type: ignore[attr-defined]
    AioRequestError,
    StatusCode,
)
from t_tech.invest.schemas import (
    CandleInterval,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderIdType,
    OrderType,
)
from t_tech.invest.utils import decimal_to_money, decimal_to_quotation

from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderNotFound,
    OrderRejected,
    cancel_stop_order,
    get_candles,
    get_instrument,
    get_last_price,
    get_max_lots,
    get_operations,
    get_order_state,
    get_portfolio,
    get_trading_schedule,
    list_stop_orders,
    post_market_order,
    post_stop_loss,
)
from zarabot.models import (
    Candle,
    Instrument,
    OperationRecord,
    OrderRecord,
    PortfolioState,
    SessionInfo,
    Side,
    StopOrderRecord,
)

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
TOKEN = "tinvest-secret-token"  # noqa: S105
REQUIRED_ENV = {
    "TINVEST_TOKEN": TOKEN,
    "TINVEST_ACCOUNT_ID": "acct-1",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.target: str | None = None
        self.fail: BaseException | None = None
        self.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
        self.order_message = ""


class _AsyncClient:
    def __init__(self, capture: _Capture) -> None:
        self._capture = capture
        self.instruments = self
        self.market_data = self
        self.orders = self
        self.stop_orders = self
        self.operations = self

    async def __aenter__(self) -> _AsyncClient:
        if self._capture.fail is not None:
            raise self._capture.fail
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self._capture.calls.append((name, kwargs))

    async def share_by(self, **kwargs: Any) -> SimpleNamespace:
        self._record("share_by", kwargs)
        return SimpleNamespace(
            instrument=SimpleNamespace(
                figi="BBG000000001",
                ticker="SBER",
                lot=10,
                min_price_increment=decimal_to_quotation(Decimal("0.01")),
                currency="rub",
                trading_status=SimpleNamespace(
                    name="SECURITY_TRADING_STATUS_NORMAL_TRADING"
                ),
                uid="uid-sber",
            )
        )

    async def get_candles(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_candles", kwargs)
        ts = datetime(2026, 3, 13, 0, 0, tzinfo=UTC)
        return SimpleNamespace(
            candles=[
                SimpleNamespace(
                    time=ts,
                    open=decimal_to_quotation(Decimal("100")),
                    high=decimal_to_quotation(Decimal("101")),
                    low=decimal_to_quotation(Decimal("99")),
                    close=decimal_to_quotation(Decimal("100.5")),
                    volume=1000,
                )
            ]
        )

    async def get_last_prices(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_last_prices", kwargs)
        return SimpleNamespace(
            last_prices=[SimpleNamespace(price=decimal_to_quotation(Decimal("123.45")))]
        )

    async def get_portfolio(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_portfolio", kwargs)
        return SimpleNamespace(
            total_amount_currencies=decimal_to_money(Decimal("50000"), "rub"),
            positions=[],
        )

    async def trading_schedules(self, **kwargs: Any) -> SimpleNamespace:
        self._record("trading_schedules", kwargs)
        start = datetime(2026, 3, 16, 6, 50, tzinfo=UTC)
        end = datetime(2026, 3, 16, 15, 50, tzinfo=UTC)
        return SimpleNamespace(
            exchanges=[
                SimpleNamespace(
                    exchange="MOEX",
                    days=[
                        SimpleNamespace(
                            is_trading_day=True,
                            start_time=start,
                            end_time=end,
                        )
                    ],
                )
            ]
        )

    async def post_order(self, **kwargs: Any) -> SimpleNamespace:
        self._record("post_order", kwargs)
        status = self._capture.order_status
        return SimpleNamespace(
            order_id="exch-1",
            execution_report_status=status,
            lots_requested=kwargs.get("quantity", 1),
            lots_executed=(
                1
                if status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
                else 0
            ),
            executed_order_price=decimal_to_money(Decimal("100"), "rub"),
            executed_commission=decimal_to_money(Decimal("0.5"), "rub"),
            message=self._capture.order_message,
            figi=kwargs.get("figi", "BBG000000001"),
            direction=kwargs.get("direction"),
            order_request_id=kwargs.get("order_id", ""),
        )

    async def post_stop_order(self, **kwargs: Any) -> SimpleNamespace:
        self._record("post_stop_order", kwargs)
        return SimpleNamespace(
            stop_order_id="stop-1",
            order_request_id=kwargs.get("order_id", ""),
        )

    async def cancel_stop_order(self, **kwargs: Any) -> SimpleNamespace:
        self._record("cancel_stop_order", kwargs)
        return SimpleNamespace()

    async def get_stop_orders(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_stop_orders", kwargs)
        return SimpleNamespace(
            stop_orders=[
                SimpleNamespace(
                    stop_order_id="stop-1",
                    lots_requested=1,
                    figi="BBG000000001",
                    ticker="SBER",
                    stop_price=decimal_to_money(Decimal("95"), "rub"),
                    create_date=NOW,
                    order_request_id="k-stop",
                    status=SimpleNamespace(name="STOP_ORDER_STATUS_ACTIVE"),
                )
            ]
        )

    async def get_max_lots(self, request: Any) -> SimpleNamespace:
        self._record("get_max_lots", {"request": request})
        return SimpleNamespace(
            buy_limits=SimpleNamespace(buy_max_lots=8, buy_max_market_lots=7)
        )

    async def get_operations(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_operations", kwargs)
        return SimpleNamespace(
            operations=[
                SimpleNamespace(
                    id="op-1",
                    figi="BBG000000001",
                    date=NOW,
                    payment=decimal_to_money(Decimal("-1.25"), "rub"),
                    price=decimal_to_money(Decimal("100"), "rub"),
                    quantity=10,
                    operation_type=SimpleNamespace(name="OPERATION_TYPE_BROKER_FEE"),
                )
            ]
        )

    async def get_order_state(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_order_state", kwargs)
        return SimpleNamespace(
            order_id="exch-1",
            execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
            lots_requested=1,
            lots_executed=1,
            executed_order_price=decimal_to_money(Decimal("100"), "rub"),
            executed_commission=decimal_to_money(Decimal("0.5"), "rub"),
            figi="BBG000000001",
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_date=NOW,
            order_request_id=kwargs.get("order_id", ""),
        )


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    cap = _Capture()

    def factory(token: str, target: str | None = None, **_: Any) -> _AsyncClient:
        assert token  # used, never logged
        cap.target = target
        return _AsyncClient(cap)

    monkeypatch.setattr("zarabot.broker.client.AsyncClient", factory)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    return cap


@pytest.mark.asyncio
async def test_get_instrument_returns_domain_type(capture: _Capture) -> None:
    instrument = await get_instrument("SBER")
    assert isinstance(instrument, Instrument)
    assert instrument.ticker == "SBER"
    assert instrument.lot == 10
    assert isinstance(instrument.min_price_increment, Decimal)


@pytest.mark.asyncio
async def test_get_candles_returns_oldest_first_aware_decimals(
    capture: _Capture,
) -> None:
    candles = await get_candles(
        "BBG000000001",
        CandleInterval.CANDLE_INTERVAL_DAY,
        NOW - timedelta(days=5),
        NOW,
    )
    assert candles
    assert isinstance(candles[0], Candle)
    assert candles[0].timestamp.tzinfo is not None
    assert isinstance(candles[0].close, Decimal)


@pytest.mark.asyncio
async def test_get_last_price_is_decimal(capture: _Capture) -> None:
    price = await get_last_price("BBG000000001")
    assert price == Decimal("123.45")
    assert type(price) is Decimal


@pytest.mark.asyncio
async def test_get_portfolio_returns_portfolio_state(capture: _Capture) -> None:
    state = await get_portfolio()
    assert isinstance(state, PortfolioState)
    assert state.cash == Decimal("50000")


@pytest.mark.asyncio
async def test_get_trading_schedule_returns_session_info(capture: _Capture) -> None:
    sessions = await get_trading_schedule(1)
    assert sessions
    assert isinstance(sessions[0], SessionInfo)
    assert sessions[0].is_trading_day is True


@pytest.mark.asyncio
async def test_post_market_order_returns_order_record_and_disables_margin(
    capture: _Capture,
) -> None:
    record = await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert isinstance(record, OrderRecord)
    assert record.key == "key-1"
    assert record.commission == Decimal("0.5")
    assert isinstance(record.commission, Decimal)
    posted = [kwargs for name, kwargs in capture.calls if name == "post_order"]
    assert posted
    assert posted[0]["confirm_margin_trade"] is False
    assert posted[0]["order_type"] is OrderType.ORDER_TYPE_MARKET


@pytest.mark.asyncio
async def test_post_stop_loss_disables_margin(capture: _Capture) -> None:
    record = await post_stop_loss("sk-1", "BBG000000001", 1, Decimal("95"))
    assert isinstance(record, StopOrderRecord)
    posted = [kwargs for name, kwargs in capture.calls if name == "post_stop_order"]
    assert posted
    assert posted[0]["confirm_margin_trade"] is False


@pytest.mark.asyncio
async def test_cancel_stop_order_is_idempotent(capture: _Capture) -> None:
    await cancel_stop_order("stop-1")


@pytest.mark.asyncio
async def test_list_stop_orders_returns_domain_records(capture: _Capture) -> None:
    records = await list_stop_orders()
    assert records
    assert isinstance(records[0], StopOrderRecord)


@pytest.mark.asyncio
async def test_get_max_lots_returns_int(capture: _Capture) -> None:
    assert await get_max_lots("BBG000000001") == 7


@pytest.mark.asyncio
async def test_get_operations_returns_domain_records(capture: _Capture) -> None:
    ops = await get_operations(NOW - timedelta(days=1), NOW)
    assert ops
    assert isinstance(ops[0], OperationRecord)
    assert isinstance(ops[0].commission, Decimal)


@pytest.mark.asyncio
async def test_get_order_state_uses_request_id_type(capture: _Capture) -> None:
    record = await get_order_state("key-1")
    assert isinstance(record, OrderRecord)
    assert record.commission == Decimal("0.5")
    assert isinstance(record.commission, Decimal)
    called = [kwargs for name, kwargs in capture.calls if name == "get_order_state"]
    assert called[0]["order_id"] == "key-1"
    assert called[0]["order_id_type"] is OrderIdType.ORDER_ID_TYPE_REQUEST


@pytest.mark.asyncio
async def test_commission_is_converted_with_money_to_decimal(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "zarabot.broker.client.money_to_decimal",
        lambda _money: Decimal("7.25"),
        raising=False,
    )
    posted = await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert posted.commission == Decimal("7.25")
    state = await get_order_state("key-1")
    assert state.commission == Decimal("7.25")


@pytest.mark.asyncio
async def test_transport_error_raises_broker_unavailable(
    capture: _Capture,
) -> None:
    capture.fail = AioRequestError(StatusCode.UNAVAILABLE, "down", None)
    with pytest.raises(BrokerUnavailable) as exc:
        await get_last_price("BBG000000001")
    assert TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_rate_limit_raises_broker_rate_limited_with_hint(
    capture: _Capture,
) -> None:
    capture.fail = AioRequestError(
        StatusCode.RESOURCE_EXHAUSTED, "slow down", {"retry-after": "2.5"}
    )
    with pytest.raises(BrokerRateLimited) as exc:
        await get_last_price("BBG000000001")
    assert exc.value.retry_after == Decimal("2.5") or exc.value.retry_after == 2.5
    assert TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_order_rejection_raises_order_rejected_with_reason(
    capture: _Capture,
) -> None:
    capture.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED
    capture.order_message = "insufficient funds"
    with pytest.raises(OrderRejected) as exc:
        await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert "insufficient funds" in str(exc.value)
    assert TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_sandbox_mode_uses_sandbox_endpoint(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    await get_last_price("BBG000000001")
    assert capture.target == INVEST_GRPC_API_SANDBOX


@pytest.mark.asyncio
async def test_get_candles_rejects_naive_datetimes(capture: _Capture) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await get_candles(
            "BBG000000001", CandleInterval.CANDLE_INTERVAL_DAY, naive, NOW
        )


@pytest.mark.asyncio
async def test_missing_instrument_raises_instrument_not_found(
    capture: _Capture,
) -> None:
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "no such share", None)
    with pytest.raises(InstrumentNotFound):
        await get_instrument("XXXX")


@pytest.mark.asyncio
async def test_missing_order_raises_order_not_found(capture: _Capture) -> None:
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "no order", None)
    with pytest.raises(OrderNotFound):
        await get_order_state("missing-key")
