"""The only module that talks to the broker. Domain types in, domain types out."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

from t_tech.invest import AsyncClient
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import (  # type: ignore[attr-defined]
    AioRequestError,
    StatusCode,
)
from t_tech.invest.schemas import (
    ExchangeOrderType,
    GetMaxLotsRequest,
    InstrumentIdType,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderIdType,
    OrderType,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderStatusOption,
    StopOrderType,
)
from t_tech.invest.utils import decimal_to_quotation, money_to_decimal

from zarabot import clock, config
from zarabot.models import (
    Candle,
    Instrument,
    OperationRecord,
    OrderRecord,
    OrderStatus,
    PortfolioState,
    Position,
    SessionInfo,
    Side,
    StopOrderRecord,
    StopOrderStatus,
    StopProtection,
)

_CLASS_CODE = "TQBR"
_EXCHANGE_HINT = "MOEX"
_STATUS_PREFIX = "SECURITY_TRADING_STATUS_"


class InstrumentNotFound(Exception):
    """Ticker did not resolve at the broker."""


class BrokerUnavailable(Exception):
    """Transport failure talking to the broker."""


class BrokerRateLimited(Exception):
    """Broker throttled the call. `retry_after` is the hint when present."""

    def __init__(self, retry_after: Decimal | None = None) -> None:
        super().__init__("broker rate limited")
        self.retry_after = retry_after


class OrderRejected(Exception):
    """Broker refused the order. `reason` is the broker's string."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StopOrderRejected(Exception):
    """Broker refused the stop order. `reason` is the broker's string."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OrderNotFound(Exception):
    """Broker has no record for this idempotency key — the order was never accepted."""


def _redact(text: str, token: str) -> str:
    return text.replace(token, "") if token else text


def _retry_after(metadata: Any) -> Decimal | None:
    if metadata is None:
        return None
    mapping: dict[str, Any]
    if isinstance(metadata, dict):
        mapping = {str(key).lower(): value for key, value in metadata.items()}
    else:
        try:
            mapping = {str(key).lower(): value for key, value in metadata}
        except TypeError:
            return None
    raw = mapping.get("retry-after") or mapping.get("ratelimit-reset")
    if raw is None:
        return None
    return Decimal(str(raw))


def _translate(
    exc: BaseException, token: str, *, not_found: type[Exception] | None
) -> NoReturn:
    if isinstance(exc, AioRequestError):
        if exc.code is StatusCode.RESOURCE_EXHAUSTED:
            raise BrokerRateLimited(_retry_after(exc.metadata)) from None
        if exc.code is StatusCode.NOT_FOUND and not_found is not None:
            raise not_found(_redact(exc.details or "not found", token)) from None
        raise BrokerUnavailable(_redact("broker unavailable", token)) from None
    raise BrokerUnavailable(_redact("broker unavailable", token)) from None


def _connect() -> AsyncClient:
    cfg = config.load()
    if cfg.trading_mode == "sandbox":
        return AsyncClient(cfg.tinvest_token, target=INVEST_GRPC_API_SANDBOX)
    return AsyncClient(cfg.tinvest_token)


def _token() -> str:
    return config.load().tinvest_token


def _account() -> str:
    return config.load().tinvest_account_id


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _trading_status(raw: object) -> str:
    name = getattr(raw, "name", str(raw))
    if name.startswith(_STATUS_PREFIX):
        return name[len(_STATUS_PREFIX) :]
    return str(name)


def _order_status(raw: object) -> OrderStatus:
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL:
        return OrderStatus.FILLED
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED:
        return OrderStatus.REJECTED
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED:
        return OrderStatus.CANCELLED
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL:
        return OrderStatus.FILLED
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW:
        return OrderStatus.SUBMITTED
    return OrderStatus.UNKNOWN


def _side_from_direction(raw: object) -> Side:
    if raw is OrderDirection.ORDER_DIRECTION_SELL:
        return Side.SELL
    return Side.BUY


_NANO = Decimal("1000000000")


def _as_decimal(raw: object | None) -> Decimal:
    if raw is None:
        return Decimal(0)
    units = getattr(raw, "units", None)
    nano = getattr(raw, "nano", None)
    if units is None:
        return Decimal(0)
    return Decimal(units) + Decimal(nano or 0) / _NANO


def _decimal_money(raw: object | None) -> Decimal:
    return _as_decimal(raw)


def _executed_commission(raw: object | None) -> Decimal | None:
    if raw is None:
        return None
    return money_to_decimal(raw)


def _decimal_quote(raw: object | None) -> Decimal:
    return _as_decimal(raw)


def _lots_from_quote(raw: object | None) -> int:
    return int(_decimal_quote(raw))


async def get_instrument(ticker: str) -> Instrument:
    token = _token()
    try:
        async with _connect() as client:
            response = await client.instruments.share_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                class_code=_CLASS_CODE,
                id=ticker,
            )
    except Exception as exc:
        _translate(exc, token, not_found=InstrumentNotFound)
    share = response.instrument
    return Instrument(
        figi=share.figi,
        ticker=share.ticker,
        lot=share.lot,
        min_price_increment=_decimal_quote(share.min_price_increment),
        currency=str(share.currency).upper(),
        trading_status=_trading_status(share.trading_status),
        refreshed_at=clock.now(),
    )


async def get_candles(
    figi: str, interval: object, since: datetime, until: datetime
) -> list[Candle]:
    _reject_naive(since)
    _reject_naive(until)
    token = _token()
    try:
        async with _connect() as client:
            response = await client.market_data.get_candles(
                instrument_id=figi,
                from_=since,
                to=until,
                interval=interval,
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    candles = [
        Candle(
            timestamp=item.time,
            open=_decimal_quote(item.open),
            high=_decimal_quote(item.high),
            low=_decimal_quote(item.low),
            close=_decimal_quote(item.close),
            volume=item.volume,
        )
        for item in response.candles
    ]
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


async def get_last_price(figi: str) -> Decimal:
    token = _token()
    try:
        async with _connect() as client:
            response = await client.market_data.get_last_prices(instrument_id=[figi])
    except Exception as exc:
        _translate(exc, token, not_found=None)
    prices = list(response.last_prices)
    if not prices:
        raise BrokerUnavailable("broker unavailable")
    return _decimal_quote(prices[0].price)


async def get_portfolio() -> PortfolioState:
    token = _token()
    cfg = config.load()
    try:
        async with _connect() as client:
            response = await client.operations.get_portfolio(
                account_id=cfg.tinvest_account_id
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    cash = _decimal_money(response.total_amount_currencies)
    holdings: list[Position] = []
    for raw in response.positions:
        lots = _lots_from_quote(getattr(raw, "quantity_lots", None))
        if lots <= 0:
            continue
        if str(getattr(raw, "instrument_type", "")).lower() == "currency":
            continue
        entry = _decimal_money(raw.average_position_price)
        ticker = getattr(raw, "ticker", "") or ""
        figi = raw.figi
        stop = entry * (Decimal(100) - cfg.stop_loss_pct) / Decimal(100)
        target = entry * (Decimal(100) + cfg.take_profit_pct) / Decimal(100)
        quantity = _decimal_quote(raw.quantity)
        lot_size = int(quantity / lots) if lots else 1
        holdings.append(
            Position(
                id=0,
                ticker=ticker,
                figi=figi,
                strategy="ADOPTED",
                lots=lots,
                lot_size=max(lot_size, 1),
                entry_price=entry,
                entry_at=clock.now(),
                stop_price=stop,
                target_price=target,
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
        )
    return PortfolioState(cash=cash, positions=tuple(holdings))


async def get_trading_schedule(days: int) -> list[SessionInfo]:
    token = _token()
    start = clock.now()
    until = start + timedelta(days=days)
    try:
        async with _connect() as client:
            response = await client.instruments.trading_schedules(from_=start, to=until)
    except Exception as exc:
        _translate(exc, token, not_found=None)
    sessions: list[SessionInfo] = []
    for exchange in response.exchanges:
        if _EXCHANGE_HINT not in str(exchange.exchange).upper():
            continue
        for day in exchange.days:
            sessions.append(
                SessionInfo(
                    start=day.start_time if day.is_trading_day else None,
                    end=day.end_time if day.is_trading_day else None,
                    is_trading_day=bool(day.is_trading_day),
                )
            )
        break
    return sessions


def _order_record(
    *,
    key: str,
    figi: str,
    side: Side,
    lots: int,
    status: OrderStatus,
    filled_lots: int | None,
    filled_price: Decimal | None,
    commission: Decimal | None,
    broker_reason: str | None,
    created_at: datetime,
    settled_at: datetime | None,
) -> OrderRecord:
    intent = "ENTRY" if side is Side.BUY else "EXIT"
    return OrderRecord(
        key=key,
        ticker="",
        figi=figi,
        side=side,
        intent=intent,
        lots=lots,
        status=status,
        filled_lots=filled_lots,
        filled_price=filled_price,
        commission=commission,
        broker_reason=broker_reason,
        created_at=created_at,
        settled_at=settled_at,
    )


async def post_market_order(key: str, figi: str, side: Side, lots: int) -> OrderRecord:
    token = _token()
    direction = (
        OrderDirection.ORDER_DIRECTION_BUY
        if side is Side.BUY
        else OrderDirection.ORDER_DIRECTION_SELL
    )
    try:
        async with _connect() as client:
            response = await client.orders.post_order(
                instrument_id=figi,
                quantity=lots,
                direction=direction,
                account_id=_account(),
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=key,
                confirm_margin_trade=False,
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    status = _order_status(response.execution_report_status)
    reason = _redact(getattr(response, "message", "") or "", token)
    if status is OrderStatus.REJECTED:
        raise OrderRejected(reason or "rejected")
    filled = response.lots_executed or None
    price = _decimal_money(response.executed_order_price) if filled else None
    commission = (
        _executed_commission(getattr(response, "executed_commission", None))
        if filled
        else None
    )
    now = clock.now()
    return _order_record(
        key=key,
        figi=figi,
        side=side,
        lots=lots,
        status=status,
        filled_lots=filled,
        filled_price=price,
        commission=commission,
        broker_reason=reason or None,
        created_at=now,
        settled_at=now if status is OrderStatus.FILLED else None,
    )


async def post_stop_loss(
    key: str, figi: str, lots: int, stop_price: Decimal
) -> StopOrderRecord:
    token = _token()
    try:
        async with _connect() as client:
            response = await client.stop_orders.post_stop_order(
                instrument_id=figi,
                quantity=lots,
                stop_price=decimal_to_quotation(stop_price),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
                account_id=_account(),
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                expiration_type=(
                    StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL
                ),
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
                order_id=key,
                confirm_margin_trade=False,
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    stop_id = getattr(response, "stop_order_id", None) or None
    if not stop_id:
        raise StopOrderRejected("rejected")
    return StopOrderRecord(
        key=key,
        stop_order_id=stop_id,
        position_id=0,
        ticker="",
        lots=lots,
        stop_price=stop_price,
        status=StopOrderStatus.ACTIVE,
        created_at=clock.now(),
        settled_at=None,
    )


async def cancel_stop_order(stop_order_id: str) -> None:
    token = _token()
    try:
        async with _connect() as client:
            await client.stop_orders.cancel_stop_order(
                account_id=_account(), stop_order_id=stop_order_id
            )
    except Exception as exc:
        if isinstance(exc, AioRequestError) and exc.code is StatusCode.NOT_FOUND:
            return
        _translate(exc, token, not_found=None)


def _stop_status(raw: object) -> StopOrderStatus:
    name = getattr(raw, "name", str(raw))
    if "EXECUTED" in name:
        return StopOrderStatus.EXECUTED
    if "CANCELED" in name or "CANCELLED" in name or "EXPIRED" in name:
        return StopOrderStatus.CANCELLED
    if "ACTIVE" in name:
        return StopOrderStatus.ACTIVE
    return StopOrderStatus.ACTIVE


async def list_stop_orders() -> list[StopOrderRecord]:
    token = _token()
    try:
        async with _connect() as client:
            response = await client.stop_orders.get_stop_orders(
                account_id=_account(),
                status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    records: list[StopOrderRecord] = []
    for raw in response.stop_orders:
        lots = int(raw.lots_requested)
        if lots <= 0:
            continue
        key = getattr(raw, "order_request_id", "") or raw.stop_order_id
        records.append(
            StopOrderRecord(
                key=key,
                stop_order_id=raw.stop_order_id,
                position_id=0,
                ticker=getattr(raw, "ticker", "") or "",
                lots=lots,
                stop_price=_decimal_money(raw.stop_price),
                status=_stop_status(getattr(raw, "status", None)),
                created_at=raw.create_date,
                settled_at=None,
            )
        )
    return records


async def get_max_lots(figi: str) -> int:
    token = _token()
    request = GetMaxLotsRequest(account_id=_account(), instrument_id=figi)
    try:
        async with _connect() as client:
            response = await client.orders.get_max_lots(request)
    except Exception as exc:
        _translate(exc, token, not_found=None)
    return int(response.buy_limits.buy_max_market_lots)


async def get_operations(since: datetime, until: datetime) -> list[OperationRecord]:
    _reject_naive(since)
    _reject_naive(until)
    token = _token()
    try:
        async with _connect() as client:
            response = await client.operations.get_operations(
                account_id=_account(), from_=since, to=until
            )
    except Exception as exc:
        _translate(exc, token, not_found=None)
    records: list[OperationRecord] = []
    for raw in response.operations:
        payment = _decimal_money(raw.payment)
        name = getattr(getattr(raw, "operation_type", None), "name", "")
        commission = abs(payment) if "FEE" in name else Decimal(0)
        price_raw = getattr(raw, "price", None)
        price = _decimal_money(price_raw) if price_raw is not None else None
        quantity = getattr(raw, "quantity", None)
        records.append(
            OperationRecord(
                id=raw.id,
                figi=raw.figi,
                ticker="",
                occurred_at=raw.date,
                commission=commission,
                payment=payment,
                price=price,
                quantity=quantity,
            )
        )
    return records


async def get_order_state(key: str) -> OrderRecord:
    token = _token()
    try:
        async with _connect() as client:
            response = await client.orders.get_order_state(
                account_id=_account(),
                order_id=key,
                order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
            )
    except Exception as exc:
        _translate(exc, token, not_found=OrderNotFound)
    side = _side_from_direction(response.direction)
    status = _order_status(response.execution_report_status)
    filled = response.lots_executed or None
    created = getattr(response, "order_date", None) or clock.now()
    return _order_record(
        key=key,
        figi=response.figi,
        side=side,
        lots=response.lots_requested,
        status=status,
        filled_lots=filled,
        filled_price=_decimal_money(response.executed_order_price) if filled else None,
        commission=(
            _executed_commission(getattr(response, "executed_commission", None))
            if filled
            else None
        ),
        broker_reason=None,
        created_at=created,
        settled_at=created if status is OrderStatus.FILLED else None,
    )
