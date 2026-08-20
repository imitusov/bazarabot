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

**`OrderRecord(key: str, ticker: str, figi: str, side: Side, intent: str, lots: int, status: OrderStatus, filled_lots: int | None, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None, created_at: datetime, settled_at: datetime | None, exit_trigger: ExitTrigger | None = None)`**
Client-keyed order. `intent` is `ENTRY` or `EXIT`. `exit_trigger` is non-null
exactly when `intent` is `EXIT` (`STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`).

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

## `zarabot.clock`

Sole owner of "now" and of trading-day arithmetic. No other module may call
`datetime.now()`.

**`now() → datetime`**
Current instant, timezone-aware, UTC. The only permitted reader of the system clock.

**`to_moscow(moment: datetime) → datetime`**
Converts a timezone-aware instant to `Europe/Moscow` via the IANA zone, never a
fixed offset. Raises `ValueError` on a naive input.

**`moscow_date(moment: datetime) → date`**
Moscow calendar date of an instant. Raises `ValueError` on a naive input.

**`trading_days_between(start: datetime, end: datetime, calendar: TradingCalendar) → int`**
Count of exchange trading days elapsed after `start`'s Moscow date through
`end`'s Moscow date, using `calendar` sessions with `is_trading_day=True`.
Returns 0 when both instants fall on the same trading day. Raises `ValueError`
on a naive input or when `end` precedes `start`.

## `zarabot.config`

Loads and validates every setting once at startup. Tokens never appear in
`ConfigError` messages or in `Config`'s `__repr__` / `__str__`.

**`ConfigError`**
Raised when a required variable is missing or empty, a numeric value is out of
range, or a cross-field rule fails. The message names the offending variable.

**`Config`** (frozen)
`tinvest_token: str`, `tinvest_account_id: str`, `trading_mode: str`,
`telegram_bot_token: str`, `telegram_chat_id: int`, `allocated_capital: Decimal`,
`position_size_pct: Decimal`, `max_position_pct: Decimal`, `stop_loss_pct: Decimal`,
`take_profit_pct: Decimal`, `max_holding_days: int`, `max_open_positions: int`,
`reentry_cooldown_minutes: int`, `daily_loss_limit_pct: Decimal`,
`watchlist: tuple[str, ...]`, `enabled_strategies: tuple[str, ...]`,
`ml_model_path: Path | None`, `poll_interval_seconds: int`, `db_path: Path`,
`backup_dir: Path`, `log_level: str`, `tz: str`.

**`load() → Config`**
Reads the brief's environment-variable table. Applies documented defaults to
non-risk optional variables. Never substitutes a default for missing
`ALLOCATED_CAPITAL`. Raises `ConfigError` naming the variable when: a required
variable is missing or empty; a percentage is `<= 0` or `> 100`;
`POSITION_SIZE_PCT` exceeds `MAX_POSITION_PCT`;
`MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeds 100; `TAKE_PROFIT_PCT` is not
greater than `STOP_LOSS_PCT`; `WATCHLIST` is empty; `TRADING_MODE` is not
`live` or `sandbox`; or `ML_MODEL_PATH` is set but unreadable.

## `zarabot.logging_setup`

JSON logs to stdout only. Never a file, never stderr. Tokens are replaced by
`MASK` (`"***"`).

**`MASK: str`**
Fixed redaction mask.

**`configure(level: str, secrets: list[str]) → None`**
Installs a JSON formatter on stdout and a filter that replaces every occurrence
of every value in `secrets` in the message, structured fields, and formatted
exception text. Recurses into nested dicts and sequences to depth 10; deeper
structures are replaced wholesale. A record that contains no secret is left
unmodified.

## `zarabot.db.migrations`

Owns schema creation and `schema_version`. Forward-only.

**`MigrationError`**
Raised when the recorded schema version exceeds the highest known migration.
No modification is made in that case.

**`MIGRATIONS_DIR: Path`**
Directory of `NNN_description.sql` files. Defaults to repo `migrations/`.

**`async apply(conn: aiosqlite.Connection) → int`**
Applies every migration whose version exceeds the recorded version, in
ascending order, each in its own transaction. Returns the resulting schema
version. Idempotent. `applied_at` is written via `clock.now()`.

## `zarabot.db.positions`

Sole owner of `positions` rows. Never deletes. Reads `DB_PATH` via `config.load()`.

**`PositionStateError`**
Illegal transition, pairing violation, or duplicate open ticker.

**`async open(signal: Signal, order: OrderRecord, instrument: Instrument, stop: Decimal, target: Decimal, opened_at: datetime) → Position`**
Inserts an open position with `stop_protection=LOCAL` and `stop_order_key=None`.
Raises `PositionStateError` if an open row for the ticker exists. Raises
`ValueError` on a naive `opened_at`.

**`async set_stop_protection(position_id: int, protection: StopProtection, stop_order_key: str | None) → Position`**
EXCHANGE requires a key; LOCAL forbids one. Raises `PositionStateError` on a
pairing violation or missing row.

**`async close(position_id: int, trigger: ExitTrigger, exit_price: Decimal, closed_at: datetime, order: OrderRecord | None) → Position`**
Atomic OPEN→CLOSED. Concurrent callers: exactly one succeeds. Realised P&L is
`(exit-entry)×lots×lot_size` minus commission on both legs (opening order and
closing order; 0 when unset). `order` is `None` only for `EXTERNAL`; any other
pairing raises `ValueError`. Clears `stop_protection` to LOCAL and
`stop_order_key`. Never deletes. Raises `PositionStateError` if already closed
or absent. Raises `ValueError` on a naive `closed_at`.

**`async list_open() → list[Position]`**
Open positions, or `[]`. Never `None`.

**`async list_closed() → list[Position]`**
Closed positions, newest `exit_at` first, or `[]`. Never `None`.

**`async get(position_id: int) → Position | None`**
`None` when absent.

**`async adopt(instrument: Instrument, lots: int, average_price: Decimal, adopted_at: datetime) → Position`**
Open LOCAL adopted position, strategy `ADOPTED`, stop/target from average price
at configured `stop_loss_pct` / `take_profit_pct`. `open_order_key` is
`ADOPTED-{figi}`.

**`async update_lots(position_id: int, lots: int) → Position`**
Writes the broker's lot count onto an open row. Raises `PositionStateError` if
the row is absent, already closed, or `lots` is not positive.

## `zarabot.db.orders`

Sole owner of `orders` rows and status transitions. Reads `DB_PATH` via
`config.load()`. `record_submitting` must complete before any broker call with
the same key. Never resubmit; recover by querying the key.

**`DuplicateOrderError`**
Raised when the idempotency key already exists.

**`OrderStateError`**
Raised on a transition out of a terminal status (`FILLED`, `REJECTED`,
`CANCELLED`) or when settling to a non-terminal status.

**`async record_submitting(key: str, ticker: str, side: Side, lots: int, intent: str, exit_trigger: ExitTrigger | None = None) → OrderRecord`**
Inserts `SUBMITTING` with `created_at=clock.now()`. `figi` is stored as `''`
because the contract does not receive a FIGI. `exit_trigger` is required for
`EXIT` and forbidden for `ENTRY`; either violation raises `ValueError`. Raises
`DuplicateOrderError` on a repeated key.

**`async settle(key: str, status: OrderStatus, filled_lots: int, filled_price: Decimal | None, broker_reason: str | None) → OrderRecord`**
Records a terminal outcome and `settled_at`. Raises `OrderStateError` if the row
is missing, already terminal, or `status` is not terminal.

**`async list_unresolved() → list[OrderRecord]`**
`SUBMITTING` or `SUBMITTED`, oldest first. Empty list when none.

## `zarabot.db.stop_orders`

Sole owner of `stop_orders` rows. Reuses `DuplicateOrderError` and
`OrderStateError` from `zarabot.db.orders`.

**`async record_placing(key: str, position_id: int, ticker: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
Inserts `PLACING` with `created_at=clock.now()`. Raises `DuplicateOrderError`
on a repeated key.

**`async activate(key: str, stop_order_id: str) → StopOrderRecord`**
Sets status `ACTIVE` and stores the broker identifier. Raises `OrderStateError`
if already terminal.

**`async settle(key: str, status: StopOrderStatus, settled_at: datetime) → StopOrderRecord`**
Terminal statuses: `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`. Raises
`OrderStateError` on a transition out of a terminal status or a non-terminal
target. Raises `ValueError` on naive `settled_at`.

**`async active_for_position(position_id: int) → StopOrderRecord | None`**
The single `PLACING` or `ACTIVE` stop for the position, or `None`. Raises
`OrderStateError` if more than one standing stop exists.

**`async list_active() → list[StopOrderRecord]`**
Every `ACTIVE` stop, oldest first. Empty list when none.

## `zarabot.db.cooldowns`

Sole owner of `cooldowns` rows. Reads `DB_PATH` via `config.load()`.

**`async start(ticker: str, at: datetime) → None`**
Inserts `started_at`, or overwrites only when `at` is strictly newer. Raises
`ValueError` on a naive `at`. Write failures are logged at ERROR and not
propagated (rule 12).

**`async is_active(ticker: str, now: datetime, minutes: int) → bool`**
True while `now - started_at < minutes`. False when no row exists, and False
exactly at the boundary. Raises `ValueError` on a naive `now`.

**`async active_until(ticker: str, minutes: int) → datetime | None`**
`started_at + minutes`, or `None` when no cooldown is recorded.

## `zarabot.db.signals`

Sole owner of `signals` rows. Write failures are logged at ERROR and not
propagated (rule 12).

**`async record(signal: Signal, decision: RiskDecision) → None`**
Inserts the signal with `APPROVED`/`REJECTED`, lots or rejection reason, and
`order_key=None`. Reconstructed signals use `Side.BUY` (strategies are
entry-only). Never raises.

**`async list_for_period(start: date, end: date) → list[tuple[Signal, RiskDecision]]`**
Signals whose Moscow calendar date falls in `[start, end]`, oldest first.
Empty list when none.

## `zarabot.db.snapshots`

Sole owner of `daily_snapshots`. Write failures are logged at ERROR and not
propagated (rule 12).

**`DailySnapshot(trade_date: date, opening_equity: Decimal, closing_equity: Decimal | None, cash: Decimal, realised_pnl: Decimal, unrealised_pnl: Decimal, open_positions: int, orders_placed: int, benchmark_value: Decimal | None)`**
Frozen snapshot row. `benchmark_value` is `None` when unavailable, never stored
as a stand-in zero by this module.

**`async write_daily(snapshot: DailySnapshot) → None`**
Upserts on `trade_date`. A second write for the same date updates the row.
Never raises.

**`async list_for_period(start: date, end: date) → list[DailySnapshot]`**
Rows with `trade_date` in `[start, end]`, oldest first. Empty list when none.

## `zarabot.risk.sizing`

Pure. No I/O. 95% coverage required.

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, cap_pct: Decimal) → int`**
Whole lots to buy, rounded down. `min(size_pct% × allocated, cap_pct% ×
allocated, cash) / (lot × price)`. Returns 0 when one lot exceeds the cap or
cash, or when lot cost is not positive. Never negative. Satisfies
`lots × lot × price ≤ cap_pct% × allocated` and `≤ cash`.

## `zarabot.risk.gate`

Pure. No I/O. 95% coverage required. Calls `risk.sizing`. Never mutates `state`.

**`check(signal: Signal, state: PortfolioState, instrument: Instrument, cooldown_active: bool, session_open: bool, halted: bool, now: datetime, config: Config) → RiskDecision`**
Approved with lots, or rejected with exactly one reason. Priority:
`HALTED` → `SESSION_CLOSED` → `INSTRUMENT_NOT_TRADING` → `DUPLICATE_TICKER` →
`MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` → `ZERO_LOTS` →
`POSITION_CAP`. `MAX_POSITIONS` at or above the configured maximum. `SELL`
signals are never approved. Instrument is trading iff `trading_status` is
`NORMAL_TRADING`.

## `zarabot.lifecycle.exits`

Pure. No I/O. 95% coverage required. Never consults halt.

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int, config: Config) → ExitTrigger | None`**
`STOP_LOSS` iff `price ≤ stop_price` and `stop_protection is LOCAL`.
`TAKE_PROFIT` iff `price ≥ target_price`. `MAX_AGE` iff
`trading_days_open >= max_holding_days` and `session.in_closing_window(now)`.
Precedence: stop, then target, then age. Inclusive at stop and target. Raises
`ValueError` on naive `now`.

## `zarabot.strategies.base`

Pure protocol. Strategies enter only; `lifecycle.exits` exits.

**`Strategy` protocol** — `name: str`, `lookback: int`

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
`BUY` or `None`. Never `SELL`. `None` when `len(candles) < lookback` or the
series is degenerate. Deterministic. Runtime-checkable.

## `zarabot.strategies.ma_crossover`

Pure. Fast SMA 10 vs slow SMA 30. `lookback` is 31.

**`MovingAverageCrossover`** — `name = "ma_crossover"`, `lookback = 31`

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
`BUY` when the fast SMA crosses above the slow SMA on the latest bar. `None`
when short, flat, or no golden cross. Never `SELL`.

## `zarabot.strategies.rsi_reversion`

Pure. RSI period 14, oversold below 30. `lookback` is 15.

**`RSIReversion`** — `name = "rsi_reversion"`, `lookback = 15`

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
`BUY` when RSI of closes is below 30. `None` when short, flat, or not oversold.
Never `SELL`.

## `zarabot.strategies.momentum`

Pure. Breakout above the prior 20-bar high. `lookback` is 21.

**`MomentumBreakout`** — `name = "momentum"`, `lookback = 21`

**`evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
`BUY` when the latest close exceeds the high of the prior 20 bars. `None` when
short, flat, or not a breakout. Never `SELL`.

## `zarabot.strategies.ml_model`

Load at startup (I/O); `evaluate` is pure. Absent from the registry when
`ML_MODEL_PATH` is unset.

**`FEATURE_NAMES`** — `("return_1", "return_5", "high_low_range", "close_sma_10")`

**`CONFIDENCE_THRESHOLD`** — `Decimal("0.60")`. Equal or above is a buy.

**`ModelLoadError`** — missing or unreadable model file.

**`ModelContractError`** — manifest names or order differ from `FEATURE_NAMES`.

**`LoadedModel`** — `name = "ml_model"`, `lookback = 11`. Constructed only via `load`.

**`load(path: Path) → LoadedModel`**
Reads a joblib bundle `{"model", "features"}`. Raises `ModelLoadError` or
`ModelContractError`. Never called on the trading path.

**`LoadedModel.evaluate(self, ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
`BUY` when `predict_proba` buy-class probability ≥ threshold. `None` when short,
flat, or below threshold. Never `SELL`.

**`build_features(candles: list[Candle]) → list[float]`**
Pure. Values in `FEATURE_NAMES` order from the most recent `lookback` candles.
Raises `ValueError` when given fewer candles than `lookback`. Sole owner of
feature construction.

## `zarabot.strategies.registry`

**`enabled(config: Config) → list[Strategy]`**
Instantiates the names in `config.enabled_strategies`. Unknown names raise
`ConfigError` naming `ENABLED_STRATEGIES`. `ml_model` is omitted when
`ml_model_path` is `None`; otherwise `ml_model.load` is called and
`ModelLoadError` / `ModelContractError` propagate.

## `zarabot.broker.client`

The only module that calls the broker. Sandbox is selected by
`INVEST_GRPC_API_SANDBOX` endpoint, never `post_sandbox_*`. Never passes
`confirm_margin_trade=True`. Never logs or raises the token. Prices are `Decimal`.

**Exceptions:** `InstrumentNotFound`, `BrokerUnavailable`, `BrokerRateLimited`
(with `retry_after: Decimal | None`), `OrderRejected` / `StopOrderRejected`
(with `reason: str`), `OrderNotFound`.

**`async get_instrument(ticker: str) → Instrument`**
**`async get_candles(figi: str, interval, since: datetime, until: datetime) → list[Candle]`**
Oldest-first. Empty list when none. `ValueError` on naive datetimes.
**`async get_last_price(figi: str) → Decimal`**
**`async get_portfolio() → PortfolioState`**
Broker-authoritative cash and holdings.
**`async get_trading_schedule(days: int) → list[SessionInfo]`**
**`async post_market_order(key: str, figi: str, side: Side, lots: int) → OrderRecord`**
`confirm_margin_trade=False`. Raises `OrderRejected`.
**`async post_stop_loss(key: str, figi: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
GTC market stop-loss, `confirm_margin_trade=False`.
**`async cancel_stop_order(stop_order_id: str) → None`**
Idempotent; already-cancelled/executed is not an error.
**`async list_stop_orders() → list[StopOrderRecord]`**
**`async get_max_lots(figi: str) → int`**
Buy-side market max lots.
**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
Actual commission, never estimated.
**`async get_order_state(key: str) → OrderRecord`**
Lookup by `order_id_type=ORDER_ID_TYPE_REQUEST`. Raises `OrderNotFound`.

## `zarabot.market.session`

Cached broker calendar. Closed when the schedule is missing. Never hardcodes
weekdays.

**`async refresh(days: int) → None`**
Loads `broker.client.get_trading_schedule`. On `BrokerUnavailable` /
`BrokerRateLimited`, caches nothing, logs WARNING once (rule 10).

**`is_open(now: datetime) → bool`**
True iff `now` is in a trading session, inclusive of `start`, exclusive of
`end`. `False` when uncached. Raises `ValueError` on naive `now`.

**`current_session(now: datetime) → SessionInfo | None`**
The trading session containing `now`, or `None`.

**`in_closing_window(now: datetime, minutes: int) → bool`**
Delegates to `SessionInfo.in_closing_window` for the current session.

**`next_open(now: datetime) → datetime`**
Earliest future trading `start` after `now`. Returns `now` when none is cached
(caller should refresh).

## `zarabot.market.data`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
Daily candles per ticker, oldest-first. A failing ticker is omitted and logged
at WARNING; the rest of the batch returns. Never pads. Raises `ValueError` on
naive `now`.

## `zarabot.state.halt`

Sole owner of `halt_state`. A halt suspends entries only.

**`async is_halted() → bool`**
**`async current() → HaltState | None`**
The singleton row, or `None` if missing.

**`async halt(reason: HaltReason, detail: str, at: datetime) → None`**
Persists halt. No-op when already halted. Raises `ValueError` on naive `at`.

**`async resume(actor: str, at: datetime) → bool`**
Clears the halt and records `actor`. `False` when not halted.

## `zarabot.pnl`

Commission is never estimated. `realised` uses the stored net figure on a closed
row. Positive `daily_loss_pct` is a loss versus the snapshot opening baseline.

**`realised(position: Position) → Decimal`**
**`unrealised(position: Position, price: Decimal) → Decimal`**
Mark-to-market vs entry, `lots × lot_size` units.

**`async daily_loss_pct(now: datetime) → Decimal`**
`(opening_equity - current_equity) / opening_equity × 100`. Writes today's
snapshot from the broker portfolio when the row is missing (rule 12).

**`async benchmark_return(start: date, end: date) → Decimal | None`**
Equal-weight buy-and-hold over the watchlist. `None` if any constituent's
prices are missing, never zero-filled.

## `zarabot.execution.orders`

Owns order submission, per-ticker and global submission locks, crash recovery,
and stop-order remedies. Write-then-send. Never resubmits an entry. Never
passes `confirm_margin_trade=True`. Alerts are ERROR logs until
`telegram.notifier` exists.

**`ExitFailed`**
Raised when an exit is rejected or the broker is unreachable. Caller retries.

**`async open_position(signal: Signal, lots: int, instrument: Instrument) → Position`**
Records `SUBMITTING` before `post_market_order`. Clamps lots to
`get_max_lots`; a maximum of 0 records a rejection and raises `OrderRejected`.
Opens LOCAL from the fill, then places the stop (3 attempts). Stop failure
leaves the position LOCAL and open. Partial entry opens filled lots only.

**`async close_position(position: Position, trigger: ExitTrigger) → Position`**
Bot-initiated exit for any trigger. Cancels the standing stop first only when
`EXCHANGE`, then market-sells until flat. Records `trigger` on the order row.
Raises `ValueError` for `STOP_LOSS` only when the position is `EXCHANGE`.
Raises `ExitFailed` on reject or unavailability. Never blocked by halt,
cooldown, or risk limits.

**`async resolve_unfinished(now: datetime) → list[OrderRecord]`**
Queries `get_order_state` by key; never resubmits. `OrderNotFound` settles as
`REJECTED` / never-placed. A discovered entry fill opens via `get_instrument`
and today's `db.signals` row (else strategy `ma_crossover`). A discovered exit
fill closes with the order row's `exit_trigger`. A missing trigger is a data
defect: alert and leave the position open. Raises `ValueError` on naive `now`.

**`async place_protective_stop(position: Position, instrument: Instrument) → Position`**
Places a standing stop on an unprotected position. Idempotent when already
EXCHANGE. Does not unwind.

**`async adopt_existing_stop(position: Position, stop: StopOrderRecord) → Position`**
Binds a live broker stop locally without posting a second one.

**`async cancel_orphaned_stop(stop: StopOrderRecord) → None`**
Cancels a live stop with no matching open position; settles `ORPHANED`.

**`async replace_stop(position: Position, instrument: Instrument) → Position`**
Cancels the standing stop, then places a replacement at the stored stop price.

**`async close_executed_stop(position: Position, fill_price: Decimal) → Position`**
Closes from an exchange-executed stop with `exit_trigger=STOP_LOSS` and starts
the cooldown. Never submits a sell.

## `zarabot.broker.reconcile`

Observes and records. Never places or cancels an order. SQL for `reconciliations`
lives here (same ownership pattern as `state.halt` / `halt_state`). Alerts go
through `telegram.notifier.alert`.

**`async reconcile(now: datetime) → ReconciliationReport`**
Compares `get_portfolio()` to `list_open()`. Local-only → close `EXTERNAL` at
last price with `order=None` and **no** `orders` row. Broker-only →
`positions.adopt`. Lot mismatch → `positions.update_lots`. Stop discrepancies
are reported (`STOP_MISSING`, `STOP_ORPHAN`, `STOP_MISPRICED`, `STOP_ADOPTABLE`,
`STOP_DUPLICATE`) and not acted on. A duplicate names both live stops.
Idempotent against an unchanged broker. Raises `ValueError` on naive `now`.

## `zarabot.telegram.notifier`

Never raises. Never includes a token. Telegram outages cannot delay trading.

**`async alert(text: str, urgent: bool = False) → None`**
Sends to `TELEGRAM_CHAT_ID`. Retries on failure, then logs and returns. A body
containing either token is dropped and replaced with an incident notice.

## `zarabot.telegram.commands`

One handler per brief command. Authorised `TELEGRAM_CHAT_ID` only; a mismatch
logs at INFO with the chat id and neither replies nor changes state. Replies
over 4096 characters are truncated with an omission count. No command mutates a
risk limit. `/halt` and `/resume` delegate to `state.halt` only. `/report` calls
an injected `async (start: date, end: date) → str` matching
`reporter.weekly.build`; when unset it replies `report unavailable`. `/status`
reads today's P&L and `orders_placed` from `daily_snapshots` (both 0 if none).
Send failures retry then log and never raise.

**`set_report_builder(builder: ReportBuilder | None) → None`**
Installs or clears the `/report` callable. Wired by `app.startup` once
`reporter.weekly` exists.

**`async status/positions/history/pnl/halt/resume/strategies/report/help(update, context) → None`**
PTB command handlers.

**`build_application() → Application`**
Registers every command on a python-telegram-bot `Application`.

## `zarabot.reporter.weekly`

Undefined metrics are `N/A`, never `0`. Empty weeks are valid. Over 4096
characters, sections drop in order: exit-trigger distribution, cooldown counts,
worst trade; the omission is noted.

**`async build(start: date, end: date) → str`**
P&L vs benchmark, per-strategy, win rate, worst trade, exit-trigger
distribution, cooldown-blocked signal count, gapped intended-vs-actual exits.

**`async send(now: datetime) → None`**
Builds the Moscow week containing `now` and sends via `telegram.notifier.alert`.
Failure alerts and never raises. Raises `ValueError` on naive `now`.

## `zarabot.ops.backup`

SQLite backup API only, never a raw copy of a live file. Failure alerts and
never raises; trading continues.

**`async run(db_path: Path, backup_dir: Path) → Path`**
Writes `zarabot-<UTC stamp>.db` into `backup_dir` via `sqlite3.Connection.backup`.
On failure, alerts and still returns the intended path.

**`async prune(backup_dir: Path, retention_days: int) → int`**
Deletes `zarabot-*.db` files whose mtime is strictly older than `retention_days`.
Returns how many were removed. Missing directory → `0`.

## `zarabot.app.startup`

Fixed order. No entry before the ready alert. `StartupError` after an alert
when Telegram credentials are present. Sleep-on-failure belongs to `__main__`.
Wires `/report` to `reporter.weekly.build`. Applies stop remedies from
reconciliation via `execution.orders`.

**`StartupError`**
Raised when startup aborts. No trading has begun.

**`AppContext`**
Frozen: `config`, `strategies`, `halt`, `reconciliation`. Lives here, not in
`models`.

**`async start() → AppContext`**
`config.load` → `logging_setup.configure` → `db.migrations.apply` →
`strategies.registry.enabled` → `market.session.refresh` →
`execution.orders.resolve_unfinished` → `broker.reconcile.reconcile` plus stop
remedies → restore halt → ready `alert`.

## `zarabot.app.loops`

The trading cycle. Exits run before the halt check. Halt blocks entries only.
A failure in one scheduled task never terminates another.

**`async trading_cycle(ctx: AppContext) → None`**
Session closed → return with no broker call. Otherwise: resolve unfinished
orders; refresh prices; close exchange-executed stops via `close_executed_stop`;
evaluate remaining exits (LOCAL stop-loss, take-profit, max age) and submit
via `close_position`; recompute daily P&L and halt on the loss limit; if halted
return; else fetch candles, evaluate strategies, gate, record, and open
approved entries.

**`async run(ctx: AppContext) → None`**
Schedules the trading cycle, daily rollover at session open, nightly backup
with 30-day prune, Sunday 12:00 Moscow weekly report, and a daily heartbeat.
Each task is restarted with exponential backoff after an unhandled exception.

## `zarabot.app.shutdown`

Graceful shutdown. Restarts have no financial consequence: in-flight orders are
settled or left `SUBMITTING`; positions are neither cancelled nor liquidated.

**`async shutdown(ctx: AppContext, signal: int) → None`**
Waits up to 30 seconds for unresolved orders to reach a known state via
`resolve_unfinished`. Remaining `SUBMITTING` rows are left for the next startup.
Never cancels a stop or submits a sell. Alerts, then returns.

## `zarabot.__main__`

Process entry for `python -m zarabot`. No application logic of its own.

**`main() → int`**
Installs `SIGTERM`/`SIGINT` handlers that call `app.shutdown.shutdown`, runs
`app.startup.start` then `app.loops.run`. Returns 0 on a clean shutdown. On
`StartupError` sleeps 30 seconds and returns non-zero (rule 15).
