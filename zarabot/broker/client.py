"""The only module that talks to the broker. Domain types in, domain types out."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

import aiosqlite
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
from zarabot.db import connection as db_connection
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

# Fresh means the five cacheable columns were read within this window; anything
# else is stale, and an absent row is stale. A module constant rather than a
# `config` value: it is not an operator-tunable risk parameter, and the number
# is chosen against the thing that invalidates the row. A corporate action — a
# lot-size change, a step change, a ticker reassignment — carries days of
# notice, so one day sits inside it, while a shorter window would buy nothing
# and a longer one would let a lot-size change survive a night. A watchlist
# ticker is read many times a session, so the window is crossed once a day per
# ticker and never mid-session (spec v1.85, #46).
_INSTRUMENT_MAX_AGE = timedelta(hours=24)

# The lookup is by ticker, which is what `get_instrument` is given. The upsert
# conflicts on that same column rather than on the `figi` primary key: a ticker
# reassignment is the corporate action that changes one without the other, and
# resolving it on the lookup key means the row that is SERVED is always the row
# that was just rewritten.
_INSTRUMENT_SELECT = (
    "SELECT figi, ticker, lot, min_price_increment, currency, refreshed_at "
    "FROM instruments WHERE ticker = ?"
)
_INSTRUMENT_UPSERT = (
    "INSERT INTO instruments "
    "(figi, ticker, lot, min_price_increment, currency, trading_status, "
    "refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(ticker) DO UPDATE SET "
    "figi = excluded.figi, "
    "lot = excluded.lot, "
    "min_price_increment = excluded.min_price_increment, "
    "currency = excluded.currency, "
    "trading_status = excluded.trading_status, "
    "refreshed_at = excluded.refreshed_at"
)

# The only state this module holds: the last accepted price per instrument,
# which is what makes the implausible-move check possible.
_last_accepted: dict[str, Decimal] = {}

# Process-local consecutive `BrokerUnavailable` counts, keyed by public method
# name. Cleared when that method succeeds.
_consecutive_failures: dict[str, int] = {}
_log = logging.getLogger(__name__)


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


async def _connect(*, method: str) -> _Connection:
    """Open or reuse the process client, typing a transport failure."""
    try:
        return await _open()
    except AioRequestError as exc:
        _translate(exc, _token(), method=method, not_found=None)


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


def _note_success(method: str) -> None:
    _consecutive_failures.pop(method, None)


def _raise_unavailable(
    method: str, message: str, cause: BaseException | None = None
) -> NoReturn:
    count = _consecutive_failures.get(method, 0) + 1
    _consecutive_failures[method] = count
    _log.warning(
        "broker unavailable",
        extra={
            "event": "broker_unavailable",
            "method": method,
            "consecutive_failures": count,
        },
    )
    if cause is None:
        raise BrokerUnavailable(message)
    raise BrokerUnavailable(message) from cause


def _raise_rate_limited(
    method: str, retry_after: Decimal | None, cause: BaseException
) -> NoReturn:
    _log.warning(
        "broker rate limited",
        extra={
            "event": "rate_limited",
            "method": method,
            "retry_after_seconds": retry_after,
        },
    )
    raise BrokerRateLimited(retry_after) from cause


def _translate(
    exc: AioRequestError,
    token: str,
    *,
    method: str,
    not_found: type[Exception] | None,
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
        _raise_rate_limited(method, _retry_after(exc.metadata), exc)
    if exc.code is StatusCode.NOT_FOUND and not_found is not None:
        raise not_found(_redact(exc.details or "not found", token)) from exc
    if exc.code in _TRANSPORT_STATUSES:
        name = getattr(exc.code, "name", str(exc.code))
        _raise_unavailable(method, _redact(f"broker unavailable: {name}", token), exc)
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
    """The broker's commission for one order, or `None` where it has not said.

    **A zero here is an absence, not a measurement (spec v1.91, #246).** The
    order state reports `executed_commission` as zero on every fill — 22 of 22
    on the live account, against 22 fee operations totalling 14.67 for the same
    trades — because the fee posts a second later as a separate operation. The
    value written was `Decimal(0)`, `db.orders.list_missing_commission` selects
    `commission IS NULL`, and so the backfill built to recover exactly this was
    never shown one of those rows. Every realised P&L the bot produced was
    gross, and nothing anywhere said so.

    `None` is what "the broker has not told us" looks like, and it is what makes
    the row visible to `ops.commissions`, which resolves it from the operations
    feed. A genuinely commission-free trade is recorded unknown here too and is
    settled there as a *measured* zero, terminally — it does not alert forever.

    Nothing is estimated by this: the arbiter is still the broker.
    """
    if raw is None:
        return None
    # _as_decimal, like the money and quote converters beside it, reads
    # units/nano directly. money_to_decimal wants an SDK MoneyProtocol, which
    # this module deliberately does not thread through its own helpers.
    value = _as_decimal(raw)
    return None if value == 0 else value


def _trade_ids(raw: object | None) -> tuple[str, ...]:
    """Execution identifiers, verbatim and in order; `()` where there are none.

    `OrderState.stages[].trade_id` and `Operation.trades[].trade_id` are the
    same identifier on the two sides of the join `ops.commissions` uses to tie a
    fee to the order that incurred it (#246). An empty tuple means "no join
    material", never "no trades".
    """
    if raw is None:
        return ()
    ids: list[str] = []
    for item in raw:  # type: ignore[attr-defined]
        trade_id = getattr(item, "trade_id", None)
        if trade_id:
            ids.append(str(trade_id))
    return tuple(ids)


def _decimal_quote(raw: object | None) -> Decimal:
    return _as_decimal(raw)


def _lots_from_quote(raw: object | None) -> int:
    return int(_decimal_quote(raw))


@dataclass(frozen=True)
class _CachedInstrument:
    """The five cacheable columns, plus the instant they were read.

    `trading_status` is deliberately absent. The column exists and is written,
    so the row is a truthful record of one read; nothing reads it back.
    """

    figi: str
    ticker: str
    lot: int
    min_price_increment: Decimal
    currency: str
    refreshed_at: datetime


async def _cached_instrument(ticker: str, now: datetime) -> _CachedInstrument | None:
    """The stored dimensions for `ticker`, or `None` when absent or stale.

    Catches `DatabaseNotOpenError` and nothing wider. `db.connection.shared()`
    raises it before `app.startup` step 3 and in every process that never opens
    a database — `sandbox/data.py` and `scripts/research/backtest.py` both call
    `get_instrument` on a laptop with no bot database — so the cache is skipped
    and the live read is unaffected. That is not a degraded state and raises no
    alert; narrowing the catch to that one class is what keeps a genuine
    database fault loud (failure class 5).

    It cannot hide a missing startup step 3 either: step 5 writes
    `db.trading_days` and step 6 reads `db.orders`, both before the first
    `get_instrument` any process makes, and neither catches rule 30.
    """
    try:
        conn = db_connection.shared()
    except db_connection.DatabaseNotOpenError:
        _log.debug("no database open, so the instruments cache is skipped")
        return None
    cursor = await conn.execute(_INSTRUMENT_SELECT, (ticker,))
    row = await cursor.fetchone()
    if row is None:
        return None
    refreshed_at = datetime.fromisoformat(str(row["refreshed_at"]))
    if now - refreshed_at > _INSTRUMENT_MAX_AGE:
        return None
    return _CachedInstrument(
        figi=str(row["figi"]),
        ticker=str(row["ticker"]),
        lot=int(row["lot"]),
        min_price_increment=Decimal(str(row["min_price_increment"])),
        currency=str(row["currency"]),
        refreshed_at=refreshed_at,
    )


async def _store_instrument(instrument: Instrument) -> None:
    """Write the row through, and never fail the read for it (rule 12).

    On `aiosqlite.Error` the failure is logged at ERROR and swallowed: the
    caller asked for metadata, not for a cache, and the next call for that
    ticker simply misses again. No alert is raised and none is wanted — a cache
    that is not filling degrades nothing an operator can act on, and an alert
    per cycle in a channel whose premise is that silence means healthy is
    equivalent to no alert (failure class 14).

    Every other exception propagates, `TypeError` and `AttributeError`
    foremost, exactly as rule 12 was narrowed in v1.75: an analytics path is
    where a dropped programming error survives longest.
    """
    try:
        async with db_connection.transaction(critical=False) as conn:
            await conn.execute(
                _INSTRUMENT_UPSERT,
                (
                    instrument.figi,
                    instrument.ticker,
                    instrument.lot,
                    str(instrument.min_price_increment),
                    instrument.currency,
                    instrument.trading_status,
                    instrument.refreshed_at.isoformat(),
                ),
            )
    except db_connection.DatabaseNotOpenError:
        _log.debug("no database open, so the instruments cache is skipped")
    except aiosqlite.Error:
        _log.exception("could not record the instrument in the cache")


async def _live_trading_status(figi: str) -> str:
    """The instrument's trading status, read from the broker, on every call.

    Never served from the table, whatever the row's age. The field changes
    intraday — a volatility halt moves an instrument into a break or a discrete
    auction inside one cycle — and `risk.gate` rejects `INSTRUMENT_NOT_TRADING`
    on it, so a stored `NORMAL_TRADING` is the bot sizing and submitting an
    entry into a halt. §2.1 measured it session-dependent rather than
    instrument-dependent: every watchlist ticker reads `DEALER_NORMAL_TRADING`
    after the main session closes, so a row refreshed in the evening would
    serve that into the next morning's gate and reject every ticker silently
    until it aged out (#46).

    V13 measured this endpoint against the live account on SDK 1.49.1: it
    resolves every watchlist FIGI, derives the same string `share_by` derives
    for the same instrument in the same minute, and has 120x headroom over one
    read per ticker per poll.

    A figi that no longer resolves raises `InstrumentNotFound`, the same
    exception `share_by` would raise for the ticker, so rule 8's mid-session
    skip handles it unchanged.
    """
    conn = await _connect(method="get_instrument")
    try:
        response = await conn.services.market_data.get_trading_status(figi=figi)
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_instrument",
            not_found=InstrumentNotFound,
        )
    return _trading_status(response.trading_status)


async def _fetch_instrument(ticker: str, now: datetime) -> Instrument:
    """One `share_by`, whose response carries every field including the status."""
    conn = await _connect(method="get_instrument")
    try:
        response = await conn.services.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=_CLASS_CODE,
            id=ticker,
        )
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_instrument",
            not_found=InstrumentNotFound,
        )
    share = response.instrument
    _note_success("get_instrument")
    return Instrument(
        figi=share.figi,
        ticker=share.ticker,
        lot=share.lot,
        min_price_increment=_decimal_quote(share.min_price_increment),
        currency=str(share.currency).upper(),
        trading_status=_trading_status(share.trading_status),
        refreshed_at=now,
    )


async def get_instrument(ticker: str) -> Instrument:
    """Instrument metadata: the dimensions from the cache, the status never.

    Exactly one broker request, never two. A fresh row supplies `figi`,
    `ticker`, `lot`, `min_price_increment` and `currency`, and the status is
    read live for that figi; a stale or absent row costs one `share_by`, whose
    response supplies every field, and is written through before the
    `Instrument` is returned.

    A cache miss is not an error. A ticker absent from `instruments`, or
    present and stale, is the ordinary first read of that ticker: fetch, store,
    return. There is no preload step and no background refresher — a periodic
    job is one more supervised loop and one more schedule a restart can step
    over (failure class 15), for a value only ever wanted at the moment it is
    used. A miss at startup and a miss mid-cycle go through this one function
    and neither refuses.

    A broker failure is never answered from the table. When `share_by` fails on
    a miss, or the status read fails on a hit, the typed exception is raised
    and rule 8's mid-session skip and rule 9's absorption in `market.data`
    handle it as they do today. A caller that skips a ticker for one cycle is
    correct; a caller sizing an entry on a status nobody confirmed is not, and
    outage survival is the one use this durable cache forbids.
    """
    now = clock.now()
    cached = await _cached_instrument(ticker, now)
    if cached is not None:
        status = await _live_trading_status(cached.figi)
        _note_success("get_instrument")
        return Instrument(
            figi=cached.figi,
            ticker=cached.ticker,
            lot=cached.lot,
            min_price_increment=cached.min_price_increment,
            currency=cached.currency,
            trading_status=status,
            # The instant the DIMENSIONS were read, never the status read: a
            # caller reading this is asking how old the lot size is, which is
            # the only question the field can answer. The row is not rewritten
            # on this branch for the same reason — a fresh status is not
            # evidence that the dimensions were re-read.
            refreshed_at=cached.refreshed_at,
        )
    instrument = await _fetch_instrument(ticker, now)
    await _store_instrument(instrument)
    return instrument


async def get_candles(
    figi: str, interval: CandleInterval, since: datetime, until: datetime
) -> list[Candle]:
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect(method="get_candles")
    try:
        response = await conn.services.market_data.get_candles(
            instrument_id=figi,
            from_=since,
            to=until,
            interval=interval,
        )
    except AioRequestError as exc:
        _translate(exc, conn.config.tinvest_token, method="get_candles", not_found=None)
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
    _note_success("get_candles")
    return candles


def _quote_time(raw: Any) -> datetime | None:
    """The quote's own timestamp, read directly.

    `LastPrice` carries `figi`, `price`, `time`, `instrument_uid` and
    `last_price_type` — measured against the live account, and there is no
    `timestamp` to fall back to. Probing for one could only ever turn a rename
    of `time` into every quote being rejected as stale, which reads like a
    broker data problem and would stop any LOCAL stop-loss firing. An
    `AttributeError` names the field and the line instead (#33).
    """
    moment = raw.time
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
    conn = await _connect(method="get_last_price")
    try:
        response = await conn.services.market_data.get_last_prices(instrument_id=[figi])
    except AioRequestError as exc:
        _translate(
            exc, conn.config.tinvest_token, method="get_last_price", not_found=None
        )
    prices = list(response.last_prices)
    if not prices:
        # No quote at all is neither of the two documented shapes: nothing
        # arrived to reject, and the broker was plainly reachable. Left as the
        # pre-existing BrokerUnavailable so the caller retries — `reconcile`
        # falls back to the entry price on exactly this type — rather than
        # changing another module's behaviour from inside this one.
        _raise_unavailable(
            "get_last_price", "broker returned no quote for this instrument"
        )
    quote = prices[0]
    price = _decimal_quote(quote.price)
    _reject_quote(figi, price, _quote_time(quote), conn.config)
    # A rejected quote never reaches here, so it cannot become the baseline
    # that makes the next implausible value look reasonable.
    _last_accepted[figi] = price
    _note_success("get_last_price")
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
    conn = await _connect(method="get_portfolio")
    cfg = conn.config
    try:
        response = await conn.services.operations.get_portfolio(
            account_id=cfg.tinvest_account_id
        )
    except AioRequestError as exc:
        _translate(exc, cfg.tinvest_token, method="get_portfolio", not_found=None)
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
    _note_success("get_portfolio")
    return PortfolioState(cash=cash, positions=tuple(holdings))


def _trade_date(day: object) -> date | None:
    """The Moscow calendar date of `TradingDay.date`, or None when undateable.

    Field 1 is populated and correct on a closed day, measured against the live
    account (§2.1) — the 1970 sentinel lives in fields 3 and 4 only, so the date
    must not be derived from `start_time`, nor from the entry's position in the
    list, which would turn a short response into a silently mis-dated calendar
    (#51). Never `.date()` on the UTC value: that is wrong by one day whenever
    the broker expresses midnight Moscow rather than midnight UTC.
    """
    raw = getattr(day, "date", None)
    if isinstance(raw, datetime):
        moment = _aware(raw)
        if moment is None or moment < _EPOCH_GUARD:
            return None
        return clock.moscow_date(moment)
    if isinstance(raw, date):
        return raw if raw >= _EPOCH_GUARD.date() else None
    return None


def _session_from_day(day: object) -> SessionInfo | None:
    """One calendar day. A day that is not a session is marked, never invented.

    `None` when the day carries no usable date: it cannot be keyed, recorded or
    deduped, and the two alternatives — a date fabricated from list position, or
    a `None` admitted back into `SessionInfo` — are each the defect #51 was.
    """
    trade_date = _trade_date(day)
    if trade_date is None:
        return None
    start = _aware(getattr(day, "start_time", None))
    end = _aware(getattr(day, "end_time", None))
    trading = bool(getattr(day, "is_trading_day", False))
    if start is None or end is None or start < _EPOCH_GUARD or end < _EPOCH_GUARD:
        # 1970-01-01 in both is what a closed day carries; believing it would
        # be another way to believe the market is open.
        trading = False
    if not trading:
        return SessionInfo(
            trade_date=trade_date, start=None, end=None, is_trading_day=False
        )
    return SessionInfo(trade_date=trade_date, start=start, end=end, is_trading_day=True)


async def get_trading_schedule(days: int) -> list[SessionInfo]:
    if days > _MAX_SCHEDULE_DAYS:
        raise ValueError(
            f"days must not exceed {_MAX_SCHEDULE_DAYS}; the broker rejects a "
            "longer horizon with INVALID_ARGUMENT / 30002"
        )
    # Anchored to the start of the current UTC day, because the broker measures
    # the horizon from there and not from the instant of the call (#39).
    start = (
        clock.now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    until = start + timedelta(days=days)
    conn = await _connect(method="get_trading_schedule")
    try:
        response = await conn.services.instruments.trading_schedules(
            exchange=_EXCHANGE, from_=start, to=until
        )
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_trading_schedule",
            not_found=None,
        )
    sessions: list[SessionInfo] = []
    for exchange in response.exchanges:
        if str(exchange.exchange).upper() != _EXCHANGE:
            continue
        for day in exchange.days:
            session = _session_from_day(day)
            if session is None:
                # No honest fallback exists, so the day is left out rather than
                # given a date from its list position. Omission shortens the
                # window, which `market.session.covers` reports; a mis-dated day
                # is something nothing would report (#51).
                _log.warning(
                    "trading schedule day has no usable date; omitting it",
                    extra={"method": "get_trading_schedule"},
                )
                continue
            sessions.append(session)
    # The broker returns one contiguous entry per day, oldest first (§2.1). Stated
    # here so `market.session` may read `fetched[0]` as the window's first day
    # without depending on a response shape nothing pins.
    sessions.sort(key=lambda session: session.trade_date)
    _note_success("get_trading_schedule")
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
    trade_ids: tuple[str, ...] = (),
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
        trade_ids=trade_ids,
    )


async def post_market_order(key: str, figi: str, side: Side, lots: int) -> OrderRecord:
    conn = await _connect(method="post_market_order")
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
        _translate(
            exc, conn.config.tinvest_token, method="post_market_order", not_found=None
        )
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
    _note_success("post_market_order")
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
    conn = await _connect(method="post_stop_loss")
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
        _translate(
            exc, conn.config.tinvest_token, method="post_stop_loss", not_found=None
        )
    stop_id = getattr(response, "stop_order_id", None) or None
    if not stop_id:
        raise StopOrderRejected("rejected")
    _note_success("post_stop_loss")
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
    conn = await _connect(method="cancel_stop_order")
    try:
        await conn.services.stop_orders.cancel_stop_order(
            account_id=conn.config.tinvest_account_id, stop_order_id=stop_order_id
        )
    except AioRequestError as exc:
        # The executor calls this while racing the exchange, so an
        # already-cancelled or already-executed stop order is not an error.
        if exc.code is StatusCode.NOT_FOUND:
            _note_success("cancel_stop_order")
            return
        _translate(
            exc, conn.config.tinvest_token, method="cancel_stop_order", not_found=None
        )
    _note_success("cancel_stop_order")


async def cancel_order(key: str) -> None:
    """Cancel a live ordinary order by our own idempotency key.

    Idempotent in the same sense as `cancel_stop_order`: the caller is racing
    the exchange by definition, so an order already filled, already cancelled
    or unknown here is not an error. The authoritative answer comes from the
    `get_order_state` that follows, never from this call's outcome (#10).
    """
    conn = await _connect(method="cancel_order")
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
            _note_success("cancel_order")
            return
        _translate(
            exc, conn.config.tinvest_token, method="cancel_order", not_found=None
        )
    _note_success("cancel_order")


def _stop_status(raw: object) -> StopOrderStatus:
    name = getattr(raw, "name", str(raw))
    if "EXECUTED" in name:
        return StopOrderStatus.EXECUTED
    if "CANCELED" in name or "CANCELLED" in name or "EXPIRED" in name:
        return StopOrderStatus.CANCELLED
    return StopOrderStatus.ACTIVE


async def list_stop_orders() -> list[StopOrderRecord]:
    conn = await _connect(method="list_stop_orders")
    try:
        response = await conn.services.stop_orders.get_stop_orders(
            account_id=conn.config.tinvest_account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
        )
    except AioRequestError as exc:
        _translate(
            exc, conn.config.tinvest_token, method="list_stop_orders", not_found=None
        )
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
    _note_success("list_stop_orders")
    return records


def _omit_stop(stop_id: str, cause: str) -> None:
    """Name a stop left out of `get_executed_stop_fills`, and why (v1.98, #259).

    A plain log line rather than a §7.1 event: it has no catalogue row, and an
    unknown `event` makes `scripts/deploy/export_health.py` exit non-zero.
    """
    _log.warning("executed stop %s omitted: %s", stop_id, cause)


async def get_executed_stop_fills(
    since: datetime, until: datetime
) -> dict[str, OrderRecord]:
    """The broker's own record of every stop placed in the window that has fired.

    Keyed by `stop_order_id`. Two SDK calls composed here because this is the
    only module permitted to talk to the broker: executed stops, then each
    one's `exchange_order_id` resolved to the actual fill. A stop whose
    exchange order does not resolve is omitted, and logged — the caller retries
    rather than booking a price nobody reported (rule 33).

    `since` and `until` bound when each stop was **placed**, not when it fired:
    the broker filters `from`/`to` on creation (§2.1, v1.98, #259). A caller
    after a stop's fill passes a `since` at or before that stop was posted.
    """
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect(method="get_executed_stop_fills")
    try:
        response = await conn.services.stop_orders.get_stop_orders(
            account_id=conn.config.tinvest_account_id,
            status=StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED,
            from_=since,
            to=until,
        )
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_executed_stop_fills",
            not_found=None,
        )

    fills: dict[str, OrderRecord] = {}
    for raw in response.stop_orders:
        stop_id = getattr(raw, "stop_order_id", "") or ""
        exchange_id = getattr(raw, "exchange_order_id", "") or ""
        if not stop_id:
            continue
        if not exchange_id:
            _omit_stop(stop_id, "no exchange_order_id")
            continue
        try:
            state = await conn.services.orders.get_order_state(
                account_id=conn.config.tinvest_account_id,
                order_id=exchange_id,
                order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
            )
        except AioRequestError as exc:
            # Not yet settled, or not resolvable. Omit it: the position stays
            # open and the caller asks again next cycle. Said out loud, because
            # a silent omission is what left the 09-16 stop undiagnosable (#259).
            code = getattr(exc.code, "name", str(exc.code))
            detail = _redact(exc.details or "", conn.config.tinvest_token)
            _omit_stop(
                stop_id,
                f"exchange order {exchange_id} did not resolve ({code}: {detail})",
            )
            continue
        filled = state.lots_executed or None
        if not filled:
            _omit_stop(stop_id, f"exchange order {exchange_id} has no executed lots")
            continue
        fills[stop_id] = _order_record(
            key=exchange_id,
            figi=state.figi,
            side=Side.SELL,
            lots=int(state.lots_requested),
            status=_order_status(state.execution_report_status),
            filled_lots=int(filled),
            # Per share. `executed_order_price` on an OrderState is the order's
            # rouble total (§2.1, v1.98, #258).
            filled_price=_decimal_money(state.average_position_price),
            commission=_executed_commission(
                getattr(state, "executed_commission", None)
            ),
            trade_ids=_trade_ids(getattr(state, "stages", None)),
            broker_reason=None,
            created_at=getattr(state, "order_date", None) or clock.now(),
            settled_at=getattr(state, "order_date", None) or clock.now(),
        )
    _note_success("get_executed_stop_fills")
    return fills


async def get_max_lots(figi: str) -> int:
    conn = await _connect(method="get_max_lots")
    request = GetMaxLotsRequest(
        account_id=conn.config.tinvest_account_id, instrument_id=figi
    )
    try:
        response = await conn.services.orders.get_max_lots(request)
    except AioRequestError as exc:
        _translate(
            exc, conn.config.tinvest_token, method="get_max_lots", not_found=None
        )
    # The non-margin limit. buy_margin_limits is never read.
    _note_success("get_max_lots")
    return int(response.buy_limits.buy_max_market_lots)


async def get_operations(since: datetime, until: datetime) -> list[OperationRecord]:
    _reject_naive(since)
    _reject_naive(until)
    conn = await _connect(method="get_operations")
    try:
        response = await conn.services.operations.get_operations(
            account_id=conn.config.tinvest_account_id, from_=since, to=until
        )
    except AioRequestError as exc:
        _translate(
            exc, conn.config.tinvest_token, method="get_operations", not_found=None
        )
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
                trade_ids=_trade_ids(getattr(raw, "trades", None)),
            )
        )
    _note_success("get_operations")
    return records


async def get_order_state(key: str) -> OrderRecord:
    conn = await _connect(method="get_order_state")
    try:
        response = await conn.services.orders.get_order_state(
            account_id=conn.config.tinvest_account_id,
            # By the client key alone: after a crash the exchange identifier is
            # precisely what was lost.
            order_id=key,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
        )
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_order_state",
            not_found=OrderNotFound,
        )
    _note_success("get_order_state")
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
    conn = await _connect(method="get_order_state_by_broker_id")
    try:
        response = await conn.services.orders.get_order_state(
            account_id=conn.config.tinvest_account_id,
            order_id=broker_order_id,
            order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
        )
    except AioRequestError as exc:
        _translate(
            exc,
            conn.config.tinvest_token,
            method="get_order_state_by_broker_id",
            not_found=OrderNotFound,
        )
    # Keyed by what it was asked about: this module does not know the local
    # row's key and must not invent one.
    _note_success("get_order_state_by_broker_id")
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
        # Per share. On an OrderState `executed_order_price` is the order's
        # rouble total; only PostOrderResponse carries it per share (§2.1, #258).
        filled_price=(
            _decimal_money(response.average_position_price) if filled else None
        ),
        commission=(
            _executed_commission(getattr(response, "executed_commission", None))
            if filled
            else None
        ),
        trade_ids=_trade_ids(getattr(response, "stages", None)),
        broker_reason=None,
        created_at=created,
        settled_at=created if status is OrderStatus.FILLED else None,
    )
