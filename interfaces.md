# Interfaces

This file records the public surface of every completed module. It is appended
to after each module passes its tests. **Read it before implementing any
module** — call only what is recorded here, and never reimplement something
already listed.

Entries appear in build order, matching `dependency-order.md`, so the file reads
top-to-bottom as the dependency chain.

Record for each public function: exact name, parameters with types, return type
including `| None`, whether it is async, one line on what it does, exceptions
that are part of the contract, and any critical constraint (for example "sole
owner of position mutation", "never raises").

## `zarabot.models`

Domain types. Validation only; no I/O. Construction raises `ValueError` on a
naive datetime, a negative lot count, a negative price, or a non-positive lot
size; `TypeError` when a monetary field is given a `float`. Every dataclass is
frozen.

**Enumerations** (string values equal the member names; store as TEXT):
- `Side` — `BUY`, `SELL`
- `OrderStatus` — `SUBMITTING`, `SUBMITTED`, `FILLED`, `REJECTED`, `CANCELLED`, `UNKNOWN`
- `ExitTrigger` — `STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`, `EXTERNAL`
- `RejectionReason` — `HALTED`, `SESSION_CLOSED`, `INSTRUMENT_NOT_TRADING`, `DUPLICATE_TICKER`, `MAX_POSITIONS`, `COOLDOWN_ACTIVE`, `INSUFFICIENT_CASH`, `ZERO_LOTS`, `POSITION_CAP`, `BROKER_LOT_LIMIT`
- `HaltReason` — `DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`
- `StopOrderStatus` — `PLACING`, `ACTIVE`, `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`
- `StopProtection` — `EXCHANGE`, `LOCAL`

**`Candle(timestamp: datetime, open: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: int)`**
OHLCV bar. `timestamp` timezone-aware.

**`Instrument(figi: str, ticker: str, lot: int, min_price_increment: Decimal, currency: str, trading_status: str, refreshed_at: datetime)`**
Exchange instrument metadata. `lot` is units per lot and must be positive.

**`Signal(ticker: str, strategy: str, side: Side, generated_at: datetime, reference_price: Decimal)`**
Entry signal produced by a strategy.

**`Position(id: int, ticker: str, figi: str, strategy: str, lots: int, lot_size: int, entry_price: Decimal, entry_at: datetime, stop_price: Decimal, target_price: Decimal, status: str, adopted: bool, open_order_key: str, close_order_key: str | None, exit_trigger: ExitTrigger | None, exit_price: Decimal | None, exit_at: datetime | None, realised_pnl: Decimal | None, stop_protection: StopProtection, stop_order_key: str | None)`**
Open or closed holding. `lots` must be positive. `stop_protection=EXCHANGE` requires `stop_order_key`; `LOCAL` forbids one. Raises `ValueError` on a pairing violation.

**`OrderRecord(key: str, ticker: str, figi: str, side: Side, intent: str, lots: int, status: OrderStatus, filled_lots: int | None, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None, created_at: datetime, settled_at: datetime | None)`**
Client-keyed order. `intent` is `ENTRY` or `EXIT`.

**`StopOrderRecord(key: str, stop_order_id: str | None, position_id: int, ticker: str, lots: int, stop_price: Decimal, status: StopOrderStatus, created_at: datetime, settled_at: datetime | None)`**
Standing stop-loss tracked locally.

**`OperationRecord(id: str, figi: str, ticker: str, occurred_at: datetime, commission: Decimal, payment: Decimal, price: Decimal | None, quantity: int | None)`**
Broker operation including actual commission. `payment` may be negative (a debit).

**`PortfolioState(cash: Decimal, positions: tuple[Position, ...])`**
Broker-authoritative cash and holdings.

**`SessionInfo(start: datetime | None, end: datetime | None, is_trading_day: bool)`**
One calendar day's session. `in_closing_window(now: datetime, minutes: int = 15) → bool` is true during the final `minutes` of the session (`now` inclusive of the window start, exclusive of `end`). Raises `ValueError` on naive `now`. Not I/O.

**`RiskDecision(approved: bool, lots: int | None, reason: RejectionReason | None)`**
Exactly one of: approved with `lots > 0` and `reason is None`, or rejected with `reason` set and `lots is None`. Raises `ValueError` if both, neither, or approved with zero lots.

**`HaltState(halted: bool, reason: HaltReason | None, detail: str | None, halted_at: datetime | None, resumed_at: datetime | None, resumed_by: str | None)`**
Persisted halt flag.

**`ReconciliationReport(ran_at: datetime, adjustments: tuple[dict[str, Any], ...])`**
Observational broker-vs-local comparison. Empty `adjustments` means agreement.

**`TradingCalendar(sessions: tuple[SessionInfo, ...])`**
Queried exchange schedule consumed by `clock.trading_days_between` and `market.session`.

**`BacktestResult(trades: tuple[Position, ...], pnl: Decimal, win_rate: Decimal, max_drawdown: Decimal, exit_trigger_distribution: tuple[tuple[ExitTrigger, int], ...], benchmark_return: Decimal | None)`**
Sandbox backtest summary. `benchmark_return` is `None` when unavailable, never zero-filled.

