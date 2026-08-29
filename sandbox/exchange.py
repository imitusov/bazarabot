"""A simulated broker backed by historical bars.

This is what makes a backtest mean something: with it the simulation does not
*resemble* the live path, it **is** the live path with the broker and the clock
replaced. Every function here matches its `broker.client` counterpart's
signature and raises the same exception for the same condition, because a
double that cannot fail the way the broker fails cannot exercise the code that
handles failure.

Never imported by `zarabot/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from zarabot.broker.client import (
    InstrumentNotFound,
    OrderNotFound,
    OrderRejected,
    StopOrderRejected,
)
from zarabot.models import (
    Candle,
    ExitTrigger,
    Instrument,
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


class Phase(StrEnum):
    """Which price of a bar the exchange reports as the last price.

    A phase rather than a price, because the marks are per instrument and a
    backtest runs the whole watchlist: a scalar passed by the caller would
    report one ticker's low as every ticker's. The exchange holds the bars, so
    it is the only thing that can resolve this per instrument (v1.50).
    """

    OPEN = "open"
    LOW = "low"
    HIGH = "high"
    CLOSE = "close"


_ONE = Decimal("1")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class Commission:
    """The broker's tariff: a percentage of turnover with a minimum.

    A flat per-trade figure was both the wrong shape and, in the old
    backtester, applied twice to one round trip (#12).
    """

    pct: Decimal
    minimum: Decimal

    def on(self, turnover: Decimal) -> Decimal:
        return max(turnover * self.pct / _HUNDRED, self.minimum)


@dataclass
class _Holding:
    lots: int
    average_price: Decimal


@dataclass
class SimulatedExchange:
    """Historical bars presented through `broker.client`'s surface."""

    bars: dict[str, list[Candle]]
    instruments: dict[str, Instrument]
    cash: Decimal
    slippage: Decimal
    commission: Commission

    reject_stops: bool = False

    _now: datetime | None = None
    # Which price of the current bar `get_last_price` reports. Walking a bar's
    # open, low and high is what makes the daily loss limit and a LOCAL stop
    # reachable at all (#53).
    _phase: Phase = Phase.CLOSE
    # Bars whose standing stops have already been checked. Four cycles across a
    # bar must not be four chances to fire.
    _stops_checked: set[datetime] = field(default_factory=set)
    _orders: dict[str, OrderRecord] = field(default_factory=dict)
    _stops: dict[str, StopOrderRecord] = field(default_factory=dict)
    _executed_stops: dict[str, OrderRecord] = field(default_factory=dict)
    _holdings: dict[str, _Holding] = field(default_factory=dict)

    # ---------------------------------------------------------------- helpers

    def _ticker_for(self, figi: str) -> str:
        for ticker, instrument in self.instruments.items():
            if instrument.figi == figi:
                return ticker
        raise InstrumentNotFound(figi)

    def _visible(self, ticker: str) -> list[Candle]:
        """Bars up to and including the cursor. Never the future."""
        if self._now is None:
            return []
        return [bar for bar in self.bars.get(ticker, []) if bar.timestamp <= self._now]

    def _bar_for(self, ticker: str) -> Candle | None:
        """The bar the cursor is inside — the latest one at or before `now`.

        `now` may be a sub-bar instant, so this is a search rather than an
        exact match on the timestamp.
        """
        seen = self._visible(ticker)
        return seen[-1] if seen else None

    def _next_bar(self, ticker: str) -> Candle | None:
        """The bar after the cursor — where a market order gets its price.

        The exchange may see it; the strategy may not. That asymmetry is what
        makes the fill free of look-ahead while keeping it synchronous.
        """
        if self._now is None:
            return None
        for bar in self.bars.get(ticker, []):
            if bar.timestamp > self._now:
                return bar
        return None

    def hold(self, figi: str, lots: int, average_price: Decimal) -> None:
        """Seed a holding, for tests and for a backtest resumed mid-history."""
        self._holdings[figi] = _Holding(lots=lots, average_price=average_price)

    def touched(self, figi: str, level: Decimal, trigger: ExitTrigger) -> bool:
        """Whether the current bar reached `level` in the trigger's direction.

        Stops read the **low** and take-profits the **high**. Reading the close,
        as the old backtester did, means a day that traded 8% down intraday and
        closed at -1% never triggers a 5% stop — while the exchange stop fires
        on the intraday print (#12).
        """
        bar = self._bar_for(self._ticker_for(figi))
        if bar is None:
            return False
        if trigger is ExitTrigger.TAKE_PROFIT:
            return bar.high >= level
        return bar.low <= level

    # ------------------------------------------------------------------ clock

    async def advance(self, moment: datetime, phase: Phase = Phase.CLOSE) -> None:
        """Move the cursor, set the reported phase, fire any triggered stop.

        Stops are checked once per bar, on first entry to it, so walking four
        phases does not give four chances to fire.
        """
        self._now = moment
        self._phase = phase
        self._fire_stops(moment)

    def _apply_fill(self, order: OrderRecord, price: Decimal) -> OrderRecord:
        ticker = self._ticker_for(order.figi)
        units = Decimal(order.lots * self.instruments[ticker].lot)
        turnover = price * units
        fee = self.commission.on(turnover)
        if order.side is Side.BUY:
            self.cash -= turnover + fee
            held = self._holdings.get(order.figi)
            self._holdings[order.figi] = _Holding(
                lots=(held.lots if held else 0) + order.lots,
                average_price=price,
            )
        else:
            self.cash += turnover - fee
            held = self._holdings.get(order.figi)
            if held is not None:
                held.lots -= order.lots
                if held.lots <= 0:
                    del self._holdings[order.figi]
        return _settled(order, price, fee, self._moment())

    def _fire_stops(self, moment: datetime) -> None:
        """A standing stop fires on the bar's low, filling no better than its open.

        Checked once per bar, on first entry to it: four cycles across a bar are
        four marks, not four chances to fire.
        """
        entered = {
            bar.timestamp
            for bar in (self._bar_for(ticker) for ticker in self.bars)
            if bar is not None and bar.timestamp not in self._stops_checked
        }
        for key, stop in list(self._stops.items()):
            if stop.status is not StopOrderStatus.ACTIVE:
                continue
            ticker = stop.ticker
            bar = self._bar_for(ticker)
            if bar is None or bar.timestamp in self._stops_checked:
                continue
            if bar.low > stop.stop_price:
                continue
            # The exchange cannot fill where the market never traded: a bar
            # that gaps through the stop fills at its open.
            price = min(stop.stop_price, bar.open)
            figi = self.instruments[ticker].figi
            units = Decimal(stop.lots * self.instruments[ticker].lot)
            turnover = price * units
            fee = self.commission.on(turnover)
            self.cash += turnover - fee
            held = self._holdings.get(figi)
            if held is not None:
                held.lots -= stop.lots
                if held.lots <= 0:
                    del self._holdings[figi]
            exchange_id = f"EX-{stop.stop_order_id}"
            self._executed_stops[str(stop.stop_order_id)] = OrderRecord(
                key=exchange_id,
                ticker=ticker,
                figi=figi,
                side=Side.SELL,
                intent="EXIT",
                lots=stop.lots,
                status=OrderStatus.FILLED,
                filled_lots=stop.lots,
                filled_price=price,
                commission=fee,
                broker_reason=None,
                created_at=moment,
                settled_at=moment,
            )
            self._stops[key] = _stop_settled(stop, moment)
        self._stops_checked |= entered

    def _slipped(self, price: Decimal, side: Side) -> Decimal:
        if side is Side.BUY:
            return price * (_ONE + self.slippage)
        return price * (_ONE - self.slippage)

    # --------------------------------------------------- broker.client surface

    async def get_instrument(self, ticker: str) -> Instrument:
        found = self.instruments.get(ticker)
        if found is None:
            raise InstrumentNotFound(ticker)
        return found

    async def get_candles(
        self, figi: str, interval: object, since: datetime, until: datetime
    ) -> list[Candle]:
        ticker = self._ticker_for(figi)
        return [bar for bar in self._visible(ticker) if since <= bar.timestamp <= until]

    async def get_last_price(self, figi: str) -> Decimal:
        bar = self._bar_for(self._ticker_for(figi))
        if bar is None:
            raise InstrumentNotFound(figi)
        price: Decimal = getattr(bar, self._phase.value)
        return price

    async def get_portfolio(self) -> PortfolioState:
        positions: list[Position] = []
        for figi, held in self._holdings.items():
            ticker = self._ticker_for(figi)
            positions.append(_holding_position(ticker, figi, held, self._moment()))
        return PortfolioState(cash=self.cash, positions=tuple(positions))

    async def get_max_lots(self, figi: str) -> int:
        bar = self._bar_for(self._ticker_for(figi))
        if bar is None or bar.close <= 0:
            return 0
        lot = self.instruments[self._ticker_for(figi)].lot
        return int(self.cash / (bar.close * Decimal(lot)))

    async def post_market_order(
        self, key: str, figi: str, side: Side, lots: int
    ) -> OrderRecord:
        if lots <= 0:
            raise OrderRejected("lots must be positive")
        ticker = self._ticker_for(figi)
        order = OrderRecord(
            key=key,
            ticker=ticker,
            figi=figi,
            side=side,
            intent="ENTRY" if side is Side.BUY else "EXIT",
            lots=lots,
            # Live at the broker until the next bar opens.
            status=OrderStatus.SUBMITTED,
            filled_lots=0,
            filled_price=None,
            commission=None,
            broker_reason=None,
            created_at=self._moment(),
            settled_at=None,
        )
        # Priced at the NEXT bar's open — that price was not knowable when the
        # decision was made — but returned now. Deferring the fill would send
        # every entry through open_position's crash-recovery path and trip the
        # outage counter, a live/backtest divergence on the ordinary path
        # (v1.47).
        following = self._next_bar(ticker)
        if following is not None and side is Side.BUY:
            # The broker refuses what the account cannot fund, and get_max_lots
            # exists to make that avoidable. Debiting unconditionally drove cash
            # negative and the next get_portfolio raised "cash must not be
            # negative" — and a simulator that funds any order cannot show that
            # the gate and sizing keep the bot solvent (#47).
            price = self._slipped(following.open, side)
            turnover = price * Decimal(lots * self.instruments[ticker].lot)
            if turnover + self.commission.on(turnover) > self.cash:
                raise OrderRejected("insufficient funds")
        if following is None:
            # History ran out. Inventing a price here is the look-ahead the
            # next-open rule exists to prevent.
            self._orders[key] = order
            return order
        settled = self._apply_fill(order, self._slipped(following.open, side))
        self._orders[key] = settled
        return settled

    async def get_order_state(self, key: str) -> OrderRecord:
        found = self._orders.get(key)
        if found is None:
            raise OrderNotFound(key)
        return found

    async def cancel_order(self, key: str) -> None:
        order = self._orders.get(key)
        if order is not None and order.status is OrderStatus.SUBMITTED:
            self._orders[key] = _cancelled(order, self._moment())

    async def post_stop_loss(
        self, key: str, figi: str, lots: int, stop_price: Decimal
    ) -> StopOrderRecord:
        if lots <= 0:
            raise StopOrderRejected("lots must be positive")
        if self.reject_stops:
            # A failure the real broker can produce, and the only way a
            # backtest can reach rule 23's degrade path, where a position
            # stays LOCAL and the bot owns its own stop.
            raise StopOrderRejected("stop orders refused")
        ticker = self._ticker_for(figi)
        record = StopOrderRecord(
            key=key,
            stop_order_id=f"SIM-{key}",
            position_id=0,
            ticker=ticker,
            lots=lots,
            stop_price=stop_price,
            status=StopOrderStatus.ACTIVE,
            created_at=self._moment(),
            settled_at=None,
        )
        self._stops[key] = record
        return record

    async def cancel_stop_order(self, stop_order_id: str) -> None:
        for key, stop in list(self._stops.items()):
            if stop.stop_order_id == stop_order_id:
                self._stops[key] = _stop_cancelled(stop, self._moment())

    async def list_stop_orders(self) -> list[StopOrderRecord]:
        return [
            stop
            for stop in self._stops.values()
            if stop.status is StopOrderStatus.ACTIVE
        ]

    async def get_executed_stop_fills(
        self, since: datetime, until: datetime
    ) -> dict[str, OrderRecord]:
        return {
            stop_id: fill
            for stop_id, fill in self._executed_stops.items()
            if fill.settled_at is not None and since <= fill.settled_at <= until
        }

    async def get_trading_schedule(self, days: int) -> list[SessionInfo]:
        """One session per bar, so `market.session` runs on top rather than
        being stubbed out. Stubbing it would skip the code that decides whether
        the market is open, which is where #39 and #43 lived."""
        sessions: list[SessionInfo] = []
        for bar in next(iter(self.bars.values()), []):
            sessions.append(
                SessionInfo(
                    start=bar.timestamp,
                    end=bar.timestamp.replace(hour=15, minute=45),
                    is_trading_day=True,
                )
            )
        return sessions

    def _moment(self) -> datetime:
        if self._now is None:
            raise RuntimeError("advance() before using the exchange")
        return self._now


def _settled(
    order: OrderRecord, price: Decimal, fee: Decimal, moment: datetime
) -> OrderRecord:
    return OrderRecord(
        key=order.key,
        ticker=order.ticker,
        figi=order.figi,
        side=order.side,
        intent=order.intent,
        lots=order.lots,
        status=OrderStatus.FILLED,
        filled_lots=order.lots,
        filled_price=price,
        commission=fee,
        broker_reason=None,
        created_at=order.created_at,
        settled_at=moment,
        exit_trigger=order.exit_trigger,
    )


def _cancelled(order: OrderRecord, moment: datetime) -> OrderRecord:
    return OrderRecord(
        key=order.key,
        ticker=order.ticker,
        figi=order.figi,
        side=order.side,
        intent=order.intent,
        lots=order.lots,
        status=OrderStatus.CANCELLED,
        filled_lots=0,
        filled_price=None,
        commission=None,
        broker_reason="cancelled",
        created_at=order.created_at,
        settled_at=moment,
        exit_trigger=order.exit_trigger,
    )


def _stop_settled(stop: StopOrderRecord, moment: datetime) -> StopOrderRecord:
    return StopOrderRecord(
        key=stop.key,
        stop_order_id=stop.stop_order_id,
        position_id=stop.position_id,
        ticker=stop.ticker,
        lots=stop.lots,
        stop_price=stop.stop_price,
        status=StopOrderStatus.EXECUTED,
        created_at=stop.created_at,
        settled_at=moment,
    )


def _stop_cancelled(stop: StopOrderRecord, moment: datetime) -> StopOrderRecord:
    return StopOrderRecord(
        key=stop.key,
        stop_order_id=stop.stop_order_id,
        position_id=stop.position_id,
        ticker=stop.ticker,
        lots=stop.lots,
        stop_price=stop.stop_price,
        status=StopOrderStatus.CANCELLED,
        created_at=stop.created_at,
        settled_at=moment,
    )


def _holding_position(
    ticker: str, figi: str, held: _Holding, moment: datetime
) -> Position:
    return Position(
        id=0,
        ticker=ticker,
        figi=figi,
        strategy="ADOPTED",
        lots=held.lots,
        lot_size=1,
        entry_price=held.average_price,
        entry_at=moment,
        stop_price=held.average_price,
        target_price=held.average_price,
        status="OPEN",
        adopted=True,
        open_order_key=f"SIM-{figi}",
        close_order_key=None,
        exit_trigger=None,
        exit_price=None,
        exit_at=None,
        realised_pnl=None,
        stop_protection=StopProtection.LOCAL,
        stop_order_key=None,
    )
