"""Tests for zarabot.broker.client — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    StopOrderStatusOption,
)
from t_tech.invest.utils import decimal_to_money, decimal_to_quotation

import zarabot.broker.client as broker_client
from zarabot import config
from zarabot.broker.client import (
    BrokerRateLimited,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderNotFound,
    OrderRejected,
    PriceRejected,
    cancel_order,
    cancel_stop_order,
    get_candles,
    get_executed_stop_fills,
    get_instrument,
    get_last_price,
    get_max_lots,
    get_operations,
    get_order_state,
    get_order_state_by_broker_id,
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
    OrderStatus,
    PortfolioState,
    SessionInfo,
    Side,
    StopOrderRecord,
)

# A Monday, mid-session. The weekend that follows is 21-22 March 2026.
NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 3, 16, 0, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 3, 21, tzinfo=UTC).date()
SUNDAY = datetime(2026, 3, 22, tzinfo=UTC).date()
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SCHEDULE_DAYS = 14

TOKEN = "tinvest-secret-token"  # noqa: S105
REQUIRED_ENV = {
    "TINVEST_TOKEN": TOKEN,
    "TINVEST_ACCOUNT_ID": "acct-1",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}

# The main equity board: 10:00-18:54:59 MSK, weekends closed.
MAIN_BOARD_OPEN = (7, 0, 0)
MAIN_BOARD_CLOSE = (15, 54, 59)
# The extended session the substring filter used to pick: to 23:49 MSK, and it
# reports Saturday and Sunday as trading days (#43).
WEEKEND_BOARD_OPEN = (4, 0, 0)
WEEKEND_BOARD_CLOSE = (20, 49, 59)


def _day(
    moment: datetime,
    *,
    trading: bool,
    open_at: tuple[int, int, int],
    close_at: tuple[int, int, int],
) -> SimpleNamespace:
    if not trading:
        # A closed day carries 1970-01-01 in both timestamps.
        return SimpleNamespace(
            date=moment, is_trading_day=False, start_time=EPOCH, end_time=EPOCH
        )
    return SimpleNamespace(
        date=moment,
        is_trading_day=True,
        start_time=moment.replace(
            hour=open_at[0], minute=open_at[1], second=open_at[2]
        ),
        end_time=moment.replace(
            hour=close_at[0], minute=close_at[1], second=close_at[2]
        ),
    )


def _board(name: str, *, weekends_trade: bool, days: int) -> SimpleNamespace:
    open_at = WEEKEND_BOARD_OPEN if weekends_trade else MAIN_BOARD_OPEN
    close_at = WEEKEND_BOARD_CLOSE if weekends_trade else MAIN_BOARD_CLOSE
    entries = []
    for offset in range(days):
        moment = MIDNIGHT + timedelta(days=offset)
        weekend = moment.weekday() >= 5
        entries.append(
            _day(
                moment,
                trading=weekends_trade or not weekend,
                open_at=open_at,
                close_at=close_at,
            )
        )
    return SimpleNamespace(exchange=name, days=entries)


# Cash is a currency position in the portfolio. RUB is the only currency the
# schema allows an instrument to trade in (migrations/001_initial.sql), so it is
# the only one that is buying power.
RUB_FIGI = "RUB000UTSTOM"
USD_FIGI = "BBG0013HGFT4"


def _currency_position(
    figi: str, ticker: str, quantity: Decimal, blocked: Decimal = Decimal(0)
) -> SimpleNamespace:
    """`quantity` is the balance held, `blocked_lots` the part already reserved."""
    return SimpleNamespace(
        figi=figi,
        ticker=ticker,
        instrument_type="currency",
        quantity=decimal_to_quotation(quantity),
        quantity_lots=decimal_to_quotation(quantity),
        # A currency position is priced in roubles whatever currency it holds,
        # so the price's currency code does not identify it.
        average_position_price=decimal_to_money(Decimal("1"), "rub"),
        current_price=decimal_to_money(Decimal("1"), "rub"),
        blocked=blocked > 0,
        blocked_lots=decimal_to_quotation(blocked),
    )


def _share_position(
    figi: str, ticker: str, lots: int, lot_size: int, entry: Decimal
) -> SimpleNamespace:
    return SimpleNamespace(
        figi=figi,
        ticker=ticker,
        instrument_type="share",
        quantity=decimal_to_quotation(Decimal(lots * lot_size)),
        quantity_lots=decimal_to_quotation(Decimal(lots)),
        average_position_price=decimal_to_money(entry, "rub"),
        current_price=decimal_to_money(entry, "rub"),
        blocked=False,
        blocked_lots=decimal_to_quotation(Decimal(0)),
    )


# 60000 RUB on the account, 10000 of it blocked by a standing order, plus 100
# USD worth 80000 RUB. total_amount_currencies converts and sums all of it.
PORTFOLIO_RUB = Decimal("60000")
PORTFOLIO_RUB_BLOCKED = Decimal("10000")
PORTFOLIO_AVAILABLE_RUB = PORTFOLIO_RUB - PORTFOLIO_RUB_BLOCKED
PORTFOLIO_TOTAL_CURRENCIES = Decimal("140000")


def _default_portfolio_positions() -> list[SimpleNamespace]:
    return [
        _currency_position(
            RUB_FIGI, "RUB000UTSTOM", PORTFOLIO_RUB, PORTFOLIO_RUB_BLOCKED
        ),
        _currency_position(USD_FIGI, "USD000UTSTOM", Decimal("1000")),
        _share_position("BBG000000001", "SBER", 3, 10, Decimal("250")),
    ]


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.targets: list[str | None] = []
        self.tokens: list[str] = []
        self.constructed = 0
        self.closed = 0
        self.fail: BaseException | None = None
        self.operations_override: list[SimpleNamespace] | None = None
        self.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
        self.order_lots_executed: int | None = None
        self.order_message = ""
        self.last_price = Decimal("123.45")
        self.last_price_time: datetime | None = NOW
        # When true the quote object carries no `time` attribute at all, which
        # is what a renamed SDK field would look like (#33).
        self.last_price_omit_time = False
        self.schedule_override: list[SimpleNamespace] | None = None
        self.portfolio_positions: list[SimpleNamespace] = _default_portfolio_positions()
        self.portfolio_total_currencies = PORTFOLIO_TOTAL_CURRENCIES

    @property
    def target(self) -> str | None:
        return self.targets[-1] if self.targets else None

    def kwargs_for(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, kwargs in self.calls if called == name]


class _Services:
    """Stands in for the SDK's AsyncServices. Every service is this object."""

    def __init__(self, capture: _Capture) -> None:
        self._capture = capture
        self.instruments = self
        self.market_data = self
        self.orders = self
        self.stop_orders = self
        self.operations = self

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self._capture.calls.append((name, kwargs))
        if self._capture.fail is not None:
            raise self._capture.fail

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
        newest = datetime(2026, 3, 13, 0, 0, tzinfo=UTC)
        oldest = datetime(2026, 3, 12, 0, 0, tzinfo=UTC)

        def bar(ts: datetime, close: str) -> SimpleNamespace:
            return SimpleNamespace(
                time=ts,
                open=decimal_to_quotation(Decimal("100")),
                high=decimal_to_quotation(Decimal("101")),
                low=decimal_to_quotation(Decimal("99")),
                close=decimal_to_quotation(Decimal(close)),
                volume=1000,
            )

        # Returned newest-first on purpose: the module must order them.
        return SimpleNamespace(candles=[bar(newest, "100.5"), bar(oldest, "99.5")])

    async def get_last_prices(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_last_prices", kwargs)
        quote = SimpleNamespace(price=decimal_to_quotation(self._capture.last_price))
        if not self._capture.last_price_omit_time:
            quote.time = self._capture.last_price_time
        return SimpleNamespace(last_prices=[quote])

    async def get_portfolio(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_portfolio", kwargs)
        return SimpleNamespace(
            total_amount_currencies=decimal_to_money(
                self._capture.portfolio_total_currencies, "rub"
            ),
            positions=list(self._capture.portfolio_positions),
        )

    async def trading_schedules(self, **kwargs: Any) -> SimpleNamespace:
        self._record("trading_schedules", kwargs)
        from_ = kwargs.get("from_")
        to = kwargs.get("to")
        if from_ is None or to is None:
            raise AioRequestError(
                StatusCode.INVALID_ARGUMENT, "30002 from_ and to are required", None
            )
        # Measured against the live account (#39): the horizon is counted from
        # the START OF THE DAY of `from_`, never from the instant of the call.
        # from=now,      to=now+14d      -> INVALID_ARGUMENT 30002
        # from=now,      to=now+13d      -> ok
        # from=midnight, to=midnight+14d -> ok
        day_start = from_.replace(hour=0, minute=0, second=0, microsecond=0)
        if to - day_start > timedelta(days=SCHEDULE_DAYS):
            raise AioRequestError(
                StatusCode.INVALID_ARGUMENT, "30002 horizon exceeds 14 days", None
            )
        if self._capture.schedule_override is not None:
            return SimpleNamespace(exchanges=self._capture.schedule_override)
        span = (to - day_start).days
        exchange = kwargs.get("exchange", "")
        if exchange == "MOEX":
            # Naming it returns exactly one exchange.
            return SimpleNamespace(
                exchanges=[_board("MOEX", weekends_trade=False, days=span)]
            )
        if exchange:
            return SimpleNamespace(exchanges=[])
        # No name: 147 exchanges, 53 of them containing "MOEX". The first
        # substring match is whichever the broker happened to order first.
        return SimpleNamespace(
            exchanges=[
                _board("MOEX_MRNG_EVNG_E_WKND_D", weekends_trade=True, days=span),
                _board("MOEX", weekends_trade=False, days=span),
                _board("SPB", weekends_trade=False, days=span),
            ]
        )

    async def post_order(self, **kwargs: Any) -> SimpleNamespace:
        self._record("post_order", kwargs)
        status = self._capture.order_status
        requested = kwargs.get("quantity", 1)
        executed = self._capture.order_lots_executed
        if executed is None:
            executed = (
                requested
                if status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
                else 0
            )
        return SimpleNamespace(
            order_id="exch-1",
            execution_report_status=status,
            lots_requested=requested,
            lots_executed=executed,
            executed_order_price=decimal_to_money(Decimal("100"), "rub"),
            executed_commission=decimal_to_money(Decimal("0.5"), "rub"),
            message=self._capture.order_message,
            figi=kwargs.get("instrument_id", "BBG000000001"),
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

    async def cancel_order(self, **kwargs: Any) -> SimpleNamespace:
        self._record("cancel_order", kwargs)
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
        if self._capture.operations_override is not None:
            return SimpleNamespace(operations=self._capture.operations_override)
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
                    state=SimpleNamespace(name="OPERATION_STATE_EXECUTED"),
                    parent_operation_id="op-parent",
                )
            ]
        )

    async def get_order_state(self, **kwargs: Any) -> SimpleNamespace:
        self._record("get_order_state", kwargs)
        return SimpleNamespace(
            order_id="exch-1",
            execution_report_status=(
                OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
            ),
            lots_requested=1,
            lots_executed=1,
            executed_order_price=decimal_to_money(Decimal("100"), "rub"),
            executed_commission=decimal_to_money(Decimal("0.5"), "rub"),
            figi="BBG000000001",
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_date=NOW,
            order_request_id=kwargs.get("order_id", ""),
        )


class _AsyncClient:
    """Stands in for t_tech.invest.AsyncClient — one channel, entered once."""

    def __init__(self, capture: _Capture) -> None:
        self._capture = capture
        self.entered = 0

    async def __aenter__(self) -> _Services:
        self.entered += 1
        return _Services(self._capture)

    async def __aexit__(self, *args: object) -> bool:
        self._capture.closed += 1
        return False


@pytest.fixture(autouse=True)
async def _reset_module_state() -> AsyncIterator[None]:
    """No client, no memoised config and no price history leaks between tests."""
    config.get.cache_clear()
    accepted = getattr(broker_client, "_last_accepted", None)
    if isinstance(accepted, dict):
        accepted.clear()
    yield
    closer = getattr(broker_client, "close", None)
    if closer is not None:
        await closer()
    config.get.cache_clear()


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    cap = _Capture()

    def factory(token: str, target: str | None = None, **_: Any) -> _AsyncClient:
        assert token  # used, never logged
        cap.constructed += 1
        cap.tokens.append(token)
        cap.targets.append(target)
        return _AsyncClient(cap)

    monkeypatch.setattr("zarabot.broker.client.AsyncClient", factory)
    monkeypatch.setattr("zarabot.clock.now", lambda: NOW)
    return cap


# --------------------------------------------------------------------------
# Happy path per method — each returns the documented domain type.
# --------------------------------------------------------------------------


async def test_get_instrument_returns_domain_type(capture: _Capture) -> None:
    instrument = await get_instrument("SBER")
    assert isinstance(instrument, Instrument)
    assert instrument.ticker == "SBER"
    assert instrument.lot == 10
    assert isinstance(instrument.min_price_increment, Decimal)


async def test_get_candles_returns_oldest_first_aware_decimals(
    capture: _Capture,
) -> None:
    candles = await get_candles(
        "BBG000000001",
        CandleInterval.CANDLE_INTERVAL_DAY,
        NOW - timedelta(days=5),
        NOW,
    )
    assert len(candles) == 2
    assert isinstance(candles[0], Candle)
    assert candles[0].timestamp < candles[1].timestamp
    assert candles[0].timestamp.tzinfo is not None
    assert isinstance(candles[0].close, Decimal)


async def test_get_last_price_is_decimal(capture: _Capture) -> None:
    price = await get_last_price("BBG000000001")
    assert price == Decimal("123.45")
    assert type(price) is Decimal


async def test_get_portfolio_returns_portfolio_state(capture: _Capture) -> None:
    state = await get_portfolio()
    assert isinstance(state, PortfolioState)
    assert state.cash == PORTFOLIO_AVAILABLE_RUB
    assert isinstance(state.cash, Decimal)


# --------------------------------------------------------------------------
# #16 — cash is RUB buying power, never total_amount_currencies.
# --------------------------------------------------------------------------


async def test_get_portfolio_cash_is_available_rub_not_total_currencies(
    capture: _Capture,
) -> None:
    """The discriminating case: the converted total dwarfs the spendable roubles.

    `total_amount_currencies` sums every currency position converted to roubles,
    blocked funds included, so an order sized against it is refused for
    insufficient funds (#16).
    """
    state = await get_portfolio()
    assert capture.portfolio_total_currencies > PORTFOLIO_AVAILABLE_RUB
    assert state.cash == PORTFOLIO_AVAILABLE_RUB
    assert state.cash != capture.portfolio_total_currencies


async def test_get_portfolio_cash_excludes_blocked_rub(capture: _Capture) -> None:
    """Where the response separates available from blocked, available wins."""
    capture.portfolio_positions = [
        _currency_position(RUB_FIGI, "RUB000UTSTOM", Decimal("60000"), Decimal("25000"))
    ]
    state = await get_portfolio()
    assert state.cash == Decimal("35000")


async def test_get_portfolio_cash_ignores_non_rub_currency_positions(
    capture: _Capture,
) -> None:
    """A dollar balance is not rouble buying power, whatever it converts to."""
    capture.portfolio_positions = [
        _currency_position(USD_FIGI, "USD000UTSTOM", Decimal("1000")),
        _currency_position(RUB_FIGI, "RUB000UTSTOM", Decimal("7000")),
    ]
    state = await get_portfolio()
    assert state.cash == Decimal("7000")


async def test_get_portfolio_without_a_rub_position_reports_no_cash(
    capture: _Capture,
) -> None:
    """No rouble position means nothing to spend — never the converted total."""
    capture.portfolio_positions = [
        _currency_position(USD_FIGI, "USD000UTSTOM", Decimal("1000"))
    ]
    state = await get_portfolio()
    assert state.cash == Decimal(0)


async def test_get_portfolio_cash_is_never_negative(capture: _Capture) -> None:
    """More blocked than held is not negative buying power, it is none."""
    capture.portfolio_positions = [
        _currency_position(RUB_FIGI, "RUB000UTSTOM", Decimal("1000"), Decimal("4000"))
    ]
    state = await get_portfolio()
    assert state.cash == Decimal(0)


async def test_get_portfolio_reports_share_holdings_not_currency_positions(
    capture: _Capture,
) -> None:
    """Currency rows are cash; only instruments become positions."""
    state = await get_portfolio()
    assert [position.ticker for position in state.positions] == ["SBER"]
    holding = state.positions[0]
    assert holding.lots == 3
    assert holding.lot_size == 10
    assert holding.entry_price == Decimal("250")
    assert holding.adopted is True


async def test_get_trading_schedule_returns_session_info(capture: _Capture) -> None:
    sessions = await get_trading_schedule(SCHEDULE_DAYS)
    assert sessions
    assert all(isinstance(session, SessionInfo) for session in sessions)
    trading = [session for session in sessions if session.is_trading_day]
    assert trading
    assert trading[0].start is not None
    assert trading[0].start.tzinfo is not None


async def test_post_market_order_returns_order_record_and_disables_margin(
    capture: _Capture,
) -> None:
    record = await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert isinstance(record, OrderRecord)
    assert record.key == "key-1"
    assert record.status is OrderStatus.FILLED
    assert record.commission == Decimal("0.5")
    assert isinstance(record.commission, Decimal)
    posted = capture.kwargs_for("post_order")
    assert posted
    assert posted[0]["confirm_margin_trade"] is False
    assert posted[0]["order_type"] is OrderType.ORDER_TYPE_MARKET
    assert posted[0]["order_id"] == "key-1"


async def test_post_stop_loss_disables_margin(capture: _Capture) -> None:
    record = await post_stop_loss("sk-1", "BBG000000001", 1, Decimal("95"))
    assert isinstance(record, StopOrderRecord)
    posted = capture.kwargs_for("post_stop_order")
    assert posted
    assert posted[0]["confirm_margin_trade"] is False


async def test_cancel_stop_order_is_idempotent(capture: _Capture) -> None:
    assert await cancel_stop_order("stop-1") is None
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "already gone", None)
    assert await cancel_stop_order("stop-1") is None


async def test_cancel_order_uses_the_request_key(capture: _Capture) -> None:
    """By our own key: after a crash the exchange identifier is what was lost."""
    assert await cancel_order("k-1") is None
    name, kwargs = next(c for c in capture.calls if c[0] == "cancel_order")
    assert kwargs["order_id"] == "k-1"
    assert kwargs["order_id_type"] is OrderIdType.ORDER_ID_TYPE_REQUEST


async def test_cancel_order_is_idempotent(capture: _Capture) -> None:
    """The caller races the exchange; an order already gone is not an error."""
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "already gone", None)
    assert await cancel_order("k-1") is None


async def test_cancel_order_raises_on_transport_failure(capture: _Capture) -> None:
    capture.fail = AioRequestError(StatusCode.UNAVAILABLE, "down", None)
    with pytest.raises(BrokerUnavailable):
        await cancel_order("k-1")


async def test_cancel_order_propagates_a_defect(capture: _Capture) -> None:
    """INVALID_ARGUMENT is a malformed request, not an outage (#23)."""
    capture.fail = AioRequestError(StatusCode.INVALID_ARGUMENT, "bad", None)
    with pytest.raises(AioRequestError):
        await cancel_order("k-1")


async def test_list_stop_orders_returns_domain_records(capture: _Capture) -> None:
    records = await list_stop_orders()
    assert records
    assert isinstance(records[0], StopOrderRecord)
    assert isinstance(records[0].stop_price, Decimal)


async def test_get_order_state_by_broker_id_uses_the_exchange_lookup(
    capture: _Capture,
) -> None:
    """A row filed under a key the broker never saw can only be found by the
    broker's own identifier (#8)."""
    record = await get_order_state_by_broker_id("exch-77")
    called = [kwargs for name, kwargs in capture.calls if name == "get_order_state"]
    assert called[-1]["order_id"] == "exch-77"
    assert called[-1]["order_id_type"] is OrderIdType.ORDER_ID_TYPE_EXCHANGE
    assert record.key == "exch-77"
    assert record.commission is not None


async def test_get_order_state_by_broker_id_not_found(capture: _Capture) -> None:
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "no such order", None)
    with pytest.raises(OrderNotFound):
        await get_order_state_by_broker_id("exch-77")


async def test_get_max_lots_returns_int(capture: _Capture) -> None:
    assert await get_max_lots("BBG000000001") == 7


async def test_get_operations_returns_domain_records(capture: _Capture) -> None:
    ops = await get_operations(NOW - timedelta(days=1), NOW)
    assert ops
    assert isinstance(ops[0], OperationRecord)


def _raw_operation(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": "op-1",
        "figi": "BBG000000001",
        "date": NOW,
        "payment": decimal_to_money(Decimal("9000"), "rub"),
        "price": decimal_to_money(Decimal("100"), "rub"),
        "quantity": 90,
        "operation_type": SimpleNamespace(name="OPERATION_TYPE_SELL"),
        "state": SimpleNamespace(name="OPERATION_STATE_EXECUTED"),
        "parent_operation_id": "",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_get_operations_carries_type_state_and_parent(
    capture: _Capture,
) -> None:
    """A sale is identified by what the broker called it, never by the sign of
    `payment` (#11)."""
    capture.operations_override = [
        _raw_operation(),
        _raw_operation(
            id="op-2",
            payment=decimal_to_money(Decimal("-1.25"), "rub"),
            operation_type=SimpleNamespace(name="OPERATION_TYPE_BROKER_FEE"),
            parent_operation_id="op-1",
        ),
    ]
    sale, fee = await get_operations(NOW - timedelta(days=1), NOW)
    assert sale.operation_type == "OPERATION_TYPE_SELL"
    assert sale.state == "OPERATION_STATE_EXECUTED"
    assert sale.parent_operation_id is None
    assert fee.operation_type == "OPERATION_TYPE_BROKER_FEE"
    assert fee.parent_operation_id == "op-1"


async def test_get_operations_takes_commission_from_the_type_not_a_substring(
    capture: _Capture,
) -> None:
    capture.operations_override = [
        _raw_operation(),
        _raw_operation(
            id="op-2",
            payment=decimal_to_money(Decimal("-1.25"), "rub"),
            operation_type=SimpleNamespace(name="OPERATION_TYPE_BROKER_FEE"),
        ),
    ]
    sale, fee = await get_operations(NOW - timedelta(days=1), NOW)
    assert sale.commission == Decimal("0")
    assert fee.commission == Decimal("1.25")


async def test_get_operations_omits_operations_that_did_not_happen(
    capture: _Capture,
) -> None:
    """A cancelled or still-progressing operation is not something that
    happened, and counting one as a cost or as a sale is the same error."""
    capture.operations_override = [
        _raw_operation(),
        _raw_operation(
            id="op-2", state=SimpleNamespace(name="OPERATION_STATE_CANCELED")
        ),
        _raw_operation(
            id="op-3", state=SimpleNamespace(name="OPERATION_STATE_PROGRESS")
        ),
    ]
    ops = await get_operations(NOW - timedelta(days=1), NOW)
    assert [op.id for op in ops] == ["op-1"]
    assert isinstance(ops[0].commission, Decimal)


async def test_get_order_state_uses_request_id_type(capture: _Capture) -> None:
    record = await get_order_state("key-1")
    assert isinstance(record, OrderRecord)
    assert record.commission == Decimal("0.5")
    called = capture.kwargs_for("get_order_state")
    assert called[0]["order_id"] == "key-1"
    assert called[0]["order_id_type"] is OrderIdType.ORDER_ID_TYPE_REQUEST


async def test_commission_comes_from_executed_commission(capture: _Capture) -> None:
    """Commission is read from the order response, per spec §4 broker.client.

    Asserts the converted VALUE, not which helper converts it.
    """
    posted = await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert posted.commission == Decimal("0.5")
    state = await get_order_state("key-1")
    assert state.commission == Decimal("0.5")


async def test_sandbox_mode_uses_sandbox_endpoint(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    config.get.cache_clear()
    await get_last_price("BBG000000001")
    assert capture.target == INVEST_GRPC_API_SANDBOX
    # Sandbox is the endpoint, never the post_sandbox_* method family.
    assert not any(name.endswith("_sandbox_order") for name, _ in capture.calls)


async def test_get_candles_rejects_naive_datetimes(capture: _Capture) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await get_candles(
            "BBG000000001", CandleInterval.CANDLE_INTERVAL_DAY, naive, NOW
        )


async def test_missing_instrument_raises_instrument_not_found(
    capture: _Capture,
) -> None:
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "no such share", None)
    with pytest.raises(InstrumentNotFound):
        await get_instrument("XXXX")


async def test_missing_order_raises_order_not_found(capture: _Capture) -> None:
    capture.fail = AioRequestError(StatusCode.NOT_FOUND, "no order", None)
    with pytest.raises(OrderNotFound):
        await get_order_state("missing-key")


# --------------------------------------------------------------------------
# #23 — errors are typed by what they are, not by where they were caught.
# --------------------------------------------------------------------------


async def test_transport_error_raises_broker_unavailable(capture: _Capture) -> None:
    capture.fail = AioRequestError(StatusCode.UNAVAILABLE, "down", None)
    with pytest.raises(BrokerUnavailable) as exc:
        await get_last_price("BBG000000001")
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is capture.fail


async def test_invalid_argument_is_not_broker_unavailable(capture: _Capture) -> None:
    """A malformed request is a defect, not weather (#23, and how #39 hid)."""
    root = RuntimeError("grpc layer")
    failure = AioRequestError(StatusCode.INVALID_ARGUMENT, "30002", None)
    failure.__cause__ = root
    capture.fail = failure

    with pytest.raises(AioRequestError) as exc:
        await get_last_price("BBG000000001")

    assert exc.value is failure
    assert not isinstance(exc.value, BrokerUnavailable)
    assert exc.value.code is StatusCode.INVALID_ARGUMENT
    # `from None` would have discarded this.
    assert exc.value.__cause__ is root
    assert exc.value.__traceback__ is not None


async def test_attribute_error_from_renamed_field_reaches_caller(
    capture: _Capture,
) -> None:
    """A renamed SDK field is a programming error and must look like one."""
    capture.fail = AttributeError("'ShareResponse' object has no attribute 'lot'")
    with pytest.raises(AttributeError) as exc:
        await get_instrument("SBER")
    assert not isinstance(exc.value, BrokerUnavailable)
    assert exc.value.__traceback__ is not None
    assert exc.value.__suppress_context__ is False


async def test_type_error_from_changed_shape_reaches_caller(
    capture: _Capture,
) -> None:
    capture.fail = TypeError("post_order() got an unexpected keyword argument")
    with pytest.raises(TypeError):
        await post_market_order("key-1", "BBG000000001", Side.BUY, 1)


async def test_rate_limit_raises_broker_rate_limited_with_hint(
    capture: _Capture,
) -> None:
    capture.fail = AioRequestError(
        StatusCode.RESOURCE_EXHAUSTED, "slow down", {"retry-after": "2.5"}
    )
    with pytest.raises(BrokerRateLimited) as exc:
        await get_last_price("BBG000000001")
    assert exc.value.retry_after == Decimal("2.5")
    assert TOKEN not in str(exc.value)


async def test_rate_limit_reads_sdk_metadata_ratelimit_reset(
    capture: _Capture,
) -> None:
    """The SDK hands back a namedtuple, not a dict — read `ratelimit_reset`."""
    metadata = SimpleNamespace(
        tracking_id="t-1",
        ratelimit_limit="60",
        ratelimit_remaining="0",
        ratelimit_reset="7",
        message="rate limited",
    )
    capture.fail = AioRequestError(StatusCode.RESOURCE_EXHAUSTED, "slow down", metadata)
    with pytest.raises(BrokerRateLimited) as exc:
        await get_last_price("BBG000000001")
    assert exc.value.retry_after == Decimal("7")


async def test_order_rejection_raises_order_rejected_with_reason(
    capture: _Capture,
) -> None:
    capture.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED
    capture.order_message = "insufficient funds"
    with pytest.raises(OrderRejected) as exc:
        await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert "insufficient funds" in str(exc.value)
    assert exc.value.reason == "insufficient funds"
    assert TOKEN not in str(exc.value)


async def test_no_exception_from_this_module_carries_the_token(
    capture: _Capture,
) -> None:
    """The secret boundary, checked on every failure shape the module raises."""
    failures: list[BaseException] = [
        AioRequestError(StatusCode.UNAVAILABLE, f"failed with {TOKEN}", None),
        AioRequestError(StatusCode.NOT_FOUND, f"no share for {TOKEN}", None),
        AioRequestError(
            StatusCode.RESOURCE_EXHAUSTED, f"throttled {TOKEN}", {"retry-after": "1"}
        ),
    ]
    expected = (BrokerUnavailable, InstrumentNotFound, BrokerRateLimited)
    for failure in failures:
        capture.fail = failure
        with pytest.raises(expected) as exc:
            await get_instrument("SBER")
        raised = exc.value
        assert TOKEN not in str(raised)
        assert TOKEN not in repr(raised.args)

    capture.fail = None
    capture.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED
    capture.order_message = f"rejected because {TOKEN} is bad"
    with pytest.raises(OrderRejected) as rejected:
        await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    assert TOKEN not in str(rejected.value)
    assert TOKEN not in str(rejected.value.reason)


# --------------------------------------------------------------------------
# #39 and #43 — the trading calendar.
# --------------------------------------------------------------------------


async def test_trading_schedule_requests_the_main_board_by_name(
    capture: _Capture,
) -> None:
    """`exchange="MOEX"`, not a substring filter over 53 MOEX-ish names (#43)."""
    sessions = await get_trading_schedule(SCHEDULE_DAYS)

    requested = capture.kwargs_for("trading_schedules")
    assert len(requested) == 1
    assert requested[0]["exchange"] == "MOEX"

    by_date = {session.start.date(): session for session in sessions if session.start}
    monday = by_date[MIDNIGHT.date()]
    assert monday.start == MIDNIGHT.replace(hour=7)
    assert monday.end == MIDNIGHT.replace(hour=15, minute=54, second=59)


async def test_trading_schedule_reports_the_weekend_as_closed(
    capture: _Capture,
) -> None:
    """The extended board trades weekends; the main board does not (#43)."""
    sessions = await get_trading_schedule(SCHEDULE_DAYS)
    assert len(sessions) == SCHEDULE_DAYS

    weekend = [sessions[5], sessions[6]]  # Saturday and Sunday
    assert MIDNIGHT + timedelta(days=5) == datetime.combine(
        SATURDAY, datetime.min.time(), tzinfo=UTC
    )
    assert MIDNIGHT + timedelta(days=6) == datetime.combine(
        SUNDAY, datetime.min.time(), tzinfo=UTC
    )
    for session in weekend:
        assert session.is_trading_day is False
        assert session.start is None
        assert session.end is None


async def test_trading_schedule_anchors_the_range_to_the_utc_day_start(
    capture: _Capture,
) -> None:
    """Mid-day + 14 days is rejected by the broker; midnight + 14 is not (#39)."""
    sessions = await get_trading_schedule(SCHEDULE_DAYS)
    assert sessions  # a mid-day anchor would have raised INVALID_ARGUMENT here

    requested = capture.kwargs_for("trading_schedules")[0]
    assert requested["from_"] == MIDNIGHT
    assert requested["from_"] != NOW
    assert requested["to"] == MIDNIGHT + timedelta(days=SCHEDULE_DAYS)
    assert requested["from_"].tzinfo is not None
    assert requested["to"].tzinfo is not None


async def test_trading_schedule_refuses_more_than_fourteen_days(
    capture: _Capture,
) -> None:
    with pytest.raises(ValueError):
        await get_trading_schedule(15)
    assert capture.kwargs_for("trading_schedules") == []


async def test_non_trading_day_yields_no_session_despite_epoch_stamps(
    capture: _Capture,
) -> None:
    """The flag is honoured before the timestamps."""
    capture.schedule_override = [
        SimpleNamespace(
            exchange="MOEX",
            days=[
                SimpleNamespace(
                    date=MIDNIGHT,
                    is_trading_day=False,
                    start_time=EPOCH,
                    end_time=EPOCH,
                )
            ],
        )
    ]
    sessions = await get_trading_schedule(1)
    assert len(sessions) == 1
    assert sessions[0].is_trading_day is False
    assert sessions[0].start is None
    assert sessions[0].end is None


async def test_epoch_stamps_yield_no_session_even_when_flagged_trading(
    capture: _Capture,
) -> None:
    """`is_trading_day` true with 1970 stamps is not a session either."""
    capture.schedule_override = [
        SimpleNamespace(
            exchange="MOEX",
            days=[
                SimpleNamespace(
                    date=MIDNIGHT,
                    is_trading_day=True,
                    start_time=EPOCH,
                    end_time=EPOCH,
                )
            ],
        )
    ]
    sessions = await get_trading_schedule(1)
    assert len(sessions) == 1
    assert sessions[0].start is None
    assert sessions[0].end is None
    assert sessions[0].is_trading_day is False


# --------------------------------------------------------------------------
# #10, client half — a partial fill is not a fill.
# --------------------------------------------------------------------------


async def test_partial_fill_maps_to_submitted_with_filled_lots(
    capture: _Capture,
) -> None:
    capture.order_status = (
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL
    )
    capture.order_lots_executed = 3
    record = await post_market_order("key-1", "BBG000000001", Side.BUY, 10)
    assert record.status is OrderStatus.SUBMITTED
    assert record.status is not OrderStatus.FILLED
    assert record.filled_lots == 3
    assert record.filled_lots is not None
    assert record.filled_lots < record.lots
    assert record.settled_at is None


async def test_full_fill_maps_to_filled(capture: _Capture) -> None:
    capture.order_status = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
    capture.order_lots_executed = 10
    record = await post_market_order("key-1", "BBG000000001", Side.BUY, 10)
    assert record.status is OrderStatus.FILLED
    assert record.filled_lots == record.lots
    assert record.settled_at is not None


# --------------------------------------------------------------------------
# #18 — one channel per process, and one Config.
# --------------------------------------------------------------------------


async def test_successive_calls_reuse_one_client_and_close_releases_it(
    capture: _Capture,
) -> None:
    await get_last_price("BBG000000001")
    await get_last_price("BBG000000001")
    await get_portfolio()
    assert capture.constructed == 1
    assert capture.closed == 0

    await broker_client.close()
    assert capture.closed == 1

    # Closing is not a one-way door: a later call reconnects.
    await get_last_price("BBG000000001")
    assert capture.constructed == 2

    await broker_client.close()
    await broker_client.close()  # idempotent
    assert capture.closed == 2


async def test_close_without_a_client_is_a_no_op(capture: _Capture) -> None:
    await broker_client.close()
    assert capture.constructed == 0
    assert capture.closed == 0


async def test_config_is_read_once_across_a_sequence_of_calls(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No re-reading the environment per request (#18)."""
    config.get()  # prime the process-wide memo before counting
    reads: list[int] = []
    real_get = config.get

    def counting_get() -> config.Config:
        reads.append(1)
        return real_get()

    monkeypatch.setattr("zarabot.config.get", counting_get)
    monkeypatch.setattr(
        "zarabot.config.load", lambda: pytest.fail("load() must not be called")
    )

    await post_market_order("key-1", "BBG000000001", Side.BUY, 1)
    await get_last_price("BBG000000001")
    await get_portfolio()
    await get_order_state("key-1")

    assert len(reads) == 1


# --------------------------------------------------------------------------
# Price validation at the single point prices enter the system.
# --------------------------------------------------------------------------


async def test_zero_quote_raises_price_rejected_not_unavailable(
    capture: _Capture,
) -> None:
    capture.last_price = Decimal(0)
    with pytest.raises(PriceRejected) as exc:
        await get_last_price("FIGI-ZERO")
    assert not isinstance(exc.value, BrokerUnavailable)
    assert TOKEN not in str(exc.value)


async def test_negative_quote_raises_price_rejected(capture: _Capture) -> None:
    capture.last_price = Decimal("-1")
    with pytest.raises(PriceRejected):
        await get_last_price("FIGI-NEG")


async def test_stale_quote_raises_price_rejected_at_age_boundary(
    capture: _Capture,
) -> None:
    capture.last_price = Decimal("100")
    capture.last_price_time = NOW - timedelta(seconds=120)
    assert await get_last_price("FIGI-AGE") == Decimal("100")

    capture.last_price_time = NOW - timedelta(seconds=121)
    with pytest.raises(PriceRejected) as exc:
        await get_last_price("FIGI-AGE-STALE")
    assert not isinstance(exc.value, BrokerUnavailable)


async def test_missing_time_attribute_raises_rather_than_rejecting(
    capture: _Capture,
) -> None:
    """A renamed SDK field must surface as the integration break it is.

    Measured against the live account: LastPrice carries figi, price, time,
    instrument_uid and last_price_type — there is no `timestamp` to fall back
    to. Probing for one could only ever turn a rename into every quote in every
    cycle looking like bad broker data, in which state no LOCAL position's
    stop-loss can fire (#33).
    """
    capture.last_price_omit_time = True
    with pytest.raises(AttributeError, match="time"):
        await get_last_price("FIGI-RENAMED")


async def test_quote_without_timestamp_is_rejected(capture: _Capture) -> None:
    capture.last_price_time = None
    with pytest.raises(PriceRejected):
        await get_last_price("FIGI-NOTIME")


async def test_naive_quote_timestamp_is_rejected_not_a_value_error(
    capture: _Capture,
) -> None:
    capture.last_price_time = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(PriceRejected):
        await get_last_price("FIGI-NAIVE")


async def test_implausible_move_raises_price_rejected_and_keeps_baseline(
    capture: _Capture,
) -> None:
    capture.last_price = Decimal("100")
    figi = "FIGI-MOVE"
    assert await get_last_price(figi) == Decimal("100")

    capture.last_price = Decimal("121")
    with pytest.raises(PriceRejected):
        await get_last_price(figi)

    # The rejected value did not become the new baseline.
    capture.last_price = Decimal("110")
    assert await get_last_price(figi) == Decimal("110")


# ---------------------------------------------------------------------------
# get_executed_stop_fills — #4 and #5. An exit is priced by the broker or it is
# not booked. Nothing here may fall back to a quote.
# ---------------------------------------------------------------------------


def _executed_stop(stop_id: str, exchange_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        stop_order_id=stop_id,
        lots_requested=2,
        figi="BBG000000001",
        ticker="SBER",
        stop_price=decimal_to_money(Decimal("95"), "rub"),
        create_date=NOW,
        order_request_id="",
        exchange_order_id=exchange_id,
        status=SimpleNamespace(name="STOP_ORDER_STATUS_EXECUTED"),
    )


def _fill(price: Decimal, lots: int, commission: Decimal) -> SimpleNamespace:
    return SimpleNamespace(
        order_id="exch-1",
        execution_report_status=(
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
        ),
        lots_requested=lots,
        lots_executed=lots,
        executed_order_price=decimal_to_money(price, "rub"),
        executed_commission=decimal_to_money(commission, "rub"),
        figi="BBG000000001",
        direction=OrderDirection.ORDER_DIRECTION_SELL,
        order_date=NOW,
        order_request_id="",
    )


def _arrange_stops(
    monkeypatch: pytest.MonkeyPatch,
    stops: list[SimpleNamespace],
    fills: dict[str, SimpleNamespace],
    seen: list[dict[str, Any]],
) -> None:
    async def get_stop_orders(self: Any, **kwargs: Any) -> SimpleNamespace:
        seen.append({"call": "get_stop_orders", **kwargs})
        return SimpleNamespace(stop_orders=stops)

    async def get_order_state(self: Any, **kwargs: Any) -> SimpleNamespace:
        seen.append({"call": "get_order_state", **kwargs})
        found = fills.get(str(kwargs.get("order_id")))
        if found is None:
            raise AioRequestError(StatusCode.NOT_FOUND, "no such order", None)
        return found

    monkeypatch.setattr(_Services, "get_stop_orders", get_stop_orders)
    monkeypatch.setattr(_Services, "get_order_state", get_order_state)


async def test_executed_stop_fill_carries_the_brokers_own_numbers(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4: the exit price is the broker's executed price, never a quote."""
    seen: list[dict[str, Any]] = []
    _arrange_stops(
        monkeypatch,
        [_executed_stop("stop-1", "exch-1")],
        {"exch-1": _fill(Decimal("95.00"), 2, Decimal("1.25"))},
        seen,
    )
    fills = await get_executed_stop_fills(NOW - timedelta(hours=8), NOW)

    assert set(fills) == {"stop-1"}
    record = fills["stop-1"]
    assert record.filled_price == Decimal("95.00")
    assert record.filled_lots == 2
    assert record.commission == Decimal("1.25")

    queried = [c for c in seen if c["call"] == "get_stop_orders"][0]
    assert queried["status"] is StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED
    resolved = [c for c in seen if c["call"] == "get_order_state"][0]
    assert resolved["order_id"] == "exch-1"
    assert resolved["order_id_type"] is OrderIdType.ORDER_ID_TYPE_EXCHANGE


async def test_unresolvable_exchange_id_is_omitted_not_guessed(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop we cannot price is left out, so the caller retries."""
    seen: list[dict[str, Any]] = []
    _arrange_stops(
        monkeypatch,
        [_executed_stop("stop-1", "missing"), _executed_stop("stop-2", None)],
        {},
        seen,
    )
    assert await get_executed_stop_fills(NOW - timedelta(hours=8), NOW) == {}


async def test_nothing_executed_returns_an_empty_dict(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    _arrange_stops(monkeypatch, [], {}, seen)
    result = await get_executed_stop_fills(NOW - timedelta(hours=8), NOW)
    assert result == {}
    assert result is not None


async def test_executed_stop_fills_rejects_naive_datetimes(
    capture: _Capture,
) -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        await get_executed_stop_fills(naive, NOW)
    with pytest.raises(ValueError):
        await get_executed_stop_fills(NOW - timedelta(hours=8), naive)
