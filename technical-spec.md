# Zarabot — Technical Specification

**Version:** 1.6
**Date:** 2026-08-18
**Implements:** `business-brief.md` v1.2

**Companion document.** Read the brief first. When this spec and the brief
conflict, **the brief takes precedence**.

**Purpose.** This document defines what every module must do — its contracts,
return types, failure modes, and ordering constraints. It does not define how
any of it is implemented. Two developers implementing from this document
independently must produce compatible code.

**Versioning.** A new version is issued when any module contract, schema, error
rule, or test contract changes.

---

## Global conventions

These apply to every module and are not repeated in individual contracts.

**Package.** All server code lives under `zarabot/`. Research code lives under
`sandbox/` and is never imported by server code.

**Concurrency.** The application is a single `asyncio` event loop. Every
function that performs I/O is a coroutine. No module starts threads. No module
performs blocking I/O on the event loop.

**Time.** Every `datetime` crossing a module boundary is **timezone-aware**.
Naive datetimes are a contract violation and any function receiving one raises
`ValueError`. Internal representation and storage are **UTC**. Moscow time is a
presentation and calendar concern only: trading-day arithmetic, session
boundaries, and anything shown to the owner use `Europe/Moscow`. The `clock`
module is the **single owner of "now"**; no other module calls
`datetime.now()` or `datetime.utcnow()`, so that time can be controlled in tests
and in backtests.

**Money.** All monetary values are `Decimal`, never `float`. Prices, quantities
and P&L are `Decimal` end to end. Floats appear only inside strategy
computation over price series, never in an order, a balance, or a stored value.

**Mutation ownership.** Each piece of mutable state has exactly one owner
module, and no other module writes it:

| State | Sole owner |
|---|---|
| Position rows | `db.positions` |
| Halt flag and halt reason | `state.halt` |
| Cooldown timestamps | `db.cooldowns` |
| Order rows and order status transitions | `db.orders` |
| "Now" | `clock` |

**Purity boundary.** `strategies.*`, `risk.gate`, `risk.sizing` and
`lifecycle.exits` are **pure**: they perform no I/O, touch no database, call no
broker, and read no clock except a `now` passed as an argument. This is what
allows the backtester to reuse them unchanged. A pure module gaining an I/O
dependency is a contract violation.

**Network boundary.** `broker.client` is the only module that makes network
calls to the broker. `telegram.notifier` and `telegram.commands` are the only
modules that make network calls to Telegram. All tests mock at these boundaries.

**Secrets.** No module logs, returns, or includes in an exception message the
value of `TINVEST_TOKEN` or `TELEGRAM_BOT_TOKEN`. See error rule 19.

**Language.** All owner-facing text — commands, alerts, reports, log messages —
is written in English inline. There is no localisation layer, no message
catalogue and no translation function, because there is one user and one
language. Text originating from the broker (instrument names, rejection reasons)
arrives in Russian and is **passed through verbatim**, never translated and never
parsed for meaning: broker reason strings are stored and displayed as opaque
values, so a wording change on their side can never alter behaviour on ours.

---

## 1. One-time manual setup

These steps cannot be automated and must be completed before deployment.
Referenced from **§10 Deployment**.

1. **Rent a VPS** — minimum 2 vCPU, 2 GB RAM, 20 GB disk, Ubuntu LTS. Confirm
   outbound HTTPS to the broker API and to Telegram is not blocked.
2. **Harden the server** — SSH key authentication only, password login disabled,
   inbound firewall permitting SSH only, unattended security upgrades enabled.
3. **Enable and verify time synchronisation** — `timedatectl` must report the
   system clock as synchronised. Session boundaries and candle alignment depend
   on it.
4. **Install Docker Engine and the Compose plugin.**
5. **Open a separate brokerage account** in the T-Invest application, distinct
   from the owner's main savings account, and fund it with the allocated capital
   and no more. This is the primary control bounding the blast radius.
6. **Issue a full-access T-Invest API token** and record it. A read-only token
   cannot place orders. Record the account identifier of the account from step 5.
7. **Create the Telegram bot** through BotFather and record its token.
8. **Obtain the owner's Telegram chat identifier** by messaging the bot once and
   reading the update. This value goes in `TELEGRAM_CHAT_ID`.
9. **Create the data directory** on the host — `/opt/zarabot/data` and
   `/opt/zarabot/data/backups` — owned by the user the container runs as.
10. **Write the environment file** `/opt/zarabot/.env` from `.env.example`, with
    file mode `600`. It contains both tokens and is never committed.

---

## 2. Pre-development verification

Each script below lives in `scripts/verify/`, is run manually before any
application code is written, prints exactly one summary line beginning `PASS` or
`FAIL`, and **exits 0 on pass and 1 on failure**. Partial success is failure: a
script that verifies three things and confirms two exits 1.

The suite is run by `scripts/verify/run_all.sh`, which executes every script in
order, stops at the first non-zero exit, and itself exits non-zero.

**V1 — `verify_token.py`.** Authenticates with `TINVEST_TOKEN` and retrieves the
account list. PASS requires: authentication succeeds, and `TINVEST_ACCOUNT_ID`
appears in the returned accounts. FAIL if the token is rejected, or the account
is absent.

**V2 — `verify_trading_rights.py`.** Confirms the token can trade, by placing
and immediately cancelling one limit order far from the market price on the
**sandbox** account. PASS requires: order accepted, order cancelled, final state
confirmed as cancelled. FAIL on any rejection. This script must never run
against the live account.

**V3 — `verify_instruments.py`.** For every ticker in `WATCHLIST`, retrieves
instrument metadata. PASS requires, for every ticker without exception: a FIGI
resolves, `lot` size is a positive integer, minimum price increment is present,
the instrument is available for trading via API, and its currency is RUB. Prints
one line per ticker plus the summary line. FAIL if any single ticker fails any
check.

**V4 — `verify_candles.py`.** Requests historical candles for every watchlist
ticker at the interval the strategies require. PASS requires: at least 250
trading days of history available for every ticker, no gaps longer than three
consecutive trading days, and every candle carrying a timezone-aware timestamp.
FAIL otherwise. The 250-day requirement exists because the longest strategy
lookback plus a backtest window cannot be satisfied by less.

**V5 — `verify_calendar.py`.** Requests the exchange trading schedule for the
next 14 days. PASS requires: the response distinguishes trading from
non-trading days, and provides session start and end times that convert to
`Europe/Moscow` without ambiguity. FAIL if the schedule must be inferred or
hardcoded — the design depends on querying it.

**V6 — `verify_order_state.py`.** On the sandbox account, submits an order with
a client-generated idempotency key, then in a **separate process** retrieves it
using `GetOrderState` with `orderIdType = ORDER_ID_TYPE_REQUEST`. PASS requires:
the order is retrievable by the client key alone; its terminal state is
unambiguous; and re-submitting `PostOrder` with the same key returns the existing
order rather than creating a second one. FAIL on any of the three.

This remains the most important verification in the suite, but its status has
changed: the behaviour is now **documented** by the broker rather than assumed by
this spec, so V6 confirms that documentation matches reality on a live account
instead of discovering whether the design is viable at all. Both mechanisms are
checked because the crash-recovery path uses the first and falls back to the
second.

**V7 — `verify_rate_limits.py`.** Issues requests to the candle endpoint at a
measured, increasing rate until the broker signals a limit. PASS requires: the
observed limit is recorded, the error returned on breach is identifiable
programmatically, and the limit is at least twice the rate implied by
`POLL_INTERVAL_SECONDS` across the full watchlist. Prints the measured limit.
FAIL if a limit cannot be triggered and identified, or if headroom is under 2×.

**V8 — `verify_telegram.py`.** Sends a message to `TELEGRAM_CHAT_ID`, then reads
back one update. PASS requires: message delivered, an incoming command from the
authorised chat is received and parsed, and message length limits are confirmed
by sending a maximum-length message. FAIL otherwise.

**V9 — `verify_environment.py`.** Runs on the VPS. PASS requires: Docker and
Compose present, system clock synchronised and within two seconds of a public
NTP source, `/opt/zarabot/data` writable, and outbound connectivity to both the
broker API and Telegram. FAIL otherwise.

**V11 — `verify_stop_orders.py`.** On the sandbox account: buys one lot, places
a good-till-cancel stop-loss against it, confirms the exchange reports the stop
as standing, cancels it cleanly, and flattens. PASS requires all five. FAIL
otherwise. This verifies the mechanism the position-protection design depends on
— that the exchange will hold a stop indefinitely without the bot present.

**V10 — `verify_sdk_index.py`.** Runs on the machine that builds the image, and
needs no token. PASS requires: the T-Bank index is reachable; the resolved wheel
matches the recorded sha256; `from t_tech.invest import AsyncClient` succeeds;
`OrderIdType.ORDER_ID_TYPE_REQUEST` exists; and `post_order` and
`get_order_state` carry the `order_id`, `order_id_type` and
`confirm_margin_trade` parameters this spec depends on. FAIL otherwise.

Each of these was confirmed against wheel 1.49.1 on 2026-08-18, so V10 is a
regression check against a future SDK version silently removing or renaming
something load-bearing — not a discovery step.

**Pinning.** After V1–V11 pass, exact resolved versions of every dependency are
written to the lockfile and **§9 Dependencies** is updated with the pinned
versions, including the SDK version V10 recorded. Development starts from the
lockfile, not from the floors.

---

## 3. Unit tests

### 3.1 Test infrastructure

**Framework.** `pytest` with `pytest-asyncio`. `asyncio_mode = "auto"` is set in
`pyproject.toml`, so coroutine tests need no per-test decorator and no
collection warnings are emitted.

**No network.** Unit tests make no real network calls. `broker.client`,
`telegram.notifier` and `telegram.commands` are mocked at the module boundary in
every test that is not testing those modules themselves. A test that opens a
socket is a failing test.

**Database isolation.** A `db` fixture creates a temporary SQLite file, applies
every migration in order, yields a connection, and deletes the file on teardown.
Each test gets a fresh database; no test observes another's rows. Repository
tests that assert transactional behaviour wrap the body in a transaction and
roll it back. An in-memory database is not used, because migration application
and multi-connection behaviour must be exercised as they run in production.

**Time control.** The `clock` module is the only source of "now", and tests
inject a fixed or scripted clock. No test calls `sleep` to advance time.

**Determinism.** Strategy, risk, sizing, exit and P&L tests are pure-function
tests with fixed inputs. Any test whose outcome depends on wall-clock time,
network state, or file ordering is a defect.

**Coverage.** 80% line coverage overall, enforced in CI-equivalent local runs.
70% is accepted for `app.loops`, `app.startup` and `app.shutdown`, which are
orchestration entry points whose branches are covered indirectly. `risk.gate`,
`risk.sizing`, `lifecycle.exits` and `execution.orders` require **95%** — these
four decide whether real money moves, and a missed branch in them is a financial
defect, not a coverage statistic.

**Fixtures provided.** A candle-series builder producing deterministic price
paths (rising, falling, flat, gapping, and volatile); an instrument fixture with
a non-trivial lot size; a portfolio-state fixture; and a broker double that can
be scripted to succeed, reject, time out, or return a stale state.

### 3.2 Test contracts

Each case states an input, an expected output or side effect, and the invariant
it proves.

**`clock`**
- `now()` returns a timezone-aware UTC datetime (proves the awareness invariant).
- `to_moscow()` on a UTC instant during a DST-shifted month returns the correct
  Moscow wall time (proves conversion is not a fixed offset).
- `trading_days_between()` across a weekend returns the count excluding Saturday
  and Sunday (proves calendar arithmetic ignores non-trading days).
- Any function given a naive datetime raises `ValueError` (proves the contract is
  enforced, not merely documented).

**`config`**
- A complete environment produces a populated config object (happy path).
- A missing `TINVEST_TOKEN` raises `ConfigError` naming that variable (proves
  fail-fast and that the message identifies the offender).
- `POSITION_SIZE_PCT` of 0 or above 100 raises `ConfigError` (boundary).
- `POSITION_SIZE_PCT` above `MAX_POSITION_PCT` raises `ConfigError` (proves
  cross-field validation, not just per-field).
- `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeding 100 raises `ConfigError`
  (proves the allocation cannot be structurally over-committed).
- `TAKE_PROFIT_PCT` less than or equal to `STOP_LOSS_PCT` raises `ConfigError`
  (proves a configuration that can never profit is rejected).
- An empty `WATCHLIST` raises `ConfigError` (proves the bot cannot start with
  nothing to trade).
- The string form of the config object contains neither token (proves accidental
  logging of the whole config leaks nothing).

**`logging_setup`**
- A log record whose message contains the token value emits the token replaced by
  a fixed mask (proves redaction on the message).
- A log record carrying the token in a structured field is redacted (proves
  redaction is not message-only).
- An exception whose string representation contains the token is redacted when
  logged with a traceback (proves redaction survives exception formatting).
- A record containing no secret passes through byte-identical (proves redaction
  does not corrupt ordinary logs).

**`db.migrations`**
- Applying migrations to an empty database creates every table at the current
  version (happy path).
- Applying migrations twice makes no changes the second time and does not raise
  (proves idempotency).
- A database at version N−1 is migrated to N without data loss in existing rows
  (proves forward migration preserves history).
- A database whose recorded version is **higher** than the code's raises
  `MigrationError` and does not modify anything (proves a rolled-back deployment
  cannot silently corrupt a newer schema).

**`db.positions`**
- Opening a position then reading open positions returns it (happy path).
- Closing a position removes it from open positions and preserves it in history
  with its exit trigger (proves closure is a state transition, not a delete).
- Closing an already-closed position raises `PositionStateError` (proves double
  exit cannot be recorded).
- Reading open positions on an empty database returns an empty list, not `None`
  (proves the nullable contract).
- A position round-tripped through the database returns `Decimal` prices equal to
  those written (proves no float conversion in storage).
- Concurrent close attempts on the same position result in exactly one success
  and one `PositionStateError` (proves the state transition is atomic).

**`db.orders`**
- An order recorded as `SUBMITTING` then confirmed as `FILLED` reports the
  terminal state (happy path).
- Orders left in `SUBMITTING` are returned by the unresolved-orders query
  (proves crash recovery can find them).
- Recording two orders with the same idempotency key raises `DuplicateOrderError`
  (proves the uniqueness invariant is enforced at the storage layer).
- A terminal order cannot transition back to a non-terminal state (proves the
  status machine is one-way).

**`db.cooldowns`**
- A ticker with no recorded cooldown is not in cooldown (happy path).
- A ticker whose cooldown started `COOLDOWN − 1 minute` ago is in cooldown
  (boundary, inside).
- A ticker whose cooldown started exactly `COOLDOWN` ago is **not** in cooldown
  (boundary, proves the interval is exclusive at the end).
- Starting a cooldown for a ticker already in cooldown extends it from the newer
  timestamp (proves the semantics of a re-entry after a rapid second close).

**`db.signals` / `db.snapshots`**
- A rejected signal is stored with its rejection reason and is retrievable by day
  (proves rejections are analysable, as the brief requires).
- A daily snapshot written twice for the same date updates rather than duplicates
  (proves the date is the key).

**`broker.client`**
- Each method returns the documented domain type given a scripted broker
  response (happy path per method).
- A transport error raises `BrokerUnavailable` (proves transport failures are
  typed, not leaked as SDK exceptions).
- A rate-limit response raises `BrokerRateLimited` carrying the retry hint
  (proves the caller can back off correctly).
- An order rejection raises `OrderRejected` carrying the broker's reason string
  (proves the reason reaches the owner).
- No exception raised by this module contains the token in its message (proves
  the secret boundary).
- Prices returned are `Decimal` (proves no float leaks in from the SDK).
- Every order submission is asserted to pass `confirm_margin_trade=False`
  (proves the no-leverage guarantee is enforced at the only place it can be
  broken — this test is the executable form of the brief's loss bound).
- `get_order_state` is asserted to pass `ORDER_ID_TYPE_REQUEST` (proves recovery
  looks orders up by the key it still has, not the identifier it lost).

**`broker.reconcile`**
- Broker and database agreeing produces no adjustments and no alert (happy path).
- A position open in the database but absent at the broker is closed locally as
  externally closed and alerted (proves the broker is authoritative).
- A position present at the broker but absent locally is adopted with the
  broker's average price as entry price, marked adopted, and alerted (proves
  unknown holdings are managed rather than ignored).
- A lot-count mismatch adopts the broker's count and alerts (proves quantity
  reconciliation).
- Reconciliation is idempotent: running it twice against an unchanged broker
  produces adjustments once (proves it does not thrash).

**`market.session`**
- A timestamp inside the main session reports open (happy path).
- Exactly at the session open instant reports open; exactly at the close instant
  reports closed (boundary, proves inclusivity at both ends).
- A Saturday, and a scheduled market holiday, report closed (proves the calendar
  is consulted, not the weekday).
- With the schedule unavailable, reports closed and raises no exception (proves
  the safe default is to not trade).

**`market.data`**
- Candles for a watchlist ticker are returned newest-last, timezone-aware
  (happy path and ordering contract).
- Fewer candles available than the longest strategy lookback returns what exists
  and the caller can detect insufficiency (proves partial history is visible, not
  silently padded).
- A ticker failing while others succeed does not fail the batch (proves one bad
  instrument cannot blind the bot to the rest).

**`strategies.*`**

For every strategy, independently:
- A price series designed to produce an entry returns a `Signal` naming the
  strategy and the ticker (happy path).
- A price series with no setup returns `None` (proves the nullable contract).
- A series shorter than the strategy's lookback returns `None` and does not raise
  (proves insufficient history is a non-event, not a crash).
- A series containing a flat price run returns `None` rather than dividing by
  zero (proves the degenerate-input path).
- The same input evaluated twice returns equal results (proves purity and
  determinism).
- No strategy returns a `SELL` signal under any input (proves the entry-only
  contract that the whole exit design rests on).

Additionally, `strategies.ml_model`:
- With `ML_MODEL_PATH` unset, the strategy is absent from the registry (proves
  disabled-by-default).
- A missing or unreadable model file raises `ModelLoadError` at startup, not at
  first signal (proves failure is loud and early).
- A model whose feature contract does not match the expected names and order
  raises `ModelContractError` (proves a stale model cannot silently mispredict).
- A prediction below the confidence threshold returns `None` (boundary).

**`risk.sizing`**
- A standard case returns whole lots at or below the configured percentage
  (happy path).
- A price so high that one lot exceeds the position cap returns zero lots
  (proves the expensive-instrument path, which would otherwise over-allocate).
- Available cash below the cost of one lot returns zero lots (proves cash is
  respected independently of the percentage).
- Rounding is always downward: a budget worth 2.9 lots returns 2 (proves the
  intended direction of error).
- The returned lot count multiplied by lot size and price never exceeds
  `MAX_POSITION_PCT` of allocated capital for any input (proves the ceiling is
  structural).

**`risk.gate`**
- A clean signal in an unremarkable portfolio is approved (happy path).
- Each rejection reason is produced by a state constructed to trigger exactly it:
  halted, session closed, position cap, maximum positions, cooldown active,
  insufficient cash, zero lots, instrument not trading, and an existing position
  in the same ticker (proves every branch, one test each).
- With several violations present at once, the rejection reason is the
  highest-priority one, deterministically (proves rejection reporting is stable
  and not order-dependent).
- Exactly at `MAX_OPEN_POSITIONS` a new entry is rejected; at one below it is
  approved (boundary, proves inclusivity).
- The gate never returns approval for a `SELL` (proves exits never route through
  the gate).
- The gate performs no I/O — verified by calling it with every collaborator
  absent (proves purity).

**`lifecycle.exits`**
- Price exactly at the stop level triggers `STOP_LOSS`; one increment above does
  not (boundary).
- Price exactly at the target triggers `TAKE_PROFIT`; one increment below does
  not (boundary).
- A position whose age reaches the maximum during the final fifteen minutes
  triggers `MAX_AGE`; the same position earlier in that session does not
  (boundary, proves the timing rule).
- A position at both stop and maximum age returns `STOP_LOSS` (proves the
  documented precedence, so the recorded reason is deterministic).
- Age is counted in trading days: a position opened Friday is not aged by the
  weekend (proves calendar-aware ageing).
- An adopted position ages from its adoption timestamp (proves the reconciliation
  interaction).
- Evaluation is pure and repeatable for identical inputs.

**`execution.orders`**
- A successful entry records the order before the broker call and marks it filled
  after confirmation (proves the write-then-send ordering that makes crashes
  survivable).
- A crash simulated between the database write and the broker call leaves an
  unresolved order that recovery resolves by querying the broker with the
  idempotency key (proves no double submission).
- A broker timeout followed by a successful state query showing a fill records
  the fill and opens the position (proves an uncertain outcome is resolved by
  asking, not assuming).
- A rejected entry records the rejection and opens no position, and is not
  retried (proves entry rejections are terminal).
- A rejected **exit** is retried on the following cycle and alerts immediately
  (proves the documented exception).
- Two concurrent entry attempts for the same ticker result in one order (proves
  the per-ticker lock).
- The order lock is released when the broker call raises (proves the release
  guarantee under failure, not only on success).

**stop-order lifecycle** (`execution.orders`, `broker.reconcile`)
- Opening a position places exactly one stop order at the computed price
  (happy path).
- A position is `LOCAL` between its creation and the stop being confirmed, and
  `EXCHANGE` only after (proves there is no window in which neither owner is
  watching — the gap this two-step design exists to close).
- A stop order rejected three times leaves the position `LOCAL` and open, and
  alerts (proves the degrade path, not an unwind).
- `set_stop_protection(EXCHANGE, None)` raises, as does `(LOCAL, key)` (proves
  the pairing invariant that keeps ownership unambiguous).
- A `LOCAL` position returns `STOP_LOSS` from `lifecycle.exits`; an `EXCHANGE`
  position never does (proves the trigger has exactly one owner — the test that
  prevents selling a position twice).
- A take-profit exit cancels the stop order **before** submitting the sell
  (proves the binding order).
- A cancel that races an already-executed stop is not an error (proves
  idempotency against the exchange).
- Reconciliation finding an open position with no live stop places one; finding a
  stop with no position cancels it; finding a stop at the wrong price replaces it
  (proves all three adjustment paths).
- Restarting with a live stop adopts it rather than placing a second (proves the
  duplicate-protection path, verified by asserting no new stop order is created).
- A stop reported `EXECUTED` closes the position with `exit_trigger = STOP_LOSS`
  and starts the cooldown (proves the exchange-initiated close path).

**partial fills**
- An entry filling 2 of 3 lots opens a position of 2 with stop and target from
  the achieved price, and places a stop for 2 (proves sizing follows the fill).
- The unfilled remainder produces no follow-up order (proves it is abandoned).
- An exit filling partially retries the remainder until flat (proves the system
  never rests half-exited).

**`state.halt`**
- Halting then reading state reports halted with its reason (happy path).
- Halt state survives a simulated restart (proves persistence — a crash must
  never resume trading).
- Resuming clears the halt and records who cleared it (proves auditability).
- Resuming when not halted is accepted and changes nothing (proves idempotency).
- A halt does not prevent `lifecycle.exits` from returning triggers, nor
  `execution.orders` from placing an exit (proves the halt-blocks-entries-only
  contract, which is the single most consequential interaction in the system).

**`pnl`**
- Realised P&L for a closed position matches the arithmetic including commission
  (happy path).
- Unrealised P&L for an open position uses the current price (happy path).
- The daily loss percentage is computed against the day's opening baseline, not
  against allocated capital drift (proves the baseline definition).
- With no positions and no trades, all figures are zero rather than `None`
  (proves the empty-portfolio path).
- The buy-and-hold benchmark over a window with a missing price for one
  instrument reports the benchmark as unavailable rather than as zero (proves
  missing data is not silently treated as no return).

**`telegram.commands`**
- Each command from the authorised chat returns its documented content (happy
  path per command).
- Any command from an unauthorised chat identifier returns nothing, is logged,
  and performs no state change (proves the single security boundary).
- `/resume` when not halted replies that nothing was halted (proves the
  no-op path).
- A response exceeding the message limit is truncated with an explicit note
  naming how many entries were omitted (proves the truncation contract).
- No command mutates a risk limit (proves the brief's prohibition).

**`telegram.notifier`**
- A failed send is retried and, if still failing, written to the log without
  raising (proves Telegram outages never reach trading logic).
- No alert body contains either token (proves the secret boundary at the last
  point of egress).

**`reporter.weekly`**
- A week with trades produces a report containing every documented section
  (happy path).
- A week with no trades produces a valid report stating so (proves the empty
  path, which is otherwise a division-by-zero waiting to happen).
- Win rate with zero closed trades is reported as not applicable, not as zero
  (proves the undefined-metric path).
- A report exceeding the message limit drops the least important section and
  notes the omission (proves the documented trimming order).

**`app.startup`**
- Startup with valid config, a reachable broker and a clean database completes
  and reports ready (happy path).
- Invalid config aborts before any broker call is made (proves fail-fast
  ordering).
- An unresolved order from a previous run is resolved before the first strategy
  evaluation (proves recovery precedes trading — the ordering that prevents a
  duplicate order).
- Reconciliation runs before the first entry is permitted (proves the same for
  position truth).
- A halted-at-shutdown bot starts halted (proves halt persistence end to end).

**`app.loops` / `app.shutdown`**
- With the session closed, no market data call is made (proves the session guard
  gates the loop).
- A shutdown signal during an in-flight order submission waits for a known state
  before exiting (proves the graceful-shutdown contract).
- Shutdown neither cancels nor liquidates positions (proves restarts have no
  financial consequence).

**`ops.backup`**
- A backup produces a file that opens as a valid database containing the same
  rows (proves the copy is consistent, not a torn file).
- Backups older than the retention window are removed and newer ones are kept
  (boundary).
- A failing backup alerts and does not stop trading (proves the priority order).

**`sandbox.backtest`**
- A backtest over a known price series produces the hand-computed trade sequence
  (proves the engine is correct against a worked example).
- The backtester and the live path produce identical decisions for identical
  inputs (proves the shared-code invariant — this is the test that makes
  backtests trustworthy).
- A strategy is never given a candle timestamped after the decision instant
  (proves absence of look-ahead bias).
- Commission and a configured slippage assumption are applied to every simulated
  fill (proves the results are not idealised).

---

## 4. Module contracts

Contracts only. Signatures are binding; implementations are the developer's
choice. `async` marks a coroutine. Every listed exception is part of the
contract and must be raised exactly under the stated conditions.

### `zarabot/models.py`

Domain types shared across every module. Contains validation only, never logic.

**Enumerations**
- `Side` — `BUY`, `SELL`
- `OrderStatus` — `SUBMITTING`, `SUBMITTED`, `FILLED`, `REJECTED`, `CANCELLED`, `UNKNOWN`
- `ExitTrigger` — `STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`, `EXTERNAL`
- `RejectionReason` — `HALTED`, `SESSION_CLOSED`, `INSTRUMENT_NOT_TRADING`, `DUPLICATE_TICKER`, `MAX_POSITIONS`, `COOLDOWN_ACTIVE`, `INSUFFICIENT_CASH`, `ZERO_LOTS`, `POSITION_CAP`, `BROKER_LOT_LIMIT`
- `HaltReason` — `DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`
- `StopOrderStatus` — `PLACING`, `ACTIVE`, `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`
- `StopProtection` — `EXCHANGE`, `LOCAL`. Which side owns a position's stop trigger

`BROKER_LOT_LIMIT` covers the broker refusing the size outright — its maximum
for the account is zero lots. It is distinct from `ZERO_LOTS`, which means our
own sizing arithmetic produced nothing affordable; the two have different causes
and only separate reasons make the rejection log diagnostic.

**Frozen dataclasses** — `Candle`, `Instrument`, `Signal`, `Position`,
`OrderRecord`, `StopOrderRecord`, `OperationRecord`, `PortfolioState`,
`SessionInfo`, `RiskDecision`, `HaltState`, `ReconciliationReport`,
`TradingCalendar`, `BacktestResult`.

`OperationRecord` carries the broker's actual commission. `TradingCalendar` is
the queried schedule that `clock.trading_days_between` and `market.session` read.
`AppContext` (the assembled dependencies) and `LoadedModel` (an ML model plus its
feature manifest) are **not** domain types — they live with `app.startup` and
`strategies.ml_model` respectively, and no other module constructs them.

- Every monetary field is `Decimal`; every timestamp field is timezone-aware.
- Construction with a naive datetime raises `ValueError`.
- Construction with a negative lot count, negative price, or non-positive lot
  size raises `ValueError`.
- `RiskDecision` is either approved with a positive lot count, or rejected with a
  `RejectionReason`. It can never be both, and never neither.
- Must never contain a method that performs I/O.

### `zarabot/clock.py`

Single owner of the current time and of trading-day arithmetic.

**`now() → datetime`**
- Returns the current instant, timezone-aware, in UTC.
- The only place in the codebase permitted to read the system clock.
- In tests and backtests this module is substituted; no other module may be.

**`to_moscow(moment: datetime) → datetime`**
- Converts a timezone-aware instant to `Europe/Moscow`.
- Raises `ValueError` on a naive input.
- Must never assume a fixed offset.

**`moscow_date(moment: datetime) → date`**
- The Moscow calendar date of an instant. Used as the key for daily counters and
  snapshots, so that a day means a Moscow trading day.

**`trading_days_between(start: datetime, end: datetime, calendar: TradingCalendar) → int`**
- Number of exchange trading days elapsed, excluding weekends and holidays.
- Returns 0 when both instants fall on the same trading day.
- Raises `ValueError` on a naive input or when `end` precedes `start`.

### `zarabot/config.py`

Loads and validates every setting once at startup.

**`load() → Config`**
- Reads all variables in the brief's environment-variable table (§18), applies
  defaults, coerces types, and validates.
- Returns a frozen `Config`.
- Raises `ConfigError` naming the offending variable when: a required variable is
  missing or empty; a numeric value is out of range; `POSITION_SIZE_PCT` exceeds
  `MAX_POSITION_PCT`; `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeds 100;
  `TAKE_PROFIT_PCT` is not greater than `STOP_LOSS_PCT`; `WATCHLIST` is empty;
  or `ML_MODEL_PATH` is set but unreadable.
- Must never substitute a default for a missing **risk** variable.
- Must never include a token value in an exception message or in `__repr__`.
- Called before any other module is initialised.

### `zarabot/logging_setup.py`

Configures structured logging and enforces secret redaction.

**`configure(level: str, secrets: list[str]) → None`**
- Installs a JSON formatter writing to stdout and a filter that replaces every
  occurrence of every value in `secrets` with a fixed mask.
- Redaction applies to the message, to structured fields, and to formatted
  exception text, recursing into nested dictionaries and sequences to a depth of
  10; deeper structures are replaced wholesale rather than passed through
  unredacted.
- Must never write to a file, and never to stderr.
- Called by `app.startup` immediately after `config.load()` and before any other
  module logs anything.

### `zarabot/db/migrations.py`

Owns schema creation and version tracking.

**`async apply(conn: Connection) → int`**
- Applies every migration whose version exceeds the database's recorded version,
  in ascending order, each in its own transaction.
- Returns the resulting schema version.
- Raises `MigrationError` if the recorded version exceeds the highest known
  migration, and makes no modification in that case.
- Idempotent: applying twice is a no-op the second time.
- Called by `app.startup` before any repository function.

### `zarabot/db/positions.py`

**Sole owner of position row mutation.** No other module writes these rows.

**`async open(signal: Signal, order: OrderRecord, instrument: Instrument, stop: Decimal, target: Decimal, opened_at: datetime) → Position`**
- Inserts an open position and returns it with its assigned identifier.
- Inserts with `stop_protection = LOCAL` **always**. The position row is created
  before the standing stop order exists, and for that window the bot itself is
  the only thing watching the stop. Defaulting to `LOCAL` means the position is
  never recorded as protected by something that has not been confirmed to exist;
  the failure direction is a redundant local check, not an unwatched position.
- Raises `PositionStateError` if an open position already exists for the ticker.

**`async set_stop_protection(position_id: int, protection: StopProtection, stop_order_key: str | None) → Position`**
- Promotes a position to `EXCHANGE` once its standing stop is confirmed active,
  or returns it to `LOCAL` when that stop is cancelled, executed, or found
  missing.
- Raises `PositionStateError` when `EXCHANGE` is requested without a key, or
  `LOCAL` with one — the pairing is the invariant that prevents both owners
  acting on the same position.
- Called only by `execution.orders` and by the startup remediation step.

**`async close(position_id: int, trigger: ExitTrigger, exit_price: Decimal, closed_at: datetime, order: OrderRecord) → Position`**
- Transitions a position to closed, recording the trigger, exit price, realised
  P&L and the closing order.
- Raises `PositionStateError` if the position is already closed or absent.
- The transition is atomic: concurrent calls produce exactly one success.
- Must never delete a row — history is permanent.

**`async list_open() → list[Position]`**
- Returns all open positions, empty list when none. Never returns `None`.

**`async get(position_id: int) → Position | None`**
- Returns `None` when absent rather than raising.

**`async adopt(instrument: Instrument, lots: int, average_price: Decimal, adopted_at: datetime) → Position`**
- Creates an open position for a holding discovered at the broker but unknown
  locally, with `adopted = True`, stop and target derived from `average_price`,
  and age counted from `adopted_at`.
- Called only by `broker.reconcile`.

### `zarabot/db/orders.py`

**Sole owner of order rows and of order status transitions.**

**`async record_submitting(key: str, ticker: str, side: Side, lots: int, intent: str) → OrderRecord`**
- Persists the intent to place an order **before** it is sent.
- Raises `DuplicateOrderError` if the idempotency key already exists.
- Ordering constraint: must complete before `broker.client.post_order` is called
  with the same key. This ordering is what makes a crash mid-submission
  recoverable, and reversing it is a critical defect.

**`async settle(key: str, status: OrderStatus, filled_lots: int, filled_price: Decimal | None, broker_reason: str | None) → OrderRecord`**
- Records a terminal outcome.
- Raises `OrderStateError` on a transition out of a terminal status.

**`async list_unresolved() → list[OrderRecord]`**
- Returns orders left in `SUBMITTING` or `SUBMITTED`, oldest first.
- Consumed by `app.startup` before trading begins.

### `zarabot/db/stop_orders.py`

**Sole owner of stop-order rows.** No other module writes them.

**`async record_placing(key: str, position_id: int, ticker: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
- Persists the intent before the broker is called, exactly as `db.orders` does
  for ordinary orders, and for the same reason: a crash between the write and
  the call must leave something recoverable.
- Raises `DuplicateOrderError` on a repeated key.

**`async activate(key: str, stop_order_id: str) → StopOrderRecord`** — records the broker's identifier once the stop is standing.

**`async settle(key: str, status: StopOrderStatus, settled_at: datetime) → StopOrderRecord`**
- Terminal statuses are `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`.
- Raises `OrderStateError` on a transition out of a terminal status.

**`async active_for_position(position_id: int) → StopOrderRecord | None`**
- Returns the standing stop for a position, or `None`. Never returns a list:
  more than one active stop per position is an invariant violation, not a case
  the caller must handle.

**`async list_active() → list[StopOrderRecord]`** — every stop believed standing, for reconciliation.

### `zarabot/db/cooldowns.py`

**Sole owner of cooldown timestamps.**

**`async start(ticker: str, at: datetime) → None`** — records or overwrites with the newer instant.

**`async is_active(ticker: str, now: datetime, minutes: int) → bool`**
- True while `now - started_at < minutes`. Exactly at the boundary returns False.

**`async active_until(ticker: str, minutes: int) → datetime | None`** — for display in command replies.

### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`

**`async record(signal: Signal, decision: RiskDecision) → None`** — stores every signal, approved or rejected, with its reason.

**`async list_for_period(start: date, end: date) → list[...]`** — for the weekly report.

**`async write_daily(snapshot) → None`** — upserts on the Moscow date; a second write for the same date updates rather than duplicates.

### `zarabot/broker/client.py`

The **only** module that calls the broker. Wraps `t_tech.invest.AsyncClient` and
returns domain types, never SDK types. The SDK's own services — `OrdersService`,
`MarketDataService`, `InstrumentsService`, `OperationsService`, `SandboxService`
— are reachable only from inside this module.

**Sandbox is selected by endpoint, never by a different method family.** The SDK
exposes a `SandboxService` with a parallel set of methods — `post_sandbox_order`,
`get_sandbox_order_state`, `cancel_sandbox_order` and so on. This module must
**not** use them for trading operations. `TRADING_MODE=sandbox` instead
constructs the client against `INVEST_GRPC_API_SANDBOX` and calls the ordinary
`orders.post_order` / `orders.get_order_state`, so sandbox and live exercise the
same code path and a sandbox result is evidence about live behaviour. Using the
`post_sandbox_*` family would mean testing code that never runs in production —
worse than not testing, because it produces false confidence. The
`SandboxService` methods are permitted only for account housekeeping with no live
equivalent (opening and funding a sandbox account), and only from verification
scripts, never from `zarabot/`.

**Margin trading is prohibited at the call site.** The SDK's `post_order` accepts
`confirm_margin_trade: bool = False`. This module must never pass `True`, under
any condition, for any order. The brief's no-leverage guarantee — which is what
bounds the maximum loss to the allocated capital, and therefore what the entire
security model rests on — is enforced here, at the one place an order can be
created. A code change setting this flag is a critical defect regardless of what
else it does.

**`async get_instrument(ticker: str) → Instrument`**
- Raises `InstrumentNotFound` when the ticker does not resolve, `BrokerUnavailable`
  on transport failure, `BrokerRateLimited` when throttled.

**`async get_candles(figi: str, interval, since: datetime, until: datetime) → list[Candle]`**
- Returns candles ordered oldest-first with timezone-aware timestamps.
- Returns an empty list when the range contains no trading activity.
- Raises `ValueError` on naive datetimes.

**`async get_last_price(figi: str) → Decimal`**

**`async get_portfolio() → PortfolioState`**
- Returns cash and holdings as reported by the broker. This is the authoritative
  view referred to throughout the brief.

**`async get_trading_schedule(days: int) → list[SessionInfo]`**
- Session open and close instants per day, timezone-aware, marking non-trading
  days.

**`async post_market_order(key: str, figi: str, side: Side, lots: int) → OrderRecord`**
- Submits a market order using `key` as the broker-side idempotency key.
- Raises `OrderRejected` carrying the broker's reason, `BrokerUnavailable`, or
  `BrokerRateLimited`.
- Must never be called before `db.orders.record_submitting` has persisted `key`.

**`async post_stop_loss(key: str, figi: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
- Places a standing stop-loss with the exchange using
  `post_stop_order(stop_order_type=STOP_ORDER_TYPE_STOP_LOSS,
  expiration_type=STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
  exchange_order_type=EXCHANGE_ORDER_TYPE_MARKET)`, carrying its own idempotency
  key and `confirm_margin_trade=False`.
- Raises `StopOrderRejected` carrying the broker's reason, or `BrokerUnavailable`.
- Good-till-cancel is required: a day-expiring stop would silently stop
  protecting the position overnight, which is precisely when it is needed.

**`async cancel_stop_order(stop_order_id: str) → None`**
- Idempotent. An already-cancelled or already-executed stop order is not an
  error, because the executor calls this while racing the exchange.

**`async list_stop_orders() → list[StopOrderRecord]`**
- Every standing stop order on the account. Consumed by reconciliation.

**`async get_max_lots(figi: str) → int`**
- The maximum lots the broker will accept for a buy on this account. A pre-submit
  sanity check against `risk.sizing`, which models cash but not settlement or
  instrument-specific restrictions.

**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
- Executed operations including **actual commission charged**. Commission is read
  from here, never estimated: an estimated commission makes every realised P&L
  figure quietly wrong, and P&L is the number this project exists to produce.

**`async get_order_state(key: str) → OrderRecord`**
- Retrieves an order **by the client idempotency key alone**, so a restarted
  process can determine what happened. Implemented as
  `get_order_state(account_id=…, order_id=<our key>, order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)`.
  The enum member exists in the SDK (`OrderIdType.ORDER_ID_TYPE_REQUEST = 2`) and
  the parameter is present on the async method signature, so this is verified
  against the library, not only against documentation. Passing the exchange
  identifier here is a defect: after a crash the exchange identifier is precisely
  what was lost.
- The key passed to `post_order` as `order_id` and the key passed here are the
  same value — the row's primary key in the `orders` table.
- Raises `OrderNotFound` when the broker has no record, which proves the order
  was never accepted.
- **Documented fallback.** `PostOrder` is itself idempotent on the
  `(orderId, accountId)` pair: re-submitting with a key already used returns the
  status of the existing order rather than creating a second one. If
  `ORDER_ID_TYPE_REQUEST` proves unreliable in practice, recovery may re-call
  `post_market_order` with the original key, which is a safe read. Two
  independent recovery paths exist; the design does not rest on either alone.
- **Key retention caveat.** The broker states idempotency keys are retained for
  one year but explicitly declines to guarantee it, noting the mechanism may
  change. This design needs retention measured in minutes — from crash to
  restart — so the caveat is immaterial here, but it means keys must never be
  treated as a permanent audit identifier. The `orders` table is that record.

All functions in this module: must never log or include the token in any
exception; must convert every SDK exception into one of the typed exceptions
above; must never return a `float`.

### `zarabot/broker/reconcile.py`

**`async reconcile(now: datetime) → ReconciliationReport`**
- Compares `broker.client.get_portfolio()` against `db.positions.list_open()`.
- Locally-open but absent at the broker → closed as `EXTERNAL` at the last known
  price.
- Present at the broker but unknown locally → adopted via `db.positions.adopt`.
- Lot mismatch → the broker's count is written locally.
- **Stop orders are reconciled too, but this module does not act on them.**
  Every open position must have exactly one live stop order. This module
  *reports* each discrepancy — a position with no stop, a stop with no position,
  a stop at the wrong price — and the caller performs the remedy through
  `execution.orders`, which is the only module permitted to place or cancel
  orders. Keeping reconciliation observational is what allows it to run
  anywhere, including read-only diagnostics, without financial side effects.
- On restart an existing stop is **adopted** rather than replaced — two stops on
  one position would sell it twice.
- Returns a report enumerating every adjustment; an empty report means agreement.
- Idempotent.
- Ordering constraint: runs during `app.startup` after migrations and after
  unresolved-order recovery, and before any entry is permitted.
- Must never place or cancel an order, including stop orders. Reconciliation
  observes and records; it does not trade. Every remedy it identifies is carried
  out by `app.startup` through `execution.orders`.

### `zarabot/market/session.py`

**`async refresh(days: int) → None`** — caches the schedule; called at startup and once per trading day.

**`is_open(now: datetime) → bool`**
- True when `now` falls within a main session, inclusive of the open instant and
  exclusive of the close instant.
- Returns `False` when the schedule is unavailable — the safe default is not to
  trade.

**`current_session(now: datetime) → SessionInfo | None`**

**`in_closing_window(now: datetime, minutes: int) → bool`** — true during the final `minutes` of the current session; used only by the maximum-age exit.

**`next_open(now: datetime) → datetime`** — used by the loop to sleep rather than poll.

### `zarabot/market/data.py`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
- Returns per-ticker candle series, oldest-first.
- A ticker that fails is omitted from the result and logged; the batch still
  returns. One unavailable instrument must never blind the bot to the rest.
- Never pads or interpolates missing candles.

### `zarabot/strategies/base.py`

**`Strategy` protocol** — `name: str`, `lookback: int`, and:

**`evaluate(ticker: str, candles: list[Candle], now: datetime) → Signal | None`**
- Pure. No I/O, no clock, no database, no broker.
- Returns a `BUY` signal or `None`. **Must never return a `SELL` signal** —
  strategies enter, the lifecycle exits.
- Returns `None` when fewer than `lookback` candles are supplied.
- Returns `None` rather than raising on degenerate input such as a flat series.
- Deterministic: identical inputs produce identical outputs.

`ma_crossover`, `rsi_reversion`, `momentum` each implement this protocol and
declare their own parameters and lookback. `registry.enabled(config) → list[Strategy]`
builds the active set from `ENABLED_STRATEGIES`, raising `ConfigError` on an
unknown name.

### `zarabot/strategies/ml_model.py`

**`load(path: Path) → LoadedModel`**
- Loads the exported model and its feature manifest.
- Raises `ModelLoadError` when absent or unreadable, and `ModelContractError`
  when the manifest's feature names or order differ from those the code builds.
- Called once at startup, never on the trading path — a model failure must be
  loud and early, never mid-session.

**`evaluate(...) → Signal | None`** — as the protocol, returning `None` below the configured confidence threshold. Absent from the registry entirely when `ML_MODEL_PATH` is unset.

### `zarabot/risk/sizing.py`

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, cap_pct: Decimal) → int`**
- Pure. Returns the number of **whole lots** to buy.
- Rounds down, always. Returns 0 when one lot exceeds the cap or exceeds cash.
- The returned value must satisfy, for every possible input:
  `lots × lot_size × price ≤ cap_pct% × allocated` and `≤ cash`.
- Never returns a negative number.

### `zarabot/risk/gate.py`

**`check(signal: Signal, state: PortfolioState, instrument: Instrument, cooldown_active: bool, session_open: bool, halted: bool, now: datetime, config: Config) → RiskDecision`**
- Pure. Calls `risk.sizing` and returns approval with a lot count, or rejection
  with exactly one reason.
- Rejection reasons are evaluated in this fixed priority order, so that the
  recorded reason is deterministic when several apply:
  `HALTED` → `SESSION_CLOSED` → `INSTRUMENT_NOT_TRADING` → `DUPLICATE_TICKER` →
  `MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` → `ZERO_LOTS` →
  `POSITION_CAP`.
- `MAX_POSITIONS` applies at or above the configured maximum.
- Rejects any signal whose side is `SELL`. Exits never pass through this module.
- Must never perform I/O, and must never mutate `state`.

### `zarabot/lifecycle/exits.py`

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int, config: Config) → ExitTrigger | None`**
- Pure. Returns the trigger that fires, or `None`.
- `STOP_LOSS` when `price ≤ position.stop_price` **and only when
  `position.stop_protection == 'LOCAL'`**. When the exchange holds the stop, this
  module must never return `STOP_LOSS`: the trigger has exactly one owner at a
  time, and both acting on the same position would sell it twice. Ownership is
  recorded on the position, not inferred.
- `TAKE_PROFIT` when `price ≥ position.target_price`.
- `MAX_AGE` when `trading_days_open ≥ MAX_HOLDING_DAYS` **and**
  `session.in_closing_window(now)`.
- Precedence when more than one applies: `STOP_LOSS`, then `TAKE_PROFIT`, then
  `MAX_AGE`. Fixed, so the recorded reason never depends on evaluation order.
- Boundaries are inclusive at the stop and the target.
- Must never place an order, and must never consult a halt — an active halt does
  not suppress exits.

### `zarabot/execution/orders.py`

Owns order submission, the submission locks, and crash recovery.

**`async open_position(signal: Signal, lots: int, instrument: Instrument) → Position`**
- Generates an idempotency key, records `SUBMITTING`, submits a market buy,
  settles the order, computes stop and target from the fill price, opens the
  position, and then places the standing stop-loss.
- Ordering constraint: the position row exists before the stop order is placed,
  so a crash in between leaves a position reconciliation can detect as
  unprotected. The reverse order would leave a stop order belonging to no known
  position.
- Before submitting, the requested lot count is checked against
  `broker.client.get_max_lots`; a request above it is reduced to the broker's
  maximum and logged, and a maximum of zero cancels the entry with a recorded
  rejection.
- On confirmation that the stop is standing, promotes the position to
  `EXCHANGE` via `db.positions.set_stop_protection`. Until that call the position
  remains `LOCAL` and the bot watches the stop itself, so no window exists in
  which nothing is watching.
- If the stop-loss cannot be placed after three attempts, the position is **not**
  unwound. It stays `LOCAL`, the owner is alerted, and
  `lifecycle.exits` enforces that position's stop by polling instead. Force
  selling a sound position because a secondary order failed would convert an
  operational problem into a realised loss.
- Raises `OrderRejected` after recording the rejection. The entry is **not**
  retried.
- Ordering constraint: the database write strictly precedes the broker call.
- Concurrency: held under a per-ticker lock and a global submission lock, both
  acquired through an async context manager that guarantees release on success,
  on exception, and on task cancellation.

**`async close_position(position: Position, trigger: ExitTrigger) → Position`**
- **Cancels the standing stop order first**, returns the position to `LOCAL`,
  then submits a market sell for the full position, settles, closes the position, and starts the ticker's cooldown.
  This order is binding: selling before cancelling leaves a live stop order
  against a position that no longer exists, which can sell a quantity the account
  does not hold.
- When the trigger is `STOP_LOSS`, the exchange has already sold. This function is
  not called; the fill is discovered by polling stop-order state or by
  reconciliation, and the position is closed from that fill.
- Raises `ExitFailed` after alerting, when the broker rejects or is unreachable.
  The caller retries on the next cycle. This is the documented exception to the
  no-retry rule.
- Must never be blocked by halt state, cooldown, or any risk limit.

**Partial fills.** An entry that fills partially opens a position for the lots
actually filled, sizes stop and target from the achieved average price, and
places the stop for that quantity. The unfilled remainder is abandoned, never
chased with a follow-up order — the strategy's entry price is stale by then, and
topping up would breach the one-open-position-per-ticker invariant. A partial
fill is alerted, because on a liquid watchlist it indicates the instrument is
thinner than the watchlist assumes. A partial fill on an **exit** is retried for
the remainder until the position is flat; a half-exited position is the one state
the system must never rest in.

**`async resolve_unfinished(now: datetime) → list[OrderRecord]`**
- For every unresolved order, queries `broker.client.get_order_state` by key and
  settles it; `OrderNotFound` settles it as never-placed.
- Opens or closes the corresponding position when a fill is discovered.
- Ordering constraint: completes before any new order is submitted in the
  process's lifetime.
- Must never resubmit an order.

### `zarabot/state/halt.py`

**Sole owner of the halt flag.**

**`async is_halted() → bool`** · **`async current() → HaltState | None`**

**`async halt(reason: HaltReason, detail: str, at: datetime) → None`**
- Persists the halt so it survives a restart. Idempotent when already halted.
- Suspends **entries only**. Never affects `lifecycle.exits` or
  `execution.orders.close_position`.

**`async resume(actor: str, at: datetime) → bool`**
- Clears the halt, recording who cleared it. Returns `False` when not halted.

### `zarabot/pnl.py`

**`realised(position: Position) → Decimal`** · **`unrealised(position: Position, price: Decimal) → Decimal`** — both net of commission.

Commission is the **actual figure reported by the broker** via
`broker.client.get_operations`, recorded on the order row when the order settles.
It is never estimated from a rate. On a small account, commission is a
material fraction of a 10% move, and an estimated figure would make every
realised P&L slightly and permanently wrong.

**`async daily_loss_pct(now: datetime) → Decimal`**
- Current equity against the day's opening baseline, as a percentage. Positive
  means a loss. The baseline is the snapshot written at session open, never
  allocated capital.

**`async benchmark_return(start: date, end: date) → Decimal | None`**
- Buy-and-hold return over the watchlist for the period.
- Returns `None` when any constituent price is missing — an unavailable benchmark
  is reported as unavailable, never as zero.

### `zarabot/telegram/notifier.py`

**`async alert(text: str, urgent: bool = False) → None`**
- Sends to the configured chat. Retries on failure, then logs and returns.
- **Never raises.** Telegram must never be able to interrupt trading.
- Never includes a token in a message.

### `zarabot/telegram/commands.py`

One handler per command in the brief's command table.

- Every handler first checks the sender against `TELEGRAM_CHAT_ID`; a mismatch
  logs and returns without replying and without any state change.
- Replies exceeding the platform limit are truncated with an explicit note of how
  many entries were omitted.
- No handler mutates a risk limit.
- `/halt` and `/resume` delegate to `state.halt` and to nothing else.

### `zarabot/reporter/weekly.py`

**`async build(start: date, end: date) → str`**
- Composes the report: P&L against benchmark, per-strategy performance, win rate,
  worst trade, exit-trigger distribution, cooldown-blocked signal count, and
  intended-versus-actual exit price for gapped exits.
- A week with no closed trades produces a valid report saying so.
- Undefined metrics are reported as not applicable, never as zero.
- Over the length limit, sections are dropped in this order — exit-trigger
  distribution, cooldown counts, worst trade — and the omission is noted.

**`async send(now: datetime) → None`** — builds and sends; failure alerts but does not raise.

### `zarabot/app/startup.py`

**`async start() → AppContext`**

Fixed ordering; each step completes before the next begins:
1. `config.load()` — abort on failure before anything else, including any network call.
2. `logging_setup.configure()`.
3. Open the database and `db.migrations.apply()`.
4. `strategies.registry.enabled()`, including model load if configured.
5. `market.session.refresh()`.
6. `execution.orders.resolve_unfinished()`.
7. `broker.reconcile.reconcile()`, then apply its remedies via
   `execution.orders`: re-protect unprotected positions, cancel orphaned stops,
   replace mispriced ones. Reconciliation identifies; the executor acts.
8. Restore halt state.
9. Alert the owner that the bot is running, reporting version, mode, halt state
   and any reconciliation adjustments.

- Raises `StartupError` on any failure, having alerted if Telegram credentials
  were valid. No entry may be attempted before step 9 completes.

### `zarabot/app/loops.py`

**`async trading_cycle(ctx: AppContext) → None`** — one iteration, in this fixed order:
1. If the session is closed, return without any broker call.
2. Refresh prices for open positions, and poll standing stop orders for
   execution. A stop filled by the exchange closes its position here.
3. Evaluate the remaining exits — take-profit, maximum age, and stop-loss only
   for `LOCAL`-protected positions — and submit them. **Before** any halt check,
   and before entries.
4. Recompute daily P&L; halt if the daily loss limit is breached.
5. If halted, return; entries stop here.
6. Fetch candles, evaluate strategies, and pass each signal through the gate.
7. Record every signal with its decision; execute the approved ones.

Steps 3 and 4 running before step 5 is what implements the brief's
halt-blocks-entries-only rule, and their order is binding.

**`async run(ctx) → None`** — schedules the trading cycle, the daily rollover, the nightly backup, the weekly report, and the heartbeat. A failure in one task must never terminate another.

### `zarabot/app/shutdown.py`

**`async shutdown(ctx, signal) → None`**
- Stops accepting new signals, waits for in-flight submissions to reach a known
  state or a bounded timeout, settles what it can, records state, closes the
  database, and exits.
- Must never cancel or liquidate positions.
- Orders unresolved at the timeout are left as `SUBMITTING` for the next startup
  to resolve — this is correct, not a leak.

### `zarabot/__main__.py`

The process entry point, so that `python -m zarabot` is the start command.

**`main() → int`**
- Installs signal handlers for `SIGTERM` and `SIGINT`, runs `app.startup.start()`,
  then `app.loops.run()`, and routes either signal to `app.shutdown.shutdown()`.
- Returns 0 on a clean shutdown, non-zero on `StartupError`.
- Contains no application logic of its own. Anything worth testing belongs in
  `app.*`; this module exists only to be the thing Python executes.
- On `StartupError` it sleeps 30 seconds before returning, so the container
  restart policy cannot produce an alert loop (rule 15).

### `zarabot/ops/backup.py`

**`async run(db_path: Path, backup_dir: Path) → Path`** — produces a consistent copy using SQLite's own backup mechanism, never a raw file copy of a live database.

**`async prune(backup_dir: Path, retention_days: int) → int`** — removes backups strictly older than the window; returns the count removed.

- Failure alerts and returns; it must never stop trading.

### `sandbox/` — laptop research (never imported by server code)

**`data.load(ticker, start, end) → list[Candle]`** — from local cache, downloading via `broker.client` when absent.

**`backtest.run(strategy, candles, config, commission, slippage) → BacktestResult`**
- Replays candles in order, calling the **same** `strategies`, `risk.sizing` and
  `lifecycle.exits` functions the live path uses. Reimplementing any of them here
  is a critical defect: it makes every backtest unfalsifiable.
- A strategy is never passed a candle timestamped at or after the decision
  instant.
- Applies commission and the configured slippage assumption to every fill.
- Returns trades, P&L, win rate, maximum drawdown, exit-trigger distribution, and
  the buy-and-hold benchmark.

**`train.fit(...) → Path`** and **`train.export(model, features, path) → Path`**
- Exports the model together with a feature manifest naming the features and
  their order, which `strategies.ml_model` validates on load.
- Uses walk-forward validation; a single train/test split is not acceptable for a
  time series.

---

## 5. Database schema

SQLite, one file. **Every monetary value is stored as `TEXT` holding the decimal
representation**, never `REAL`: SQLite's `REAL` is a binary float and cannot
represent prices exactly. Every timestamp is stored as `TEXT` in ISO-8601 with an
explicit UTC offset. Booleans are `INTEGER` 0 or 1.

### `schema_version`

| Column | Type | Notes |
|---|---|---|
| `version` | INTEGER | Primary key. Highest row is the current version |
| `applied_at` | TEXT | UTC ISO-8601 |

### `positions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ticker` | TEXT NOT NULL | |
| `figi` | TEXT NOT NULL | |
| `strategy` | TEXT NOT NULL | Name of the strategy that opened it. `ADOPTED` for reconciled holdings |
| `lots` | INTEGER NOT NULL | CHECK > 0 |
| `lot_size` | INTEGER NOT NULL | Units per lot at entry time |
| `entry_price` | TEXT NOT NULL | Decimal string, per unit |
| `entry_at` | TEXT NOT NULL | UTC. Age is counted from here |
| `stop_price` | TEXT NOT NULL | Computed at entry, stored — never recomputed from config later |
| `target_price` | TEXT NOT NULL | Same |
| `status` | TEXT NOT NULL | CHECK IN (`OPEN`, `CLOSED`) |
| `adopted` | INTEGER NOT NULL | Default 0. 1 when created by reconciliation |
| `open_order_key` | TEXT NOT NULL | FK → `orders(key)` |
| `close_order_key` | TEXT NULL | FK → `orders(key)`. Null while open |
| `exit_trigger` | TEXT NULL | CHECK IN (`STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`, `EXTERNAL`) |
| `exit_price` | TEXT NULL | |
| `exit_at` | TEXT NULL | UTC |
| `realised_pnl` | TEXT NULL | Net of commission, actual not estimated |
| `stop_protection` | TEXT NOT NULL | CHECK IN (`EXCHANGE`, `LOCAL`). Which side owns the stop trigger |
| `stop_order_key` | TEXT NULL | FK → `stop_orders(key)`. Null only when `stop_protection = 'LOCAL'` |

**Invariants.**
- A partial unique index over `ticker` where `status = 'OPEN'` enforces at most
  one open position per instrument.
- `status = 'CLOSED'` requires `exit_trigger`, `exit_price`, `exit_at` and
  `realised_pnl` all non-null; `status = 'OPEN'` requires all four null.
- Exactly one of the two stop owners is active: `stop_protection = 'EXCHANGE'`
  requires a live `stop_order_key`; `'LOCAL'` requires none. This is the
  invariant that prevents a position being sold twice.
- `stop_price` and `target_price` are frozen at entry. Changing `STOP_LOSS_PCT`
  in configuration must never move the stop of an already-open position.
- Rows are never deleted.

### `orders`

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | Primary key. Client-generated idempotency key. UUID4 in canonical form — the broker specifies a UID of at most 36 characters, which the canonical form occupies exactly |
| `ticker` | TEXT NOT NULL | |
| `figi` | TEXT NOT NULL | |
| `side` | TEXT NOT NULL | CHECK IN (`BUY`, `SELL`) |
| `intent` | TEXT NOT NULL | CHECK IN (`ENTRY`, `EXIT`) |
| `lots` | INTEGER NOT NULL | Requested |
| `status` | TEXT NOT NULL | CHECK IN (`SUBMITTING`, `SUBMITTED`, `FILLED`, `REJECTED`, `CANCELLED`, `UNKNOWN`) |
| `filled_lots` | INTEGER NULL | |
| `filled_price` | TEXT NULL | Average fill, decimal string |
| `commission` | TEXT NULL | |
| `broker_reason` | TEXT NULL | Broker's rejection text, verbatim |
| `created_at` | TEXT NOT NULL | Written **before** submission |
| `settled_at` | TEXT NULL | |

**Invariants.** `FILLED`, `REJECTED` and `CANCELLED` are terminal — no row leaves
them. A row in `SUBMITTING` means the outcome is unknown and must be resolved by
querying the broker with `key`, never by resubmitting.

### `stop_orders`

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | Primary key. Our idempotency key for the stop order |
| `stop_order_id` | TEXT NULL | The broker's identifier, once known |
| `position_id` | INTEGER NOT NULL | FK → `positions(id)` |
| `ticker` | TEXT NOT NULL | |
| `lots` | INTEGER NOT NULL | |
| `stop_price` | TEXT NOT NULL | Decimal string |
| `status` | TEXT NOT NULL | CHECK IN (`PLACING`, `ACTIVE`, `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`) |
| `created_at` | TEXT NOT NULL | |
| `settled_at` | TEXT NULL | |

**Invariants.** At most one stop order in `ACTIVE` or `PLACING` per open
position, enforced by a partial unique index on `position_id`. `EXECUTED` means
the exchange sold the position; the corresponding position must be closed with
`exit_trigger = 'STOP_LOSS'`.

### `signals`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ticker` | TEXT NOT NULL | |
| `strategy` | TEXT NOT NULL | |
| `generated_at` | TEXT NOT NULL | UTC |
| `reference_price` | TEXT NOT NULL | Price the strategy saw |
| `decision` | TEXT NOT NULL | CHECK IN (`APPROVED`, `REJECTED`) |
| `rejection_reason` | TEXT NULL | Non-null exactly when `decision = 'REJECTED'` |
| `lots` | INTEGER NULL | Non-null exactly when approved |
| `order_key` | TEXT NULL | FK → `orders(key)` when an order followed |

### `cooldowns`

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT | Primary key |
| `started_at` | TEXT NOT NULL | UTC, instant the position closed |

### `daily_snapshots`

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. **Moscow** calendar date |
| `opening_equity` | TEXT NOT NULL | Baseline for the daily loss limit |
| `closing_equity` | TEXT NULL | Null until the session closes |
| `cash` | TEXT NOT NULL | |
| `realised_pnl` | TEXT NOT NULL | For the day |
| `unrealised_pnl` | TEXT NOT NULL | At snapshot time |
| `open_positions` | INTEGER NOT NULL | |
| `orders_placed` | INTEGER NOT NULL | Observational only — there is no daily cap |
| `benchmark_value` | TEXT NULL | Null when unavailable, never 0 |

### `halt_state`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, CHECK (`id = 1`). Single row |
| `halted` | INTEGER NOT NULL | 0 or 1 |
| `reason` | TEXT NULL | CHECK IN (`DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`) |
| `detail` | TEXT NULL | Human-readable context for the alert |
| `halted_at` | TEXT NULL | |
| `resumed_at` | TEXT NULL | |
| `resumed_by` | TEXT NULL | `owner` or `system` |

### `instruments`

| Column | Type | Notes |
|---|---|---|
| `figi` | TEXT | Primary key |
| `ticker` | TEXT NOT NULL | Unique |
| `lot` | INTEGER NOT NULL | |
| `min_price_increment` | TEXT NOT NULL | |
| `currency` | TEXT NOT NULL | CHECK = `RUB` |
| `trading_status` | TEXT NOT NULL | |
| `refreshed_at` | TEXT NOT NULL | |

### `reconciliations`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ran_at` | TEXT NOT NULL | |
| `adjustments` | TEXT NOT NULL | JSON array. Empty array means agreement |

**Content structure** of `adjustments` — one object per adjustment:

```
- closed externally: {"type": "CLOSED_EXTERNALLY", "ticker": "...", "position_id": 12, "last_price": "123.45"}
- adopted:           {"type": "ADOPTED", "ticker": "...", "lots": 3, "average_price": "123.45"}
- lot mismatch:      {"type": "LOTS_ADJUSTED", "ticker": "...", "position_id": 12, "from": 3, "to": 2}
```

**Retention.** No table is ever pruned. Growth is a few megabytes a year and the
historical record is the purpose of the project. Backups are retained 30 days.

---

## 6. Migrations

- Files live in `migrations/` named `NNN_description.sql`, applied in ascending
  numeric order.
- The driver is `db.migrations.apply`, run by `app.startup` before any repository
  call. Each file is applied inside its own transaction; a failure rolls that
  file back and aborts startup.
- `001_initial.sql` creates every table above, the partial unique index on open
  positions, and seeds the single `halt_state` row with `halted = 0`.
- Migrations are forward-only. There are no down-migrations: a bad migration is
  corrected by a new migration, because rolling a schema backwards under a
  database holding real trade history is more dangerous than the defect.
- A database whose recorded version exceeds the code's highest migration aborts
  startup rather than running — this is a rolled-back deployment, and writing
  older code against a newer schema silently corrupts data.

---

## 7. Observability queries

Telegram is the interface; these queries back the weekly report and the `/pnl`
command. Null handling is stated because every one of these metrics is undefined
on an empty week.

**Per-strategy performance.** Group closed positions in the period by `strategy`,
summing `realised_pnl` and counting rows. A strategy with no closed positions
does not appear in the result and is reported as "no trades", not as zero P&L.

**Win rate.** Closed positions with `realised_pnl > 0` over total closed
positions, in the period. **When the denominator is zero the metric is reported
as not applicable** — a `NULL` from division must never be coalesced to 0, which
would read as "lost every trade".

**Exit-trigger distribution.** Count closed positions grouped by `exit_trigger`.
Triggers that never fired are shown explicitly as zero, since a week where the
take-profit never fired is a finding.

**Gap cost.** For `STOP_LOSS` exits, the difference between `exit_price` and
`stop_price`, aggregated. A negative aggregate is the money lost to overnight
gaps beyond the intended stop, and is the number that tells the owner what
holding overnight actually costs.

**Cooldown-blocked signals.** Count of `signals` rows in the period with
`rejection_reason = 'COOLDOWN_ACTIVE'`, grouped by ticker and strategy. A
strategy dominating this count is signalling too often.

**Daily loss percentage.** Current equity against `opening_equity` from today's
`daily_snapshots` row. If the row is missing — the first cycle after an unclean
restart — the value is recomputed from the broker portfolio and the row written,
never assumed to be zero.

**Uptime.** Derived from heartbeat log events per Moscow day: expected heartbeats
for trading days over observed heartbeats. Days the exchange was closed are
excluded from the denominator rather than counted as downtime.

### 7.1 Log events

Every event is one structured JSON record on stdout. All records carry
`timestamp` (UTC), `moscow_time`, `level` and `event`; the table lists the
additional fields each event **must** include. A record missing a required field
is a defect — these fields are what makes the log answerable after the fact.

| Event | Level | Required data fields |
|---|---|---|
| `startup_ok` | INFO | `version`, `mode`, `halted`, `adjustments_count` |
| `startup_failed` | CRITICAL | `stage`, `reason` |
| `config_invalid` | CRITICAL | `variable` |
| `session_open` / `session_closed` | INFO | `trade_date`, `opens_at`, `closes_at` |
| `candles_failed` | WARNING | `ticker`, `error` |
| `signal_generated` | INFO | `ticker`, `strategy`, `reference_price` |
| `signal_rejected` | INFO | `ticker`, `strategy`, `rejection_reason` |
| `order_submitting` | INFO | `key`, `ticker`, `side`, `intent`, `lots` |
| `order_filled` | INFO | `key`, `ticker`, `filled_lots`, `filled_price`, `commission` |
| `order_rejected` | ERROR | `key`, `ticker`, `intent`, `broker_reason` |
| `order_unresolved` | WARNING | `key`, `ticker`, `age_seconds` |
| `order_resolved` | INFO | `key`, `resolved_status`, `source` |
| `position_opened` | INFO | `position_id`, `ticker`, `strategy`, `lots`, `entry_price`, `stop_price`, `target_price` |
| `position_closed` | INFO | `position_id`, `ticker`, `exit_trigger`, `exit_price`, `realised_pnl`, `gap_vs_stop` |
| `exit_failed` | ERROR | `position_id`, `ticker`, `attempt`, `error` |
| `stop_order_placed` | INFO | `position_id`, `ticker`, `stop_price`, `stop_order_id` |
| `stop_order_cancelled` | INFO | `position_id`, `stop_order_id`, `cause` |
| `stop_order_executed` | INFO | `position_id`, `ticker`, `fill_price`, `gap_vs_stop` |
| `stop_protection_degraded` | ERROR | `position_id`, `ticker`, `attempts` |
| `stop_order_orphaned` | ERROR | `stop_order_id`, `ticker` |
| `partial_fill` | WARNING | `key`, `ticker`, `intent`, `requested_lots`, `filled_lots` |
| `cooldown_started` | INFO | `ticker`, `active_until` |
| `halt_triggered` | CRITICAL | `reason`, `detail`, `daily_loss_pct` |
| `halt_cleared` | INFO | `actor` |
| `reconciliation` | INFO | `adjustments_count`, `types` |
| `broker_unavailable` | WARNING | `method`, `consecutive_failures`, `backoff_seconds` |
| `rate_limited` | WARNING | `method`, `retry_after_seconds` |
| `db_write_failed` | ERROR | `table`, `critical` |
| `telegram_send_failed` | WARNING | `attempt`, `error` |
| `unauthorised_command` | INFO | `chat_id`, `command` |
| `secret_redacted` | ERROR | `sink` — never the secret, never its length |
| `backup_ok` / `backup_failed` | INFO / ERROR | `path`, `bytes` / `error` |
| `task_crashed` | ERROR | `task`, `error`, `restart_in_seconds` |
| `clock_drift` | WARNING | `drift_seconds` |
| `heartbeat` | INFO | `uptime_seconds`, `open_positions`, `halted` |
| `weekly_report_sent` | INFO | `period_start`, `period_end` |

`gap_vs_stop` on `position_closed` is populated only for `STOP_LOSS` exits and
carries the difference between the actual exit price and the stop price. It is
the field the gap-cost query aggregates, and omitting it makes the true cost of
overnight holding unmeasurable.

---

## 8. Error handling rules

Applies across all modules. Every external failure mode has exactly one rule.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.
2. **Broker rate limited** → WARNING, back off per the broker's hint. Alert once
   if sustained beyond five minutes. Never treat as fatal.
3. **Entry order rejected** → ERROR, record `broker_reason`, alert, open no
   position. **Never retried.**
4. **Exit order rejected or broker unreachable during an exit** → ERROR, alert
   **immediately**, retry on every following cycle until the position closes or
   the owner intervenes. The documented exception to rule 3.
5. **Order submission times out or the outcome is unknown** → leave the row
   `SUBMITTING`, resolve by querying with the idempotency key on the next cycle
   or at next startup. **Never resubmit.**
6. **Order found at the broker that is unknown locally** → adopt the position,
   alert. Never ignore.
7. **Broker and database disagree on positions or quantities** → the broker wins,
   the local record is corrected, and the owner is alerted with specifics.
8. **Instrument metadata unavailable mid-session** → skip that ticker for the
   cycle, WARNING. Unavailable at startup → `StartupError`; the bot must not
   trade an instrument whose lot size it cannot confirm.
9. **Candle fetch fails for one ticker** → omit it, WARNING, continue the batch.
10. **Trading schedule unavailable** → treat the market as closed, WARNING, alert
    once. The safe default is not to trade.
11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.
12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.
13. **Telegram send failure** → retry, then log. **Never propagates.** Telegram
    being down never delays or blocks a trading decision.
14. **Telegram command from an unauthorised chat** → INFO log with the chat
    identifier, no reply, no state change.
15. **Configuration missing or invalid at startup** → refuse to start, alert if
    Telegram credentials are among the valid ones, sleep 30 seconds, exit
    non-zero. The sleep exists so the container restart policy cannot produce an
    alert loop.
16. **Schema version ahead of the code** → refuse to start, alert, change nothing.
17. **Model file missing, unreadable, or with a mismatched feature manifest**
    while ML is enabled → refuse to start. A silently disabled model would mean
    trading a different system than the owner believes.
18. **Backup failure** → ERROR, alert, trading continues. A missing backup is not
    worth stopping trading over; it is worth knowing about.
19. **Secret exposure** → no token is ever written to a log, an exception message,
    or a Telegram message. If the redaction filter detects a secret in an
    outgoing Telegram message, the message is **dropped**, and an alert reporting
    the incident without the secret is sent in its place.
20. **Daily loss limit breached** → halt, persist the halt, alert with the loss
    and the trades that produced it. Exits continue to run.
21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.
22. **A naive datetime crosses a module boundary** → `ValueError`. This is a
    programming defect, not a runtime condition; it fails loudly rather than
    being coerced to a guessed timezone.
23. **Protective stop order rejected or unplaceable** → retry three times, then
    mark the position `stop_protection = 'LOCAL'`, alert, and enforce the stop by
    polling. Never unwind a sound position because a secondary order failed.
24. **Stop order found with no matching open position** → cancel it as an orphan
    and alert. A live stop against a position that no longer exists can sell
    stock the account does not hold.
25. **Open position found with no live stop order** while
    `stop_protection = 'EXCHANGE'` → place a replacement immediately and alert.
    An unprotected position is the state this whole mechanism exists to prevent.
26. **Stop order executed by the exchange** → not an error. Close the position
    from the fill with `exit_trigger = STOP_LOSS`, start the cooldown, alert.
27. **Exit order partially filled** → retry the remainder until flat. A
    half-exited position must never be a resting state.
28. **Any code path that would set `confirm_margin_trade=True`** → rejected in
    review, not at runtime. There is no runtime condition under which this is
    correct; it is listed here because the failure mode it would produce —
    losses exceeding allocated capital — is the one failure the brief promises
    cannot happen.
29. **System clock more than 5 seconds from reference** → alert. Beyond 60
    seconds → halt: session boundaries and candle alignment can no longer be
    trusted.

---

## 9. Dependencies

Versions below were resolved against the package indexes on 2026-08-18. They are
floors, not pins: exact versions and hashes are written to the lockfile once the
verification suite in §2 passes, and the application is built from the lockfile.

**The broker SDK is not on public PyPI.** Both `tinkoff-investments` and
`t-tech-investments` return HTTP 404 from pypi.org. The official SDK is published
only to a T-Bank-hosted index:

```
pip install t-tech-investments \
  --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

This has consequences the deployment section must handle: the image build depends
on a single third-party index that is not PyPI, cannot be assumed to be as
available or as long-lived, and complicates hash-pinning. See §10.

Three dependency sets: `requirements-server.txt` runs on the VPS;
`requirements-sandbox.txt` adds research tooling and is never installed on the
server; `requirements-dev.txt` adds the test and lint toolchain and is installed
neither on the server nor in the image.

**Server**

| Package | Constraint | Reason |
|---|---|---|
| `python` | 3.12 | Match statements and modern typing used throughout the contracts |
| `t-tech-investments` | 1.49.1 | Official T-Invest SDK, gRPC. Exposes `AsyncClient` and `Client` from the **`t_tech.invest`** package — note the import root is `t_tech`, not `tinkoff`. **T-Bank index only — not on PyPI.** Wheel `t_tech_investments-1.49.1-py3-none-any.whl`, sha256 `b18ea2da…7eba`. Declares `Requires-Python >=3.8` with classifiers through 3.14 |
| `python-telegram-bot` | ≥ 22.8 | Async Telegram client with command routing built in |
| `aiosqlite` | ≥ 0.22 | Async SQLite access, so database calls do not block the event loop |
| `structlog` | ≥ 26 | Structured JSON logging with a processor pipeline — the redaction filter of rule 19 is a processor |
| `scikit-learn` | ≥ 1.9 | Loading the exported model. Server-side inference only, never training |
| `joblib` | ≥ 1.5 | Model file format shared with the sandbox |
| `numpy` | ≥ 2.5 | Numeric work inside strategies; transitively required by scikit-learn anyway |

**Sandbox (laptop only)**

| Package | Constraint | Reason |
|---|---|---|
| `pandas` | ≥ 3.0 | Backtest analysis and report prototyping |
| `matplotlib` | ≥ 3.11 | Equity curves and drawdown plots |
| `jupyterlab` | ≥ 4.6 | The research surface described in the brief |

**Development**

| Package | Constraint | Reason |
|---|---|---|
| `pytest` | ≥ 9.1 | Test runner |
| `pytest-asyncio` | ≥ 1.4 | Coroutine tests. `asyncio_mode = "auto"` remains valid in 1.x — the 1.0 release removed *legacy* mode and made *strict* the default, so the setting must be stated explicitly, which §3.1 does |
| `pytest-cov` | ≥ 7.1 | Coverage thresholds of §3.1 |
| `ruff` | ≥ 0.16 | Lint and format in one tool |
| `mypy` | ≥ 2.3 | The contracts in §4 are type annotations; unchecked annotations drift |

**Deliberately absent.** No SQLAlchemy or Alembic — eight tables behind
repository functions do not need an ORM, and hand-written SQL keeps the schema in
this document rather than in Python classes. No `freezegun` — the `clock` module
already makes time injectable, and a library that patches time globally would
hide a clock dependency this spec forbids. No `pandas` on the server — the
trading path must not depend on a heavyweight numeric stack.

---

## 10. Deployment

**Prerequisites.** Every step of §1 completed, and every script in §2 exiting 0.

**Build.** The image installs the broker SDK from the T-Bank index with an
explicit `--extra-index-url`, and everything else from PyPI. Two consequences
must be handled rather than discovered:

- **Vendor the SDK wheel into the repository** and install from the local file in
  the image build. The build must not require the T-Bank index to be reachable at
  the moment you deploy — that index being down is otherwise enough to stop you
  shipping a fix during a trading session. Refresh the vendored wheel
  deliberately, as its own change.
- **Hash-pin everything installed from PyPI.** The SDK, installed from a vendored
  file, is pinned by the file itself. Mixing an extra index into a hash-checked
  install is the classic dependency-confusion surface, and vendoring closes it.

**Start command.** `python -m zarabot`, run as a non-root user inside the
container.

**Compose service.**
- `restart: unless-stopped` — survives crashes and host reboots.
- `env_file: /opt/zarabot/.env`, mode `600`, never baked into the image.
- Volume `/opt/zarabot/data` → `/data`, holding the database and backups.
- `stop_grace_period: 60s` — long enough for `app.shutdown` to settle an in-flight
  order rather than being killed mid-submission.
- Log driver with size-based rotation, capped so logs cannot fill the disk.
- `TZ=Europe/Moscow`.
- No published ports. The container listens on nothing.

**Failure behaviour on start.** Any `StartupError` alerts if possible, sleeps 30
seconds, and exits non-zero (rule 15). The restart policy retries; the sleep
bounds the loop to two attempts a minute so a bad deploy cannot flood Telegram or
the broker.

**Graceful shutdown.** `SIGTERM` triggers `app.shutdown`: stop accepting signals,
settle in-flight submissions or leave them `SUBMITTING` for the next startup,
close the database, exit 0. Positions are never cancelled or liquidated.

**Deploy flow.** Pull, build, `up -d`, then confirm the startup alert arrives in
Telegram reporting version, mode, halt state and reconciliation result. A deploy
that produces no startup alert has failed, regardless of what the container
status says.

**Deploy window.** Outside the trading session whenever avoidable. A deploy
during the session is safe by design — unresolved orders are recovered by key and
positions are reconciled — but it consumes that safety margin for no reason.

**Verification after deploy.** `/status` returns, `/positions` matches the
broker's own application, and the next heartbeat arrives on schedule.
