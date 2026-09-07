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
- `RejectionReason` — `HALTED`, `SESSION_CLOSED`, `INSTRUMENT_NOT_TRADING`, `DUPLICATE_TICKER`, `MAX_POSITIONS`, `COOLDOWN_ACTIVE`, `INSUFFICIENT_CASH`, `ZERO_LOTS`, `PORTFOLIO_EXPOSURE`, `BROKER_LOT_LIMIT`
  (`POSITION_CAP` was withdrawn in v1.30 and `PORTFOLIO_EXPOSURE` took its
  place. The old reason could not bind, because `config.load()` refused any
  configuration in which the per-position cap was the binding minimum, while
  `/resume` listed it as an active control (#15). `signals.rejection_reason` is
  plain TEXT with no CHECK enumerating reasons, so no migration is required.)
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

**`OrderRecord(key: str, ticker: str, figi: str, side: Side, intent: str, lots: int, status: OrderStatus, filled_lots: int | None, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None, created_at: datetime, settled_at: datetime | None, exit_trigger: ExitTrigger | None = None, broker_order_id: str | None = None, commission_alerted_at: datetime | None = None)`**
Client-keyed order. `intent` is `ENTRY` or `EXIT`. `exit_trigger` is non-null
exactly when `intent` is `EXIT` (`STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`).
`broker_order_id` is the broker's own identifier where the bot knows it — a row
describing an execution the exchange performed is filed under a key the broker
has never seen. `commission_alerted_at` is set once, when the owner is first
told this row's commission is unknown (#8).

**`StopOrderRecord(key: str, stop_order_id: str | None, position_id: int, ticker: str, lots: int, stop_price: Decimal, status: StopOrderStatus, created_at: datetime, settled_at: datetime | None)`**
Standing stop-loss tracked locally.

**`OperationRecord(id: str, figi: str, ticker: str, occurred_at: datetime, commission: Decimal, payment: Decimal, price: Decimal | None, quantity: int | None, operation_type: str = "", state: str = "", parent_operation_id: str | None = None)`**
Broker operation including actual commission. `payment` may be negative (a
debit). `operation_type` and `state` are the broker's own enum names
(`OPERATION_TYPE_SELL`, `OPERATION_STATE_EXECUTED`, …); `parent_operation_id`
ties a fee to the trade that incurred it (#11).

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
`position_size_pct: Decimal`, `stop_loss_pct: Decimal`,
`take_profit_pct: Decimal`, `max_holding_days: int`, `max_open_positions: int`,
`reentry_cooldown_minutes: int`, `daily_loss_limit_pct: Decimal`,
`watchlist: tuple[str, ...]`, `enabled_strategies: tuple[str, ...]`,
`ml_model_path: Path | None`, `poll_interval_seconds: int`, `db_path: Path`,
`backup_dir: Path`, `log_level: str`, `tz: str`, `ssl_tbank_verify: bool = True`,
`price_max_age_seconds: int = 120`, `price_max_move_pct: Decimal = Decimal("20")`,
`cash_reserve_pct: Decimal = Decimal("1")`, `allow_foreign_holdings: bool = False`.
`max_position_pct` was withdrawn in v1.30 (#15): the cross-field check that
guaranteed `position_size_pct <= max_position_pct` made the per-position cap
unreachable in sizing while `/resume` displayed it as an active limit. The
field is gone from `Config` and `MAX_POSITION_PCT` is read by nothing.

**`load() → Config`**
`tinvest_token` and `tinvest_account_id` are resolved for the mode in force: in
`sandbox` they come from `TINVEST_TOKEN_SANDBOX` and `TINVEST_ACCOUNT_ID_SANDBOX`,
each falling back to its base variable when unset or blank; live ignores both.
Callers see one token and one account id and must not branch on `trading_mode`
to pick credentials.
Reads the brief's environment-variable table. Applies documented defaults to
non-risk optional variables. `SSL_TBANK_VERIFY` defaults to `true`; only the
strings `true` and `false` are accepted. `PRICE_MAX_AGE_SECONDS` defaults to
`120`; `PRICE_MAX_MOVE_PCT` defaults to `20`. `CASH_RESERVE_PCT` defaults to
`1` and is bounded `0`-`50` **inclusive at both ends**, unlike every other
percentage here: it is the slice of cash `risk.sizing` holds back so fees and
rounding cannot make an approved order unaffordable, `0` is a legitimate
choice, and a reserve above half the cash is a configuration error rather than
a preference. `ALLOW_FOREIGN_HOLDINGS` defaults
to `false` and, like `SSL_TBANK_VERIFY`, accepts only the strings `true` and
`false`; it is not a risk limit, so an unset or blank value takes the default.
`app.startup` acts on it — `config` only exposes it.
When `ssl_tbank_verify` is false,
`load()` logs a CRITICAL line naming that certificate verification is disabled
on the connection that carries the trading token; the token value is never
logged. Never substitutes a default for missing `ALLOCATED_CAPITAL`. Raises
`ConfigError` naming the variable when: a required variable is missing or empty;
a percentage is `<= 0` or `> 100` (`CASH_RESERVE_PCT` is outside `0`-`50`);
`MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeds 100; `TAKE_PROFIT_PCT` is not
greater than `STOP_LOSS_PCT`; `WATCHLIST` is empty; `TRADING_MODE` is not
`live` or `sandbox`; `SSL_TBANK_VERIFY` or `ALLOW_FOREIGN_HOLDINGS` is not
`true` or `false`; or `ML_MODEL_PATH` is set but unreadable.

**`get() → Config`**
Returns the process-wide `Config`, calling `load()` on the first call only and
returning that same instance thereafter — `get() is get()`. Memoised with
`functools.lru_cache`, so it fills lazily on first call, never at import.
`load()` re-reads every environment variable, re-parses every `Decimal` and
stats `ML_MODEL_PATH` each time; `broker.client` was paying that three times per
order (#18). `app.startup` still calls `load()` first, so a bad configuration
fails before anything else; every later reader uses `get()`. A `ConfigError`
caches nothing — the next call re-reads. Tests reset the memo with
`get.cache_clear()`; running code never should.

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
version. Idempotent. `applied_at` is written via `clock.now()`. At the start
of `apply`, issues `PRAGMA foreign_keys = ON`, `PRAGMA busy_timeout = 30000`,
and `PRAGMA journal_mode = WAL` on the given connection. Does not call
`aiosqlite.connect` and does not close `conn`. Ships `003_position_events.sql`,
which creates `position_events`.

## `zarabot.db.connection`

Sole owner of the process-wide SQLite connection. The only module that calls
`aiosqlite.connect` or closes that connection. Never connects at import.
Opened by `app.startup`, closed by `app.shutdown`. Does not apply migrations.

**`DatabaseNotOpenError`**
Raised when `shared` is called before `connect` or after `disconnect` (rule 30).
Never opens a fallback connection.

**`DatabaseAlreadyOpenError`**
Raised when `connect` is called while a process connection is already open.

**`async connect(path: str) → aiosqlite.Connection`**
Opens the SQLite file at `path`, stores it as the process connection, and issues
`PRAGMA journal_mode = WAL`, `PRAGMA foreign_keys = ON`, and
`PRAGMA busy_timeout = 30000`, and sets `row_factory` to `aiosqlite.Row` once
for the process. Raises `DatabaseAlreadyOpenError` if already open. Does not
apply migrations.

**`shared() → aiosqlite.Connection`**
Returns the open process connection. Raises `DatabaseNotOpenError` when none is
open. Must never open a connection as a side effect.

**`transaction() → async context manager yielding aiosqlite.Connection`**
The sole transaction owner: `async with transaction() as conn:`. Holds one
process-wide lock, issues `BEGIN IMMEDIATE`, commits on clean exit and rolls
back on exception. **Reentrant** — a nested acquisition on the same task joins
the outer transaction and only the outermost exit commits, so
`broker.reconcile` can call `db.positions` writers from inside its own
transaction. No other module issues `BEGIN`, `commit` or `rollback` (rule 31),
and reads take no transaction. Raises `DatabaseNotOpenError` when no connection
is open.

**`async disconnect() → None`**
Closes the process connection and forgets it. Idempotent when already closed.
After it returns, `shared()` raises `DatabaseNotOpenError`.

## `zarabot.db.positions`

Sole owner of `positions` rows and of `position_events`. Never deletes. All SQL
runs on `db.connection.shared()`. Every mutation writes one `position_events`
row in the same transaction.

**`PositionStateError`**
Illegal transition, pairing violation, or duplicate open ticker.

**`PositionEvent(position_id: int, occurred_at: datetime, event: str, detail: str)`**
Frozen dataclass. `event` is one of `OPENED`, `STOP_PROTECTION_CHANGED`,
`LOTS_ADJUSTED`, `CLOSED`, `REALISED_RECOMPUTED`, `ADOPTED`. `detail` is a JSON
object as text. `occurred_at` is timezone-aware UTC.

**`async open(signal: Signal, order: OrderRecord, instrument: Instrument, stop: Decimal, target: Decimal, opened_at: datetime) → Position`**
Inserts an open position with `stop_protection=LOCAL` and `stop_order_key=None`.
Raises `PositionStateError` if an open row for the ticker exists. Raises
`ValueError` on a naive `opened_at`.

**`async set_stop_protection(position_id: int, protection: StopProtection, stop_order_key: str | None) → Position`**
EXCHANGE requires a key; LOCAL forbids one. Raises `PositionStateError` on a
pairing violation or missing row.

**`async close(position_id: int, trigger: ExitTrigger, exit_price: Decimal, closed_at: datetime, order: OrderRecord | None, exit_commission: Decimal | None = None) → Position`**
Atomic OPEN→CLOSED. Concurrent callers: exactly one succeeds. Realised P&L is
`(exit-entry)×lots×lot_size` minus commission on both legs. Opening commission
comes from `db.orders.get(open_order_key)`, never a raw `SELECT` on `orders`
(0 when the row is missing or commission is unknown). `order` is `None` only for
`EXTERNAL`; any other pairing raises `ValueError`. `exit_commission` is the
closing fee where there is no closing order to carry it — EXTERNAL only, else
`ValueError` — netted from realised P&L and stored in `positions.exit_commission`
(#11). Clears `stop_protection` to
LOCAL and `stop_order_key`. Never deletes. Raises `PositionStateError` if already
closed or absent. Raises `ValueError` on a naive `closed_at`.

**`async recompute_realised(position_id: int) → Position`**
Rewrites `realised_pnl` for a closed position from the commissions currently on
its two orders, or from the stored `exit_commission` for an `EXTERNAL` close,
which has no closing order to re-read. Raises `PositionStateError` if absent or still open. The only
mutation permitted on a closed row.

**`async list_open() → list[Position]`**
Open positions, or `[]`. Never `None`.

**`async list_closed() → list[Position]`**
Closed positions, newest `exit_at` first, or `[]`. Never `None`.

**`async get(position_id: int) → Position | None`**
`None` when absent.

**`async adopt(instrument: Instrument, lots: int, average_price: Decimal, adopted_at: datetime, open_order_key: str) → Position`**
Open LOCAL adopted position, strategy `ADOPTED`, stop/target from average price
at configured `stop_loss_pct` / `take_profit_pct`. `open_order_key` is the key
of the bot's own unresolved `ENTRY` order for the ticker, supplied by the
caller — the synthetic `ADOPTED-{figi}` had no order row and could not satisfy
the schema's foreign key (#42). A key naming no order propagates the
`IntegrityError`; only a duplicate open position becomes `PositionStateError`,
for both `open` and `adopt`.

**`async update_lots(position_id: int, lots: int) → Position`**
Writes the broker's lot count onto an open row. Raises `PositionStateError` if
the row is absent, already closed, or `lots` is not positive.

**`async list_events(position_id: int) → list[PositionEvent]`**
That position's events, oldest first, or `[]`. Never `None`.

## `zarabot.db.job_runs`

Sole owner of `job_runs`. All SQL runs on `db.connection.shared()` inside
`transaction()`. Exists because schedule state lived in module globals, so every
restart re-armed every job and a restart through the report hour lost the week
silently (#27).

**`async has_run(job: str, period_key: str) → bool`**
Whether `job` has completed for that period. `period_key` is a Moscow date for a
daily job, a week-start date for the weekly report.

**`async mark_run(job: str, period_key: str, ran_at: datetime) → None`**
Records completion. Idempotent, keeping the first `ran_at`. Raises `ValueError`
on a naive datetime.

**`async last_run(job: str) → datetime | None`**
Most recent completion, or `None`. Makes "did the weekly report go out?"
answerable from the database.

## `zarabot.db.trading_days`

Sole owner of `trading_days`. All SQL runs on `db.connection.shared()` inside
`transaction()`. The one table here that is **not** append-only: a day is
overwritten by a newer observation, because it records what is true about a
date rather than what happened. Exists because the broker serves no schedule
before today (§2.1), so the past must be remembered rather than fetched (#45).

**`async record_many(sessions: list[SessionInfo]) → int`**
Upserts one row per day on the Moscow date, newer observation winning, in one
transaction. Returns how many were written. A session with no `start` — a
non-trading day, which carries no timestamps — is skipped, not stored under a
null key.

**`async list_since(start: date) → list[SessionInfo]`**
Recorded days from `start` onwards, oldest first. `[]` when none, never `None`.

**`async earliest() → date | None`**
The oldest recorded date, or `None` when nothing has been recorded. Coverage is
defined against this.

## `zarabot.db.orders`

Sole owner of `orders` rows and status transitions. All SQL runs on
`db.connection.shared()`. Never calls `aiosqlite.connect` and never closes the
connection. `record_submitting` must complete before any broker call with the
same key. Never resubmit; recover by querying the key. Mutations
(`record_submitting`, `settle`, `record_commission`) run inside
`BEGIN IMMEDIATE`. `get` does not commit, so it can run inside another
repository's open transaction.

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

**`async settle(key: str, status: OrderStatus, filled_lots: int, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None, broker_order_id: str | None = None) → OrderRecord`**
Records a terminal outcome, `commission`, and `settled_at`. `commission=None`
means not yet known and reads back distinct from zero. Raises `OrderStateError`
if the row is missing, already terminal, or `status` is not terminal.

**`async get(key: str) → OrderRecord | None`**
The order, or `None` when absent. No longer expected to be `None` for an
adopted position, which since #42 points at a real order row.

**`async record_commission(key: str, commission: Decimal) → OrderRecord`**
The one field settable on a terminal row. Raises `OrderStateError` if absent.

**`async mark_commission_alerted(key: str, at: datetime) → OrderRecord`**
Records that the owner has been told once about this row's unknown commission.
Idempotent — a row already marked keeps its original timestamp. Raises
`OrderStateError` if absent, `ValueError` on a naive `at`. The 24-hour
staleness policy stays in `ops.commissions`; this records only the fact (#8).

**`async list_missing_commission(since: datetime, until: datetime) → list[OrderRecord]`**
`FILLED` orders in the period whose commission is still unknown. Empty list when
none. Raises `ValueError` on naive datetimes.

**`async list_unresolved() → list[OrderRecord]`**
`SUBMITTING` or `SUBMITTED`, oldest first. Empty list when none.

## `zarabot.db.stop_orders`

Sole owner of `stop_orders` rows. All SQL runs on `db.connection.shared()`.
Never calls `aiosqlite.connect` and never closes the connection. Reuses
`DuplicateOrderError` and `OrderStateError` from `zarabot.db.orders`.
Mutations (`record_placing`, `activate`, `settle`) run inside `BEGIN IMMEDIATE`.

**`async record_placing(key: str, position_id: int, ticker: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
Inserts `PLACING` with `created_at=clock.now()`. Raises `DuplicateOrderError`
on a repeated key. Foreign-key and live-stop unique-index violations propagate
as `IntegrityError`.

**`async activate(key: str, stop_order_id: str) → StopOrderRecord`**
Sets status `ACTIVE` and stores the broker identifier. Raises `OrderStateError`
if the row is missing or already terminal.

**`async settle(key: str, status: StopOrderStatus, settled_at: datetime) → StopOrderRecord`**
Terminal statuses: `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`. Raises
`OrderStateError` on a missing row, a transition out of a terminal status, or a
non-terminal target. Raises `ValueError` on naive `settled_at`.

**`async active_for_position(position_id: int) → StopOrderRecord | None`**
The single `PLACING` or `ACTIVE` stop for the position, or `None`. Raises
`OrderStateError` if more than one standing stop exists.

**`async list_active() → list[StopOrderRecord]`**
Every `ACTIVE` stop, oldest first. Empty list when none.

## `zarabot.db.cooldowns`

Sole owner of `cooldowns` rows. All SQL runs on `db.connection.shared()`.
Never calls `aiosqlite.connect` and never closes the connection.

**`async start(ticker: str, at: datetime) → None`**
Inserts `started_at`, or overwrites only when `at` is strictly newer. Raises
`ValueError` on a naive `at`. Write failures are logged at ERROR and not
propagated (rule 12). Access before `connect` or after `disconnect` raises
`DatabaseNotOpenError` (rule 30).

**`async is_active(ticker: str, now: datetime, minutes: int) → bool`**
True while `now - started_at < minutes`. False when no row exists, and False
exactly at the boundary. Raises `ValueError` on a naive `now`. Access before
`connect` or after `disconnect` raises `DatabaseNotOpenError` (rule 30).

**`async active_until(ticker: str, minutes: int) → datetime | None`**
`started_at + minutes`, or `None` when no cooldown is recorded. Access before
`connect` or after `disconnect` raises `DatabaseNotOpenError` (rule 30).

## `zarabot.db.signals`

Sole owner of `signals` rows. All SQL runs on `db.connection.shared()`.
Never calls `aiosqlite.connect` and never closes the connection.

**`async record(signal: Signal, decision: RiskDecision) → None`**
Inserts the signal with `APPROVED`/`REJECTED`, lots or rejection reason, and
`order_key=None`. Reconstructed signals use `Side.BUY` (strategies are
entry-only). Write failures are logged at ERROR and not propagated (rule 12).
Access before `connect` or after `disconnect` raises `DatabaseNotOpenError`
(rule 30).

**`async list_for_period(start: date, end: date) → list[tuple[Signal, RiskDecision]]`**
Signals whose Moscow calendar date falls in `[start, end]`, oldest first.
Empty list when none. Access before `connect` or after `disconnect` raises
`DatabaseNotOpenError` (rule 30).

## `zarabot.db.snapshots`

Sole owner of `daily_snapshots`. All SQL runs on `db.connection.shared()`.
Never calls `aiosqlite.connect` and never closes the connection.

**`DailySnapshot(trade_date: date, opening_equity: Decimal, closing_equity: Decimal | None, cash: Decimal, realised_pnl: Decimal, unrealised_pnl: Decimal, open_positions: int, orders_placed: int, benchmark_value: Decimal | None)`**
Frozen snapshot row. `benchmark_value` is `None` when unavailable, never stored
as a stand-in zero by this module.

**`async write_daily(snapshot: DailySnapshot) → None`**
Upserts on `trade_date`. A second write for the same date updates the row.
Write failures are logged at ERROR and not propagated (rule 12). Access before
`connect` or after `disconnect` raises `DatabaseNotOpenError` (rule 30).

**`async list_for_period(start: date, end: date) → list[DailySnapshot]`**
Rows with `trade_date` in `[start, end]`, oldest first. Empty list when none.
Access before `connect` or after `disconnect` raises `DatabaseNotOpenError`
(rule 30).

## `zarabot.risk.sizing`

Pure. No I/O. 95% coverage required.

**`position_budget(allocated: Decimal, size_pct: Decimal) → Decimal`**
`size_pct% × allocated` — the intended cost of one position, before headroom and
the cash reserve narrow it further. The budget's single definition:
`size_position` bounds an order with it and `app.startup` compares it against a
lot cost to decide whether any order is possible at all (rule 36). Callers use
this rather than recomputing the formula — a second copy on the money path is a
drift hazard, and it would live in a module carrying a 70% coverage floor.
Never negative; `allocated ≤ 0` is refused by `config.load()`.

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, open_cost: Decimal, reserve_pct: Decimal) → int`**
Whole lots to buy, rounded down (truncating division, so no quotient rounded at
the context precision can return a lot the money cannot pay for).
`min(position_budget(allocated, size_pct), allocated − open_cost, cash × (100 − reserve_pct)%)
/ (lot × price)`. Returns 0 when one lot exceeds the smallest of those three,
when headroom is negative, or when lot cost is not positive. Never negative.
Satisfies `lots × lot × price ≤ allocated − open_cost` and `≤ cash` for every
input. `open_cost` is the summed cost of the positions already open, passed in
because this function is pure; `risk.gate` computes it from `state.positions`.
`reserve_pct` is a buying-power reserve — `config.cash_reserve_pct`, bounded
0–50 — and **never an estimate of commission**: nothing derived from it may be
recorded as one. `cap_pct` was withdrawn in v1.30 (#15): `config.load()`
guaranteed `size_pct ≤ cap_pct`, so the per-position cap could never be the
binding minimum while `/resume` reported it as an active control. The portfolio
headroom replaces it with a ceiling that can bind (#16).

## `zarabot.risk.gate`

Pure. No I/O. 95% coverage required. Calls `risk.sizing`. Never mutates `state`.

**`check(signal: Signal, state: PortfolioState, instrument: Instrument, cooldown_active: bool, session_open: bool, halted: bool, now: datetime, config: Config) → RiskDecision`**
Approved with lots, or rejected with exactly one reason. Priority:
`HALTED` → `SESSION_CLOSED` → `INSTRUMENT_NOT_TRADING` → `DUPLICATE_TICKER` →
`MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` →
`PORTFOLIO_EXPOSURE` → `ZERO_LOTS`. `MAX_POSITIONS` at or above the configured
maximum. `INSUFFICIENT_CASH` when `state.cash < lot_cost`;
`PORTFOLIO_EXPOSURE` when `allocated_capital − open_cost < lot_cost`, where
`open_cost` is `Σ lots × lot_size × entry_price` over `state.positions` —
computed here, so the signature is unchanged and the module stays pure (#16).
`POSITION_CAP` was withdrawn with `Config.max_position_pct` in v1.30 (#15).
`SELL` signals are never approved; they are rejected as `ZERO_LOTS`, evaluated
at that slot in the priority order so a `SELL` cannot pre-empt a
higher-priority reason. A cash reserve too small to cover a lot also surfaces
as `ZERO_LOTS`, since `config.cash_reserve_pct` reaches sizing, not the cash
check. Instrument is trading iff `trading_status` is `NORMAL_TRADING`. A
sector or correlation cap is deliberately absent — see the spec.

## `zarabot.lifecycle.exits`

Pure. No I/O. 95% coverage required. Never consults halt.

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int | None, config: Config) → ExitTrigger | None`**
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

**One `AsyncClient` and one `Config` per process.** The client is created lazily
on first call — never at import — and reused for every later call; configuration
is read through `config.get()`, once, and held alongside it (#18).

**Errors are typed by what they are, not by where they were caught (#23).** Only
`UNAVAILABLE` / `DEADLINE_EXCEEDED` become `BrokerUnavailable`;
`RESOURCE_EXHAUSTED` becomes `BrokerRateLimited`; `NOT_FOUND` becomes the
caller's not-found type where one exists. **Every other gRPC status —
`INVALID_ARGUMENT` foremost — and every non-SDK exception (`AttributeError`,
`TypeError`) propagates as itself**, cause and traceback intact. Callers that
retry on `BrokerUnavailable` must therefore expect a defect to surface rather
than loop forever. `from None` is never used.

**Exceptions:** `InstrumentNotFound`, `BrokerUnavailable`, `PriceRejected`
(quote arrived but is not usable — distinct from `BrokerUnavailable`),
`BrokerRateLimited` (with `retry_after: Decimal | None`), `OrderRejected` /
`StopOrderRejected` (with `reason: str`), `OrderNotFound`.

**`async close() → None`**
Closes the process client and forgets it. Idempotent; a no-op when none is
open. A later call creates a new one, so closing is not a one-way door. Called
only by `app.shutdown`.
**`async get_instrument(ticker: str) → Instrument`**
**`async get_candles(figi: str, interval: CandleInterval, since: datetime, until: datetime) → list[Candle]`**
Oldest-first. Empty list when none. `ValueError` on naive datetimes.
**`async get_last_price(figi: str) → Decimal`**
Reads the quote's `time` directly — a quote object without that attribute
raises `AttributeError`, since a renamed SDK field is an integration break and
not bad data (#33). Rejects non-positive prices, quotes older than
`price_max_age_seconds`, quotes with a missing or naive timestamp, and moves beyond `price_max_move_pct` from
the last accepted price for that instrument (`PriceRejected`). A rejected quote
does not update the last accepted price.
**`async get_portfolio() → PortfolioState`**
Cash is **available RUB** — the sum of `quantity − blocked_lots` over rouble
currency positions (`figi` `RUB000UTSTOM`), floored at zero. Not
`total_amount_currencies`, which converts every currency and counts blocked
funds, so it overstates what an order can actually spend (#16). A currency
position is priced in roubles whatever it holds, so identification is by
instrument, never by the price's currency code.
Broker-authoritative cash and holdings.
**`async get_trading_schedule(days: int) → list[SessionInfo]`**
Requests `exchange="MOEX"` by name — the main equity board, weekends closed —
never a substring match over the 53 MOEX-prefixed exchanges (#43). The range is
anchored to the start of the current UTC day and `days` may not exceed 14;
`ValueError` if it does, because the broker rejects a longer horizon with
`INVALID_ARGUMENT` / `30002` (#39). One `SessionInfo` per day returned, in
order. A day that is not a session — `is_trading_day` false, or `1970-01-01`
timestamps whatever the flag says — comes back as
`SessionInfo(start=None, end=None, is_trading_day=False)`. Empty list when the
exchange is absent from the response.
**`async post_market_order(key: str, figi: str, side: Side, lots: int) → OrderRecord`**
`confirm_margin_trade=False`. Raises `OrderRejected`. `commission` is
`executed_commission` converted with `money_to_decimal`, or `None` until filled.
`EXECUTION_REPORT_STATUS_PARTIALLYFILL` maps to `SUBMITTED` — still live at the
broker — with `filled_lots` below `lots` and `settled_at` null; `FILLED` means
`filled_lots == lots` (#10).
**`async post_stop_loss(key: str, figi: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
GTC market stop-loss, `confirm_margin_trade=False`.
**`async cancel_stop_order(stop_order_id: str) → None`**
Idempotent; already-cancelled/executed is not an error.
**`async cancel_order(key: str) → None`**
Cancels a live ordinary order by our own idempotency key
(`ORDER_ID_TYPE_REQUEST`). Idempotent: already filled, already cancelled, or
unknown is not an error, because the authoritative answer comes from the
`get_order_state` that follows. `BrokerUnavailable` / `BrokerRateLimited` on
transport failure only. Used to abandon a partial **entry** remainder, never an
exit remainder (#10).
**`async list_stop_orders() → list[StopOrderRecord]`**
**`async get_executed_stop_fills(since: datetime, until: datetime) → dict[str, OrderRecord]`**
The broker's own record of every stop order that fired in the window, keyed by
`stop_order_id`. Composes `get_stop_orders(status=EXECUTED)` with a
`get_order_state` on each result's `exchange_order_id`
(`ORDER_ID_TYPE_EXCHANGE`), so the returned `OrderRecord` carries
`executed_order_price`, `lots_executed` and `executed_commission` — never a
quote. A stop whose exchange order does not resolve, or that reports no executed
lots, is **omitted** rather than priced by guess: the caller leaves the position
open and retries (rule 33). Empty dict when nothing fired. Raises `ValueError` on
naive datetimes.

**`async get_max_lots(figi: str) → int`**
Buy-side market max lots.
**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
Period cost reconciliation, and the resolution of a sale the bot did not
submit (#11). Not the per-order commission source — `OperationRecord` has no
order id. Carries `operation_type`, `state` and `parent_operation_id` verbatim;
`commission` is set for fee operations identified by type, never by a substring
of a name. Returns only `OPERATION_STATE_EXECUTED` operations.
**`async get_order_state(key: str) → OrderRecord`**
Lookup by `order_id_type=ORDER_ID_TYPE_REQUEST`. Raises `OrderNotFound`.
`commission` is `executed_commission` via `money_to_decimal`, or `None` until filled.
Same partial-fill mapping as `post_market_order`.
**`async get_order_state_by_broker_id(broker_order_id: str) → OrderRecord`**
The same lookup with `ORDER_ID_TYPE_EXCHANGE`, for a row whose `key` the broker
has never seen — a stop the exchange fired on the bot's behalf. The returned
record is keyed by the `broker_order_id` asked about. Raises `OrderNotFound`
(#8).

## `zarabot.market.session`

Cached broker calendar. Closed when the schedule is missing. Never hardcodes
weekdays.

**`async refresh(days: int) → None`**
Loads `broker.client.get_trading_schedule` and **records the whole window** via
`db.trading_days.record_many`, then reloads the history into memory.
Unavailable broker, or a response with no trading sessions, leaves the cache
intact, logs WARNING, and alerts once (rule 10). The latch is cleared on the
success path, so "once" is per incident rather than per process (#32). A failed
history write is logged and does not propagate — degraded age counting must not
stop the bot trading. Does not raise.

**`calendar() → TradingCalendar`**
Recorded history plus the live window, oldest first, one entry per day. Empty
when nothing is known; never `None`. `app.loops` reads this instead of fetching
a fourteen-day schedule every cycle (#19), and it spans the past because the
broker serves no schedule before today (#45).

**`covers(day: date) → bool`**
Whether the recorded calendar reaches back to `day`. `False` with no history.
Lets a caller tell a count it can stand behind from one it cannot — an
uncovered day is simply not counted, and nothing raises.

**`is_open(now: datetime) → bool`**
True iff `now` is in a trading session, inclusive of `start`, exclusive of
`end`. `False` when uncached. Raises `ValueError` on naive `now`.

**`cache_exhausted(now: datetime) → bool`**
True when `now` is at or past the last cached session end, so `is_open` is
`False` because the calendar has run out rather than because the market is
shut. **True when the cache is empty.** Raises `ValueError` on naive `now`.

**`current_session(now: datetime) → SessionInfo | None`**
The trading session containing `now`, or `None`.

**`in_closing_window(now: datetime, minutes: int) → bool`**
Delegates to `SessionInfo.in_closing_window` for the current session.

**`next_open(now: datetime) → datetime`**
Earliest future trading `start` after `now`. Returns `now` when none is cached
(caller should refresh).

## `zarabot.market.data`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
Daily candles per ticker, oldest-first. A ticker whose fetch raises
`BrokerUnavailable`, `BrokerRateLimited` or `InstrumentNotFound` is omitted and
logged at WARNING; the rest of the batch returns. Never pads. Raises
`ValueError` on naive `now`.

A call is **degraded** for a ticker when the fetch fails, or when it returns
fewer than `lookback` candles — none at all included. A short series is still
returned; only a failed fetch omits a ticker.

Degraded calls are counted per ticker on one counter (rule 9): the third in a
row alerts **once**, naming the ticker and the reason — the failure, or the
candles returned against the candles required — and nothing further is sent for
it until a call is not degraded, which clears both its count and its alerted
flag. Tickers crossing in the same call share one alert. The counters are
process-local module state, `_failures` and `_alerted`, cleared by
`sandbox.backtest` between runs the way `market.session`'s cache is.

**Every other exception propagates**, so a renamed SDK field reaches
`app.loops._supervise` as itself rather than being absorbed as a missing
instrument.

## `zarabot.state.halt`

Sole owner of `halt_state`. A halt suspends entries only. All SQL runs on
`db.connection.shared()`. Never calls `aiosqlite.connect` and never closes the
connection. Access before `connect` or after `disconnect` raises
`DatabaseNotOpenError` (rule 30).

**`async is_halted() → bool`**
**`async current() → HaltState | None`**
The singleton row, or `None` if missing.

**`async halt(reason: HaltReason, detail: str, at: datetime) → None`**
Persists halt. Raises `ValueError` on naive `at`. Severity orders the reasons —
`DAILY_LOSS_LIMIT` > `RECONCILIATION_MISMATCH` > `MANUAL`. A strictly more severe
reason replaces a standing halt, rewriting `reason` and `detail` and alerting
through `telegram.notifier`; `halted_at` keeps the moment the halt began, since
the suspension has been continuous. The same reason or a weaker one is a no-op
and sends nothing, so re-halting adds no alert noise (#9).

**`async resume(actor: str, at: datetime) → bool`**
Clears the halt and records `actor`. `False` when not halted.

## `zarabot.pnl`

Commission is never estimated. `realised` uses the stored net figure on a closed
row. Positive `daily_loss_pct` is a loss versus the session-open baseline.
Reads configuration through `config.get()`. Writes nothing: the session-open
`daily_snapshots` row belongs to `app.loops`.

**`realised(position: Position) → Decimal`**
**`unrealised(position: Position, price: Decimal) → Decimal`**
Mark-to-market vs entry, `lots × lot_size` units.

**`async bot_equity() → Decimal`**
`allocated_capital + realised P&L of every closed position + unrealised P&L of
every open position at `get_last_price``. **Never reads broker cash or broker
equity**, so a deposit or a withdrawal cannot move it — a transfer is not a
trading result (#9). With no positions it equals `allocated_capital`, never
`None`.

**`async daily_loss_pct(now: datetime) → Decimal`**
`(opening bot equity − bot_equity()) / allocated_capital × 100`; positive is a
loss. **The denominator is `ALLOCATED_CAPITAL`**, not account equity — dividing
by equity let a 5% limit permit a 10% loss of the allocation (#9). The baseline
is `daily_snapshots.opening_equity` for the Moscow date of `now`, written at the
session open by `app.loops`. With no row for the day, the baseline is
reconstructed as `allocated_capital + realised P&L of every position closed
before today` — deliberately tight, since overnight unrealised movement counts
against today — and `telegram.notifier.alert` fires once per Moscow date.
Raises `ValueError` on a naive `now`.

**`async benchmark_return(start: date, end: date) → Decimal | None`**
Equal-weight buy-and-hold over the watchlist. `None` if any constituent's
prices are missing, never zero-filled.

## `zarabot.execution.orders`

Owns order submission, per-ticker and global submission locks, crash recovery,
and stop-order remedies. Write-then-send. Never resubmits an entry. Never
passes `confirm_margin_trade=True`. Alerts go through `telegram.notifier.alert`.

**`ExitFailed`**
Raised when an exit is rejected or the broker is unreachable. Caller retries.

**`async open_position(signal: Signal, lots: int, instrument: Instrument) → Position`**
Records `SUBMITTING` before `post_market_order`. Clamps lots to
`get_max_lots`; a maximum of 0 records a rejection and raises `OrderRejected`.
Opens LOCAL from the fill, then places the stop (3 attempts). Stop failure
leaves the position LOCAL and open. Broker-reported `commission` is passed
through to `db.orders.settle`. A `SUBMITTED` response with lots filled is a
partial: the remainder is cancelled with `cancel_order`, the order re-read with
`get_order_state`, and the position opened from that read — never from the
pre-cancel response. A failed cancel or re-read writes nothing and raises
`BrokerUnavailable`, leaving the order for recovery. Nothing filled after the
cancel raises `OrderRejected`. A `SUBMITTED` response with zero lots filled is
not cancelled (#10).

**`async close_position(position: Position, trigger: ExitTrigger) → Position`**
Bot-initiated exit for any trigger. Cancels the standing stop first only when
`EXCHANGE`, then submits exactly **one** market sell for the position's whole
lot count. A partial settles nothing and raises `ExitFailed`; the caller retries
under rule 4. Records `trigger` on the order row.
Raises `ValueError` for `STOP_LOSS` only when the position is `EXCHANGE`.
Raises `ExitFailed` on reject or unavailability. Never blocked by halt,
cooldown, or risk limits.

**`async resolve_unfinished(now: datetime) → list[OrderRecord]`**
Queries `get_order_state` by key; never resubmits. `OrderNotFound` settles as
`REJECTED` / never-placed. A discovered entry fill opens via `get_instrument`
and the `db.signals` row for the span between the order's `created_at` and
`now` in Moscow dates; with no match the strategy is `UNATTRIBUTED`, never a
real one, and the reconstructed `reference_price` is the order's `filled_price`
(#11, rule 35). A discovered exit
fill closes with the order row's `exit_trigger`. A missing trigger is a data
defect: alert and leave the position open. An `ENTRY` still `SUBMITTED` with
lots filled runs the cancel-and-re-read above. A terminal `EXIT` that sold part
of a position calls `update_lots` with the unsold remainder, leaves the position
open and alerts; one reaching the position's count closes it (#10). Raises
`ValueError` on naive `now`.

**`async place_protective_stop(position: Position, instrument: Instrument) → Position`**
Places a standing stop on an unprotected position. Idempotent when already
EXCHANGE. Does not unwind.

**`async adopt_existing_stop(position: Position, stop: StopOrderRecord) → Position`**
Binds a live broker stop locally without posting a second one.

**`async cancel_orphaned_stop(stop: StopOrderRecord) → None`**
Cancels a live stop with no matching open position; settles `ORPHANED`.

**`async replace_stop(position: Position, instrument: Instrument) → Position`**
Cancels the standing stop, then places a replacement at the stored stop price.

**`async close_executed_stop(position: Position, fill: OrderRecord) → Position`**
Closes from an exchange-executed stop with `exit_trigger=STOP_LOSS` and starts
the cooldown. Never submits a sell. `fill` is the broker's own record from
`get_executed_stop_fills`; a `filled_price` of `None` raises `ValueError` rather
than substituting a number (#4). `fill.key` — the broker's `exchange_order_id` —
is written to the row's `broker_order_id`, which is what makes a late commission
on it recoverable (#8). A `commission` of `None` does not block the close.

## `zarabot.broker.reconcile`

Observes and records. Never places or cancels an order. SQL for `reconciliations`
lives here (same ownership pattern as `state.halt` / `halt_state`). Alerts go
through `telegram.notifier.alert`. All SQL runs on `db.connection.shared()`.
Never calls `aiosqlite.connect` and never closes the connection. Access before
`connect` or after `disconnect` raises `DatabaseNotOpenError` (rule 30).

**`async reconcile(now: datetime) → ReconciliationReport`**
Compares `get_portfolio()` to `list_open()`. Local-only → the sale is resolved
from `get_operations(position.entry_at, now)`, filtered to the position's `figi`
and the sale operation types: `exit_price` is the quantity-weighted average,
`exit_at` the latest sale timestamp, `exit_commission` the sum of the fee
operations parented to those sales, closed with `order=None` and **no** `orders`
row. With no covering sale, or the feed unavailable, the position **stays open**
and the report carries `{"type": "EXIT_UNRESOLVED", "ticker", "position_id",
"reason"}` with an alert; no exit is ever recorded at a quote or at
`entry_price` (#11, rule 33). Broker-only and
unrecognised → `{"type": "FOREIGN_HOLDING", "ticker", "lots", "average_price"}`
and **no position row is written**; `app.startup` refuses to start on it (rule
32). Broker-only but recognised — the bot has an unresolved `ENTRY` order of its
own for the ticker, the crash-recovery case — → `positions.adopt`, passing that
order's key as `open_order_key` (oldest by `created_at` if somehow more than
one). Lot mismatch →
`positions.update_lots`. Stop discrepancies are reported (`STOP_MISSING`,
`STOP_ORPHAN`, `STOP_MISPRICED`, `STOP_ADOPTABLE`, `STOP_DUPLICATE`) and not
acted on. `STOP_MISPRICED` is reported only when the broker's stop differs from
the position's by a full `min_price_increment` or more, read from
`get_instrument(ticker)` — the broker snaps every posted stop to the tick, and
an exact inequality re-posted all live stops on every restart (v1.47). Nothing
is rounded: no guessed rounded price is written to `positions` or `stop_orders`.
When the increment cannot be read the stop's price is not compared at all; the
failure is alerted and the position's other stop findings still stand (rule 37). `STOP_DUPLICATE` carries `keep` (the stop matching the position's
`stop_order_key`, else the oldest by `created_at`) and `cancel` (every other
identifier); an identifier is `stop_order_id` when known, else the stop's key.
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

## `zarabot.ops.commissions`

Fills in commissions reported after the fill. Never places, cancels, or modifies
an order. `app.loops` schedules this at daily rollover and immediately before
the weekly report.

**`async backfill(since: datetime, until: datetime) → int`**
Re-queries the broker for `list_missing_commission`, records any commission now
present, and `recompute_realised` for affected closed positions. The lookup is
by `get_order_state_by_broker_id(broker_order_id)` when the row carries one —
a stop the exchange fired is filed under a key the broker never saw — and by
`get_order_state(key)` otherwise; never by matching on instrument and time (#8).
Returns how many orders were updated. Alerts only when commission is still
unknown more than 24 hours after the fill (strictly greater than 24h), and
**once per order**, marking it via `db.orders.mark_commission_alerted`; the row
is still re-queried on later runs. Raises `ValueError` on naive datetimes.

## `zarabot.app.startup`

Fixed order. No entry before the ready alert. `StartupError` after an alert
when Telegram credentials are present. Sleep-on-failure belongs to `__main__`.
Wires `/report` to `reporter.weekly.build`. Applies stop remedies from
reconciliation via `execution.orders` — every adjustment type the report can
carry is handled, and an unrecognised one alerts rather than being dropped.
`CLOSED_EXTERNALLY`, `ADOPTED`, `LOTS_ADJUSTED`, `FOREIGN_HOLDING` and
`EXIT_UNRESOLVED` are recognised without a remedy; `EXIT_UNRESOLVED` in
particular does not stop startup, since the shares it names are already gone
(#11).

**`StartupError`**
Raised when startup aborts. No trading has begun.

**`AppContext`**
Frozen: `config`, `strategies`, `halt`, `reconciliation`. Lives here, not in
`models`.

**`async start() → AppContext`**
`config.load` → write `SSL_TBANK_VERIFY` from `config.ssl_tbank_verify` into
`os.environ` (`"true"` / `"false"`) → urgent `alert` when verification is
disabled, before any broker call and carrying no token → `logging_setup.configure` →
`db.connection.connect(config.db_path)` then
`db.migrations.apply(db.connection.shared())` → `strategies.registry.enabled` →
`market.session.refresh` → `execution.orders.resolve_unfinished` →
`broker.reconcile.reconcile` plus stop remedies → refuse to start on a foreign
holding → restore halt → watchlist budget reachability → ready `alert`.
The connection is opened here, not at import. The TLS env write precedes every
broker call. Remedies: `STOP_MISSING` → `place_protective_stop`, `STOP_MISPRICED`
→ `replace_stop`, `STOP_ADOPTABLE` → `adopt_existing_stop`, `STOP_ORPHAN` →
`cancel_orphaned_stop`, and `STOP_DUPLICATE` → `cancel_orphaned_stop` for every
identifier in its `cancel` list while `keep` is retained. The remedy gate does
not skip a report whose only stop adjustment is a duplicate. A type outside the
handled set alerts urgently. A `FOREIGN_HOLDING` adjustment raises
`StartupError` naming every ticker, after alerting, unless
`config.allow_foreign_holdings` is true (rule 32); when it is, the ready alert
names the holdings and they are never traded — reconciliation writes no position
row, so no stop is placed, no exit evaluated and no sale made.

Step 8a compares `instrument.lot × get_last_price(figi)` against
`risk.sizing.position_budget(allocated_capital, position_size_pct)` for every
watchlist ticker (rule 36). No instrument affordable → urgent `alert` naming the
budget and the cheapest lot cost and ticker; some affordable → the unaffordable
ones are named in the ready alert and nothing is escalated; a ticker whose
instrument or price cannot be read, or that prices at zero, is `unknown` and
counted as neither; no readable price at all reports `budget_check=inconclusive`
rather than a blackout it did not observe. **The step never raises
`StartupError`.** An unaffordable budget stops new entries only, and refusing to
start would abandon every open position — no exits, no stop management, no
`MAX_AGE`.

## `zarabot.app.loops`

The trading cycle. Exits run before the halt check. Halt blocks entries only.
A failure in one scheduled task never terminates another.

**`async trading_cycle(ctx: AppContext) → None`**
Step 4 owns the day's opening snapshot: written on the first in-session cycle of
a Moscow day, and only when this process was already up when the session opened —
a later first cycle writes nothing and `pnl` reconstructs the baseline instead,
because a snapshot taken at 14:00 would bury the morning's drawdown. When
`bot_equity` cannot mark a position (`PriceRejected`, `BrokerUnavailable`) the
day's loss is unknowable, so the cycle **skips entries and alerts once, without
halting** — exits at step 3 stand, and the market-data outage counter is left
untouched so a bad quote cannot masquerade as a broker outage.
Session closed → return with no broker call. Otherwise: resolve unfinished
orders; refresh prices; close exchange-executed stops via `close_executed_stop`;
evaluate remaining exits (LOCAL stop-loss, take-profit, max age) and submit
via `close_position`; recompute daily P&L and halt on the loss limit; if halted
return; else fetch candles, evaluate strategies, gate, record, and open
approved entries. A `PriceRejected` for one position omits that ticker and
continues; it does not abort the cycle and does not increment the consecutive
market-data outage counter. Rejections latch like the other alerts in this
module: one alert when a cycle first rejects anything, naming the count, and
none until a cycle rejects nothing and re-arms it.
The calendar for max-age comes from `market.session.calendar()`; this module
never fetches a trading schedule (#19). A position whose entry
`market.session.covers` does not reach is evaluated with
`trading_days_open=None`, suppressing MAX_AGE for it alone, with a latched
alert (#45). A ticker already opened earlier in the
same pass is skipped before the gate and recorded `DUPLICATE_TICKER`, and
`OrderRejected`, `PositionStateError` and `DuplicateOrderError` from
`open_position` are all ordinary outcomes that continue the pass (#24).

Between cycles the loop waits the **longer** of its own escalation
(`poll × 2 ** consecutive failures`) and the broker's `retry_after` hint when
the last failure was a `BrokerRateLimited` carrying one — bounded either way by
`_MAX_BACKOFF`, because this loop also submits exits (rule 2). The hint is
overwritten by every failure and cleared by every success, alongside the
failure counter. Three consecutive rate-limited cycles alert once and the alert
names throttling, not a market-data outage.

**`async run(ctx: AppContext) → None`**
Sole owner of composition. Starts the trading cycle, daily rollover, trading-
schedule `refresh`, commission `backfill` (after rollover, and immediately
before the weekly report), nightly backup with 30-day prune, Sunday 12:00
Moscow weekly report, daily heartbeat, and the Telegram command listener via
`telegram.commands.build_application`. Closed-session cycles call
`cache_exhausted` and alert when the calendar has run out, so exhaustion is
not mistaken for a quiet close. Each task is restarted with exponential
backoff after an unhandled exception.

## `zarabot.app.shutdown`

Graceful shutdown. Restarts have no financial consequence: in-flight orders are
settled or left `SUBMITTING`; positions are neither cancelled nor liquidated.

**`async shutdown(ctx: AppContext, signal: int) → None`**
Calls `app.loops.stop_entries()` **first**, then drains — a drain that runs
first has already looked past the position the next cycle opens (#21).
Waits up to 30 seconds for unresolved orders to reach a known state via
`resolve_unfinished`. Remaining `SUBMITTING` rows are left for the next startup.
Never cancels a stop or submits a sell. Alerts, then calls
`db.connection.disconnect()` so `shared()` raises `DatabaseNotOpenError`, and
last of all `broker.client.close()`. Closing the database is that one call and
nothing else, and the broker channel is closed here because settlement above
still needs it and no module closes a client it did not open (#18).

## `zarabot.__main__`

Process entry for `python -m zarabot`. No application logic of its own.

**`main() → int`**
Installs `SIGTERM`/`SIGINT` handlers that call `app.shutdown.shutdown`, runs
`app.startup.start` then `app.loops.run`. Returns 0 on a clean shutdown. On
`StartupError` sleeps 30 seconds and returns non-zero (rule 15).

## `sandbox.data`

Laptop research. Never imported by `zarabot/`. Caches to
`<cache_dir>/<ticker>_<interval>.parquet` (`sandbox/cache/` is gitignored).

**`async load(ticker: str, start: datetime, end: datetime, interval: CandleInterval, cache_dir: Path = Path("sandbox/cache")) → list[Candle]`**
Oldest-first. Empty list when the range holds none. Naive `start`/`end` raise
`ValueError`. A cache miss fetches via `broker.client`; a cached range that does
not cover the request is extended by fetching only the missing span.

## `sandbox.exchange`

A simulated broker backed by historical bars. Never imported by `zarabot/`.
Every function matches its `broker.client` counterpart's signature and raises
the same exception for the same condition. Also answers `get_trading_schedule`,
so `market.session` runs on top rather than being stubbed.

**`Commission(pct: Decimal, minimum: Decimal)`** — the broker's tariff.
`on(turnover)` is the fee for one fill.

**`Phase`** — `OPEN` / `LOW` / `HIGH` / `CLOSE`, which price of a bar the
exchange reports as the last price. A phase rather than a price because the
marks are per instrument (#53).

**`SimulatedExchange(bars, instruments, cash, slippage, commission, reject_stops=False)`**
`await advance(moment, phase=Phase.CLOSE)` moves the cursor, sets the reported
phase and fires any stop the newly-entered bar triggers — checked **once per
bar**, so four marks are not four chances to fire. `reject_stops` makes
`post_stop_loss` raise, which is the only way a backtest reaches rule 23's
degrade path where a position stays `LOCAL`. `hold(figi, lots, average_price)` seeds a holding;
`touched(figi, level, trigger)` reports whether the current bar reached a level.

A buy whose turnover plus fee exceeds simulated cash raises `OrderRejected`,
leaving cash and holdings untouched, as the broker would (#47).

Fill model: a market order is priced at the **next** bar's open and returned on
the submitting call; stops read the bar **low** and take-profits the **high**; a
bar gapping through a stop fills at its open; a bar touching both books the
stop; an order on the last bar never fills (#12).

## `sandbox.backtest`

Runs `app.loops.trading_cycle` itself against a `SimulatedExchange` and a
temporary database. Nothing is reimplemented, because nothing needs to be —
live and backtest are the same code.

**`async run(bars: dict[str, list[Candle]], instruments: dict[str, Instrument], config: Config, strategies: Sequence[Strategy], commission: Commission, slippage: Decimal, reject_stops: bool = False) → BacktestResult`**
Replays every bar oldest-first across the whole watchlist with concurrent
positions on one shared cash balance, running **four cycles per bar** at the
open, low, high and close — the last inside the closing window. One cycle a bar
made the daily loss limit and `MAX_AGE` structurally unreachable (#53). Equity is marked to market on every bar,
so `max_drawdown` means something. The seam table it patches is explicit and covers four kinds of escape — broker,
clock, configuration and alerts. Guarded by tests that fail if a real broker
call, alert, clock read or environment config load escapes, plus a structural
test comparing the table against every module that imports `alert` (#49).

## `sandbox.train`

Walk-forward only. Imports `strategies.ml_model.build_features`; does not rebuild
features. `export` writes a joblib bundle loadable by `strategies.ml_model.load`.

**`fit(candles_by_ticker: dict[str, list[Candle]], horizon_days: int, folds: int, seed: int) → FittedModel`**
Buy/no-buy classifier. Label is whether take-profit is reached before stop
within `horizon_days`. `seed` is required and stored. Returns per-fold
validation scores.

**`export(model: FittedModel, path: Path) → Path`**
Writes `{"model", "features", "seed", "trained_at"}` with `features` equal to
`FEATURE_NAMES` in order.
