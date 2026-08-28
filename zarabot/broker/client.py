"""The only module that talks to the broker. Domain types in, domain types out."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from t_tech.invest import AsyncClient
from t_tech.invest.async_services import AsyncServices
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import (  # type: ignore[attr-defined]
    AioRequestError,
    StatusCode,
)
from t_tech.invest.schemas import (
    CandleInterval,
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
from t_tech.invest.utils import decimal_to_quotation

from zarabot import clock, config
from zarabot.config import Config
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
_STATUS_PREFIX = "SECURITY_TRADING_STATUS_"

# The main equity board: 10:00-18:54:59 MSK, weekends closed. Requested by
# name, because the response carries 147 exchanges and 53 of them contain the
# substring "MOEX" — the first match was in practice MOEX_MRNG_EVNG_E_WKND_D,
# an extended session running to 23:49 MSK that reports Saturday and Sunday as
# trading days (#43).
_EXCHANGE = "MOEX"

# The broker measures the calendar horizon from the START OF THE DAY of `from_`,
# not from the instant of the call, and rejects anything longer with
# INVALID_ARGUMENT / 30002. Confirmed against the live account: from=now +14d
# fails, from=now +13d succeeds, from=midnight +14d succeeds (#39).
_MAX_SCHEDULE_DAYS = 14

# A closed day carries 1970-01-01 in both timestamps, and some exchanges report
# is_trading_day=True alongside those epoch values. Either read as a session is
# another way to believe the market is open.
_EPOCH_GUARD = datetime(1971, 1, 1, tzinfo=UTC)

# Only these mean "the broker could not be reached", which is the one condition
# where the caller's retry-with-backoff is the right answer. Every other status
# is a fact about the request, not about the weather (#23).
_TRANSPORT_STATUSES = frozenset({StatusCode.UNAVAILABLE, StatusCode.DEADLINE_EXCEEDED})

_NANO = Decimal("1000000000")

# Only an executed operation is a fact. The rest are intentions or history.
_OPERATION_EXECUTED = "OPERATION_STATE_EXECUTED"

# Every fee the broker can charge, named rather than matched on the substring
# "FEE" in an attribute the domain record did not carry (#11).
_FEE_TYPES = frozenset(
    {
        "OPERATION_TYPE_ADVICE_FEE",
        "OPERATION_TYPE_BROKER_FEE",
        "OPERATION_TYPE_CASH_FEE",
        "OPERATION_TYPE_MARGIN_FEE",
        "OPERATION_TYPE_OTHER_FEE",
        "OPERATION_TYPE_OUT_FEE",
        "OPERATION_TYPE_SERVICE_FEE",
        "OPERATION_TYPE_SUCCESS_FEE",
        "OPERATION_TYPE_TRACK_MFEE",
        "OPERATION_TYPE_TRACK_PFEE",
    }
)

# Cash reaches the portfolio as a currency position. Roubles are RUB000UTSTOM,
# and roubles are the only buying power: the schema constrains instruments to
# RUB (migrations/001_initial.sql).
_RUB_PREFIX = "RUB"

# The only state this module holds: the last accepted price per instrument,
# which is what makes the implausible-move check possible.
_last_accepted: dict[str, Decimal] = {}


class InstrumentNotFound(Exception):
    """Ticker did not resolve at the broker."""


class BrokerUnavailable(Exception):
    """Transport failure talking to the broker."""


class PriceRejected(Exception):
    """A quote arrived but is not usable. Distinct from a transport failure."""


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


@dataclass
class _Connection:
    """One channel and one `Config` for the life of the process (#18)."""

    stack: AsyncExitStack
    services: AsyncServices
    config: Config


_connection: _Connection | None = None
_open_lock = asyncio.Lock()


async def _open() -> _Connection:
    """Return the process client, creating it on first use. Never at import."""
    global _connection
    if _connection is not None:
        return _connection
    async with _open_lock:
        if _connection is None:
            cfg = config.get()
            target = INVEST_GRPC_API_SANDBOX if cfg.trading_mode == "sandbox" else None
            stack = AsyncExitStack()
            services = await stack.enter_async_context(
                AsyncClient(cfg.tinvest_token, target=target)
            )
            _connection = _Connection(stack=stack, services=services, config=cfg)
    return _connection


async def _connect() -> _Connection:
    """Open or reuse the process client, typing a transport failure."""
    try:
        return await _open()
    except AioRequestError as exc:
        _translate(exc, _token(), not_found=None)


async def close() -> None:
    """Close the process client and forget it. Idempotent.

    Called only by `app.shutdown`. A later call creates a new client, so this
    is not a one-way door for a long-lived process that must reconnect.
    """
    global _connection
    conn = _connection
    _connection = None
    if conn is not None:
        await conn.stack.aclose()


def _token() -> str:
    return config.get().tinvest_token


def _redact(text: str, token: str) -> str:
    return text.replace(token, "") if token else text


def _retry_after(metadata: Any) -> Decimal | None:
    """The broker's back-off hint, from whichever shape the SDK hands back."""
    if metadata is None:
        return None
    raw: Any = getattr(metadata, "ratelimit_reset", None)
    if raw is None and isinstance(metadata, dict):
        lowered = {str(key).lower(): value for key, value in metadata.items()}
        raw = (
            lowered.get("retry-after")
            or lowered.get("x-ratelimit-reset")
            or lowered.get("ratelimit-reset")
        )
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _translate(
    exc: AioRequestError, token: str, *, not_found: type[Exception] | None
) -> NoReturn:
    """Type the failure by what it is, never by where it was caught (#23).

    Only a transport failure becomes `BrokerUnavailable`. Everything else that
    is not a rate limit or an expected not-found — `INVALID_ARGUMENT` foremost
    — propagates as itself, because retry-with-backoff is the right answer for
    an outage and useless for a malformed request. `from None` is never used:
    it discards the traceback naming the real fault, and the redaction filter
    is what makes preserving the cause safe.
    """
    if exc.code is StatusCode.RESOURCE_EXHAUSTED:
        raise BrokerRateLimited(_retry_after(exc.metadata)) from exc
    if exc.code is StatusCode.NOT_FOUND and not_found is not None:
        raise not_found(_redact(exc.details or "not found", token)) from exc
    if exc.code in _TRANSPORT_STATUSES:
        name = getattr(exc.code, "name", str(exc.code))
        raise BrokerUnavailable(_redact(f"broker unavailable: {name}", token)) from exc
    raise exc


def _enum_name(raw: object) -> str:
    """The broker's own name for an enum member, whatever shape it arrives in."""
    if raw is None:
        return ""
    return str(getattr(raw, "name", raw))


def _reject_naive(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("datetime must be timezone-aware")


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return None
    return value


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
        # A partial fill is not a fill: the order is still live at the broker
        # and `filled_lots` carries what has filled so far (#10).
        return OrderStatus.SUBMITTED
    if raw is OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW:
        return OrderStatus.SUBMITTED
    return OrderStatus.UNKNOWN


def _side_from_direction(raw: object) -> Side:
    if raw is OrderDirection.ORDER_DIRECTION_SELL:
        return Side.SELL
    return Side.BUY


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
    # _as_decimal, like the money and quote converters beside it, reads
    # units/nano directly. money_to_decimal wants an SDK MoneyProtocol, which
    # this module deliberately does not thread through its own helpers.
    return _as_decimal(raw)


def _decimal_quote(raw: object | None) -> Decimal:
    return _as_decimal(raw)


def _lots_from_quote(raw: object | None) -> int:
    return int(_decimal_quote(raw))


async def get_instrument(ticker: str) -> Instrument:
    conn = await _connect()
    try:
        response = await conn.services.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=_CLASS_CODE,
            id=ticker,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=InstrumentNotFound)
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
    figi: str, interval: CandleInterval, since: datetime, until: datetime
) -> list[Candle]:
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect()
    try:
        response = await conn.services.market_data.get_candles(
            instrument_id=figi,
            from_=since,
            to=until,
            interval=interval,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
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


def _quote_time(raw: object) -> datetime | None:
    moment = getattr(raw, "time", None)
    if moment is None:
        moment = getattr(raw, "timestamp", None)
    return moment if isinstance(moment, datetime) else None


def _reject_quote(
    figi: str, price: Decimal, quoted_at: datetime | None, cfg: Config
) -> None:
    """Validate the quote at the single point prices enter the system."""
    if price <= 0:
        raise PriceRejected("price is not strictly positive")
    if quoted_at is None:
        raise PriceRejected("quote timestamp is missing")
    if _aware(quoted_at) is None:
        # Data from an outside system: unusable, not an internal contract
        # breach, so it is rejected rather than raised as a ValueError.
        raise PriceRejected("quote timestamp is naive")
    if clock.now() - quoted_at > timedelta(seconds=cfg.price_max_age_seconds):
        raise PriceRejected("quote is older than price_max_age_seconds")
    last = _last_accepted.get(figi)
    if last is not None and last > 0:
        move_pct = (abs(price - last) / last) * Decimal(100)
        if move_pct > cfg.price_max_move_pct:
            raise PriceRejected("price moved beyond price_max_move_pct")


async def get_last_price(figi: str) -> Decimal:
    conn = await _connect()
    try:
        response = await conn.services.market_data.get_last_prices(instrument_id=[figi])
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
    prices = list(response.last_prices)
    if not prices:
        # No quote at all is neither of the two documented shapes: nothing
        # arrived to reject, and the broker was plainly reachable. Left as the
        # pre-existing BrokerUnavailable so the caller retries — `reconcile`
        # falls back to the entry price on exactly this type — rather than
        # changing another module's behaviour from inside this one.
        raise BrokerUnavailable("broker returned no quote for this instrument")
    quote = prices[0]
    price = _decimal_quote(quote.price)
    _reject_quote(figi, price, _quote_time(quote), conn.config)
    # A rejected quote never reaches here, so it cannot become the baseline
    # that makes the next implausible value look reasonable.
    _last_accepted[figi] = price
    return price


def _is_currency(raw: object) -> bool:
    return str(getattr(raw, "instrument_type", "")).lower() == "currency"


def _is_rub(raw: object) -> bool:
    """Identify the rouble cash position.

    Not by the price's currency code: a currency position is priced in roubles
    whatever currency it holds, so a dollar balance carries `currency="rub"` on
    `average_position_price` too. The instrument identifier is what separates
    them — roubles are RUB000UTSTOM.
    """
    return any(
        str(getattr(raw, attr, "") or "").upper().startswith(_RUB_PREFIX)
        for attr in ("figi", "ticker")
    )


def _rub_buying_power(response: object) -> Decimal:
    """RUB buying power: the rouble balance less what the exchange has blocked.

    Not `total_amount_currencies`, which is every currency position converted to
    roubles with blocked and reserved funds included. Sizing against that figure
    approves an order the broker then refuses for insufficient funds (#16), and
    the refusal reads as a broker problem rather than a sizing one. The response
    separates available from blocked per position, so available wins.

    No rouble position means no cash. Under-reporting costs a trade; the
    over-report this replaces costs a rejected order.
    """
    available = Decimal(0)
    for raw in getattr(response, "positions", None) or ():
        if not _is_currency(raw) or not _is_rub(raw):
            continue
        held = _decimal_quote(getattr(raw, "quantity", None))
        blocked = _decimal_quote(getattr(raw, "blocked_lots", None))
        available += held - blocked
    return max(available, Decimal(0))


async def get_portfolio() -> PortfolioState:
    conn = await _connect()
    cfg = conn.config
    try:
        response = await conn.services.operations.get_portfolio(
            account_id=cfg.tinvest_account_id
        )
    except AioRequestError as exc:
        _translate(exc, cfg.tinvest_token, not_found=None)
    cash = _rub_buying_power(response)
    holdings: list[Position] = []
    for raw in response.positions:
        lots = _lots_from_quote(getattr(raw, "quantity_lots", None))
        if lots <= 0:
            continue
        if _is_currency(raw):
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


def _session_from_day(day: object) -> SessionInfo:
    """One calendar day. A day that is not a session is marked, never invented."""
    start = _aware(getattr(day, "start_time", None))
    end = _aware(getattr(day, "end_time", None))
    trading = bool(getattr(day, "is_trading_day", False))
    if start is None or end is None or start < _EPOCH_GUARD or end < _EPOCH_GUARD:
        # 1970-01-01 in both is what a closed day carries; believing it would
        # be another way to believe the market is open.
        trading = False
    if not trading:
        return SessionInfo(start=None, end=None, is_trading_day=False)
    return SessionInfo(start=start, end=end, is_trading_day=True)


def _day_start() -> datetime:
    """Midnight of the current UTC day.

    The broker measures the calendar horizon from the start of the day of
    `from_`, never from the instant of the call (#39), so every schedule range
    is anchored here.
    """
    return (
        clock.now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    )


def _check_horizon(days: int) -> None:
    if days > _MAX_SCHEDULE_DAYS:
        raise ValueError(
            f"days must not exceed {_MAX_SCHEDULE_DAYS}; the broker rejects a "
            "longer horizon with INVALID_ARGUMENT / 30002"
        )


async def _schedule_range(start: datetime, until: datetime) -> list[SessionInfo]:
    conn = await _connect()
    try:
        response = await conn.services.instruments.trading_schedules(
            exchange=_EXCHANGE, from_=start, to=until
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
    sessions: list[SessionInfo] = []
    for exchange in response.exchanges:
        if str(exchange.exchange).upper() != _EXCHANGE:
            continue
        sessions.extend(_session_from_day(day) for day in exchange.days)
    return sessions


async def get_trading_schedule(days: int) -> list[SessionInfo]:
    """The next `days` days, from the start of the current UTC day."""
    _check_horizon(days)
    start = _day_start()
    return await _schedule_range(start, start + timedelta(days=days))


async def get_past_trading_schedule(days: int) -> list[SessionInfo]:
    """The `days` days ENDING at the start of the current UTC day.

    `get_trading_schedule` looks forward, which is right for "is the market
    open" and wrong for "how long has this position been held":
    `clock.trading_days_between` counts only dates the calendar contains, so
    every day between an entry and yesterday fell outside it and
    `trading_days_open` was capped at 1 (#45).
    """
    _check_horizon(days)
    until = _day_start()
    return await _schedule_range(until - timedelta(days=days), until)


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
    conn = await _connect()
    direction = (
        OrderDirection.ORDER_DIRECTION_BUY
        if side is Side.BUY
        else OrderDirection.ORDER_DIRECTION_SELL
    )
    try:
        response = await conn.services.orders.post_order(
            instrument_id=figi,
            quantity=lots,
            direction=direction,
            account_id=conn.config.tinvest_account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=key,
            # Never True. The brief's no-leverage guarantee is what bounds the
            # maximum loss to the allocated capital, and this is the one place
            # it can be broken.
            confirm_margin_trade=False,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
    status = _order_status(response.execution_report_status)
    reason = _redact(getattr(response, "message", "") or "", conn.config.tinvest_token)
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
    conn = await _connect()
    try:
        response = await conn.services.stop_orders.post_stop_order(
            instrument_id=figi,
            quantity=lots,
            stop_price=decimal_to_quotation(stop_price),
            direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
            account_id=conn.config.tinvest_account_id,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            # Good-till-cancel: a day-expiring stop would stop protecting the
            # position overnight, which is precisely when it is needed.
            expiration_type=(
                StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL
            ),
            exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            order_id=key,
            confirm_margin_trade=False,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
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
    conn = await _connect()
    try:
        await conn.services.stop_orders.cancel_stop_order(
            account_id=conn.config.tinvest_account_id, stop_order_id=stop_order_id
        )
    except AioRequestError as exc:
        # The executor calls this while racing the exchange, so an
        # already-cancelled or already-executed stop order is not an error.
        if exc.code is StatusCode.NOT_FOUND:
            return
        _translate(exc, conn.config.tinvest_token, not_found=None)


async def cancel_order(key: str) -> None:
    """Cancel a live ordinary order by our own idempotency key.

    Idempotent in the same sense as `cancel_stop_order`: the caller is racing
    the exchange by definition, so an order already filled, already cancelled
    or unknown here is not an error. The authoritative answer comes from the
    `get_order_state` that follows, never from this call's outcome (#10).
    """
    conn = await _connect()
    try:
        await conn.services.orders.cancel_order(
            account_id=conn.config.tinvest_account_id,
            # By the client key: after a crash the exchange identifier is
            # precisely what was lost.
            order_id=key,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
        )
    except AioRequestError as exc:
        if exc.code is StatusCode.NOT_FOUND:
            return
        _translate(exc, conn.config.tinvest_token, not_found=None)


def _stop_status(raw: object) -> StopOrderStatus:
    name = getattr(raw, "name", str(raw))
    if "EXECUTED" in name:
        return StopOrderStatus.EXECUTED
    if "CANCELED" in name or "CANCELLED" in name or "EXPIRED" in name:
        return StopOrderStatus.CANCELLED
    return StopOrderStatus.ACTIVE


async def list_stop_orders() -> list[StopOrderRecord]:
    conn = await _connect()
    try:
        response = await conn.services.stop_orders.get_stop_orders(
            account_id=conn.config.tinvest_account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
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


async def get_executed_stop_fills(
    since: datetime, until: datetime
) -> dict[str, OrderRecord]:
    """The broker's own record of every stop that fired in the window.

    Keyed by `stop_order_id`. Two SDK calls composed here because this is the
    only module permitted to talk to the broker: executed stops, then each
    one's `exchange_order_id` resolved to the actual fill. A stop whose
    exchange order does not resolve is omitted — the caller retries rather than
    booking a price nobody reported (rule 33).
    """
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect()
    try:
        response = await conn.services.stop_orders.get_stop_orders(
            account_id=conn.config.tinvest_account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED,
            from_=since,
            to=until,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)

    fills: dict[str, OrderRecord] = {}
    for raw in response.stop_orders:
        stop_id = getattr(raw, "stop_order_id", "") or ""
        exchange_id = getattr(raw, "exchange_order_id", "") or ""
        if not stop_id or not exchange_id:
            continue
        try:
            state = await conn.services.orders.get_order_state(
                account_id=conn.config.tinvest_account_id,
                order_id=exchange_id,
                order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
            )
        except AioRequestError:
            # Not yet settled, or not resolvable. Omit it: the position stays
            # open and the caller asks again next cycle.
            continue
        filled = state.lots_executed or None
        if not filled:
            continue
        fills[stop_id] = _order_record(
            key=exchange_id,
            figi=state.figi,
            side=Side.SELL,
            lots=int(state.lots_requested),
            status=_order_status(state.execution_report_status),
            filled_lots=int(filled),
            filled_price=_decimal_money(state.executed_order_price),
            commission=_executed_commission(
                getattr(state, "executed_commission", None)
            ),
            broker_reason=None,
            created_at=getattr(state, "order_date", None) or clock.now(),
            settled_at=getattr(state, "order_date", None) or clock.now(),
        )
    return fills


async def get_max_lots(figi: str) -> int:
    conn = await _connect()
    request = GetMaxLotsRequest(
        account_id=conn.config.tinvest_account_id, instrument_id=figi
    )
    try:
        response = await conn.services.orders.get_max_lots(request)
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
    # The non-margin limit. buy_margin_limits is never read.
    return int(response.buy_limits.buy_max_market_lots)


async def get_operations(since: datetime, until: datetime) -> list[OperationRecord]:
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect()
    try:
        response = await conn.services.operations.get_operations(
            account_id=conn.config.tinvest_account_id, from_=since, to=until
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=None)
    records: list[OperationRecord] = []
    for raw in response.operations:
        state = _enum_name(getattr(raw, "state", None))
        if state != _OPERATION_EXECUTED:
            # A cancelled or still-progressing operation is not something that
            # happened; counting one as a cost or as a sale is the same error.
            continue
        payment = _decimal_money(raw.payment)
        operation_type = _enum_name(getattr(raw, "operation_type", None))
        commission = abs(payment) if operation_type in _FEE_TYPES else Decimal(0)
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
                operation_type=operation_type,
                state=state,
                parent_operation_id=getattr(raw, "parent_operation_id", "") or None,
            )
        )
    return records


async def get_order_state(key: str) -> OrderRecord:
    conn = await _connect()
    try:
        response = await conn.services.orders.get_order_state(
            account_id=conn.config.tinvest_account_id,
            # By the client key alone: after a crash the exchange identifier is
            # precisely what was lost.
            order_id=key,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=OrderNotFound)
    return _state_to_record(key, response)


async def get_order_state_by_broker_id(broker_order_id: str) -> OrderRecord:
    """The same lookup, by the broker's own identifier instead of our key.

    A stop the exchange fired is recorded locally under an idempotency key this
    bot invented, so `get_order_state` on that key can only ever return
    `OrderNotFound` — which made the commission on every stop exit permanently
    unrecoverable (#8). Separate from `get_order_state` because the two answer
    different questions: "what happened to the order I sent" is a recovery
    path, "what happened to the order the exchange placed for me" is not.
    """
    conn = await _connect()
    try:
        response = await conn.services.orders.get_order_state(
            account_id=conn.config.tinvest_account_id,
            order_id=broker_order_id,
            order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, not_found=OrderNotFound)
    # Keyed by what it was asked about: this module does not know the local
    # row's key and must not invent one.
    return _state_to_record(broker_order_id, response)


def _state_to_record(key: str, response: Any) -> OrderRecord:
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
