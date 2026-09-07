# Zarabot — Technical Specification

**Version:** 1.61
**Date:** 2026-09-07
**Implements:** `business-brief.md` v1.11

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

**SDK types.** Domain types never wrap SDK types, and pure modules never import
the SDK. The one permitted exception is `CandleInterval`, used as a **parameter**
in `broker.client`, `market.data` and `sandbox.*` — all of which already depend
on the SDK. It never appears on a domain dataclass and never reaches
`strategies.*`, which receive `list[Candle]` and no interval at all.

**Network boundary.** `broker.client` is the only module that makes network
calls to the broker. `telegram.notifier` and `telegram.commands` are the only
modules that make network calls to Telegram. All tests mock at these boundaries.

**Secrets.** No module logs, returns, or includes in an exception message the
value of `TINVEST_TOKEN`, `TINVEST_ACCOUNT_ID`, their `*_SANDBOX` counterparts,
or `TELEGRAM_BOT_TOKEN`. An account identifier in a log is as identifying as a
token. See error rule 19.

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
9. **Create the data directory** inside the clone — `data/` and
   `data/backups/` — owned by the user the container runs as, uid 1000. The
   bind mount keeps the host's ownership, so a root-owned directory here starts
   the container and then fails on the first query.
10. **Write the environment file** `.env`, beside `docker-compose.yml` in the
    clone, from `.env.example`, with file mode `600`. It contains both tokens
    and is never committed.

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
NTP source, the deploy's data directory writable, and outbound connectivity to
both the
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

### 2.1 Measured values

The suite ran green for the first time on **2026-08-27** — V1–V11, 11 of 11.
Everything below was previously an assumption. It is recorded here because the
two most expensive defects in this project (#39 and #43) were both assumptions
that inspection could not have falsified.

| Measured | Value | Why it matters |
|---|---|---|
| SDK | 1.49.1, wheel sha256 `b18ea2da…7eba` | V10's regression baseline |
| Lot sizes | SBER 1, **GAZP 10**, LKOH 1, MGNT 1 | Sizing is in lots; a wrong lot size is a wrong position size |
| Price steps | SBER/GAZP 0.01, **LKOH/MGNT 0.50** | A stop price off-step is rejected by the exchange |
| Candle depth | 456 daily candles available | Floor is 250; the longest lookback plus a margin |
| `LastPrice` fields | Exactly `figi`, `price`, `time`, `instrument_uid`, `last_price_type`. **No `timestamp`** | Measured 2026-08-28. `_quote_time` probed for a `timestamp` that has never existed; the probe could only ever mask a rename of `time` as a rejected quote (#33) |
| SDK deprecations | `share_by` and `get_last_prices` are **deprecated as of SDK 1.0.0** | Both are on the hot path. Noted, not acted on: they work at 1.49.1, and a migration is its own change with its own verification |
| **Trading schedule, past** | **Not obtainable.** Any `from_` before today's midnight is rejected with `INVALID_ARGUMENT` / **30003** | Measured 2026-08-28 against the live account across seven ranges — 14 days back, 7, 1, and every end date from midnight to +7d. Every one failed; only a range starting at today's midnight is served. This is why `MAX_AGE` cannot simply be given a backward window (#45) |
| Longest legitimate candle gap | **6 calendar days** (2025-12-30 → 2026-01-05, the New Year closure) | Recurs annually; a continuity check below it fails on correct data |
| Market-data rate limit | 200 requests / 60s | Measured headroom 400× the loop's 1.0 calls/min |
| **`PostOrder` rate limit** | **2 / second** | The one limit close enough to matter; an exit loop slicing an order can reach it |
| Price polling cost | `get_last_prices` takes the **whole watchlist in one call** | A poll cycle costs 1 request, not one per instrument (#19) |
| Calendar horizon | **14 days measured from the start of the day** | Exceeding it returns `INVALID_ARGUMENT`/`30002`, "The required period should not exceed 14 days" (#39) |
| Exchange name | `MOEX`, main board, 10:00–18:54:59 MSK, weekends closed | 53 of the 147 returned exchanges contain "MOEX"; one of them trades weekends (#43) |
| Duplicate idempotency key | **Refused** — `INVALID_ARGUMENT`/`30057` | It does *not* return the existing order. Recovery is `get_order_state` by key, confirmed working across processes |
| Stop-order expiry | `GOOD_TILL_CANCEL` accepted; `expiration_time` returns epoch zero, meaning **unset** | The stop survives a restart — brief acceptance criterion 16, previously untested |
| Telegram | 4096-character ceiling accepted | The truncation contract assumes exactly this |

**Two protobuf sentinels appear repeatedly and both have bitten:** an unset
timestamp is `1970-01-01`, not `None`, so any check reading it as a value rather
than as absence reads a closed day or an unexpiring order as the opposite of
what it is.

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

**`models`**
- A dataclass constructed with a naive datetime raises `ValueError`, for every
  type carrying a timestamp (proves the timezone invariant is enforced at the
  boundary rather than trusted).
- A negative lot count, a negative price, or a non-positive lot size raises
  `ValueError` (proves the domain rejects impossible values before they can
  reach an order).
- A monetary field given a `float` raises `TypeError` (proves the Decimal rule
  is enforced by the type, not by discipline — this is the test that stops a
  float leaking in from the SDK or a JSON payload).
- Every dataclass is frozen: assigning to a field raises (proves domain objects
  cannot be mutated in place behind a caller's back).
- `RiskDecision` cannot be constructed both approved and rejected, nor neither
  (proves the decision is total — every signal gets exactly one outcome).
- An approved `RiskDecision` with a lot count of zero raises (proves approval
  always means a placeable order).
- Every enum member round-trips through its string value unchanged (proves the
  values written to the database and read back are stable, since the schema
  stores them as TEXT with CHECK constraints naming them).
- A `Position` with `stop_protection = EXCHANGE` and no stop order key raises,
  as does `LOCAL` with one (proves the ownership pairing at the type level, not
  only in the repository).
- An `OperationRecord` round-trips `operation_type`, `state` and
  `parent_operation_id` as the broker's own values (proves a sale is identified
  by what the broker called it, not inferred from the sign of `payment`).

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
- `CASH_RESERVE_PCT` above 50 raises `ConfigError` (boundary — a reserve above
  half of cash is a configuration error, not a preference).
- `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeding 100 raises `ConfigError`
  (proves the allocation cannot be structurally over-committed).
- `TAKE_PROFIT_PCT` less than or equal to `STOP_LOSS_PCT` raises `ConfigError`
  (proves a configuration that can never profit is rejected).
- An empty `WATCHLIST` raises `ConfigError` (proves the bot cannot start with
  nothing to trade).
- The string form of the config object contains neither token (proves accidental
  logging of the whole config leaks nothing).
- `allow_foreign_holdings` defaults to false when unset, and a near-miss spelling
  (`1`, `yes`, `TRUE`, `on`) raises `ConfigError` rather than enabling it (proves
  the safe default — this flag exists to be set deliberately by the owner, never
  to be arrived at).

**`logging_setup`**
- A log record whose message contains the token value emits the token replaced by
  a fixed mask (proves redaction on the message).
- A log record carrying the token in a structured field is redacted (proves
  redaction is not message-only).
- An exception whose string representation contains the token is redacted when
  logged with a traceback (proves redaction survives exception formatting).
- A record containing no secret passes through byte-identical apart from the
  base schema fields (proves redaction does not corrupt ordinary logs).
- Every record carries `timestamp`, `moscow_time`, `level`, `logger` and
  `message`, and `moscow_time` is the same instant as `timestamp` converted to
  `Europe/Moscow` (v1.60; proves the base schema of §7.1 is emitted here and
  cannot be forgotten by a producer).
- A record logged with `extra={"event": "heartbeat"}` carries `event`
  unaltered, and a record logged without one carries no `event` key (v1.60;
  proves the field is producer-set and that this module invents nothing).
- A token inside the `event` field is redacted (proves redaction reaches the
  field the whole catalogue is keyed on).
- A log record carrying an account identifier is redacted the same way as a
  token (v1.61).

**`db.migrations`**
- Applying migrations to an empty database creates every table **and leaves
  `schema_version` at the highest migration present in `migrations/`**, asserted
  against the files on disk rather than against a literal (happy path). Asserting
  only that the tables exist passes even when the driver stops after `001`, since
  `001` creates every table and later migrations only add columns — so the case
  must also assert a column a later migration introduces, currently
  `orders.exit_trigger`.
- After `apply`, `PRAGMA journal_mode` reports `wal` and `PRAGMA foreign_keys`
  reports `1` on the connection it was given (proves the pragmas live on the
  connection rather than in a comment — the gap that left every declared foreign
  key decorative at runtime).
- Applying migrations twice makes no changes the second time and does not raise
  (proves idempotency).
- A database at version N−1 is migrated to N without data loss in existing rows
  (proves forward migration preserves history).
- A database whose recorded version is **higher** than the code's raises
  `MigrationError` and does not modify anything (proves a rolled-back deployment
  cannot silently corrupt a newer schema).

**`db.connection`**
- `connect` on a new file, then `shared()`, returns a live connection, and
  importing the module opens no file (proves there is no import-time side effect,
  which `AGENTS.md` forbids outside `config`).
- A second `connect` without `disconnect` raises `DatabaseAlreadyOpenError`
  (proves the process holds one connection rather than silently leaking the
  previous file).
- `shared()` before `connect`, and after `disconnect`, raises
  `DatabaseNotOpenError` and opens no fallback connection (proves a test cannot
  inherit a connection, and production cannot quietly reconnect to the wrong
  file).
- `disconnect` is idempotent: a second call does not raise.
- After `connect`, `PRAGMA journal_mode` is `wal`, `PRAGMA foreign_keys` is `1`
  and `PRAGMA busy_timeout` is `30000`.
- Two concurrent tasks, one writing a row and one reading another table, both
  complete (proves WAL: a writer does not block a reader, which is the contention
  the 30-second timeout was absorbing).
- Two sequential tests connect to different temporary paths, and the second
  observes none of the first's rows (proves isolation without inheriting a
  process connection).
- Two writers in **different modules**, invoked concurrently, both complete and
  neither raises `cannot start a transaction within a transaction` (proves the
  transaction is serialised process-wide rather than per module — the collision
  reproduced on the first attempt in #40).
- A nested `transaction()` on the same task joins the outer one: the inner block
  exiting does not commit, and an exception after it rolls back the outer work
  too (proves `broker.reconcile` can call `db.positions.adopt` without
  deadlocking or committing early).
- A write inside `transaction()` does **not** survive that transaction's
  rollback, even when another module performs a write of its own in between
  (proves a foreign commit can no longer make half-written rows durable — the
  defect that made `rollback()` meaningless on the money path).
- A read path takes no transaction: reads succeed while another task holds one.
- A write that fails with `aiosqlite.Error` inside `transaction()` emits
  `db_write_failed` with `table` (the SQLite object name when the error names
  one, otherwise `unknown`), then the exception still propagates (v1.61).
- A failed write inside `transaction(critical=False)` reports `critical` false
  and still raises into the caller's `except aiosqlite.Error`; a failed write
  inside a default `transaction()` reports `critical` true (v1.64; proves the
  field distinguishes rule 11 from rule 12 rather than asserting the same thing
  every time).
- A rule-12 write failing with an error that names no table — `database is
  locked`, disk full — still reports `critical` false (v1.64; proves the flag
  follows the caller's declared rule and not the wording of the error, which is
  where v1.62's table-derivation failed).
- A rule-12 repository whose write fails produces **exactly one**
  `db_write_failed` record (proves `db.connection` is the single owner and the
  repository adds none of its own).

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
- `open`, `set_stop_protection`, `update_lots`, `close`, `recompute_realised` and
  `adopt` each write exactly one `position_events` row, in the same transaction
  as the mutation (proves the trail cannot diverge from the row it describes).
- A `close` that rolls back leaves no event behind for that attempt (proves the
  event is not committed independently of the mutation).
- `close` with an `exit_commission` on an `EXTERNAL` trigger nets it from
  realised P&L and stores it; the same value with any other trigger raises
  `ValueError` (proves the second commission source exists only where there is no
  first one).
- `recompute_realised` on an `EXTERNAL`-closed position preserves that
  commission rather than dropping it to zero (proves the backfill cannot undo a
  figure the operations feed resolved).
- After a sequence of `set_stop_protection` calls, `list_events` reconstructs the
  full stop-ownership history in order (proves post-incident reconstruction needs
  nothing but the database).
- `adopt` against a database seeded with **nothing but the migrations** and a
  single order row succeeds (proves the foreign key it must satisfy is satisfied
  — the case that was never written, because every existing fixture happened to
  pre-insert an `ADOPTED-{figi}` order row and so tested a database where the
  constraint was satisfiable by accident).
- `adopt` with an `open_order_key` naming no order row raises an error whose
  cause is the **foreign key**, and specifically not `PositionStateError("open
  position already exists")` (proves the broad translation that named the wrong
  cause is gone).
- The adopted position's `open_order_key` reads back as the key passed in, and
  `db.orders.get` on it returns that order (proves the row points at something
  real).
- No function in this module calls `aiosqlite.connect`, and none issues `BEGIN`,
  `commit` or `rollback` (proves it runs on `db.connection.shared()` inside
  `db.connection.transaction()` — the defect that opened a connection per call,
  and then the one where a bare commit made another module's half-written rows
  durable).

**`db.orders`**
- `settle` with a `broker_order_id` reads it back on the row, and without one
  leaves it `None` (proves the identifier survives, which is the whole
  mechanism by which a late commission becomes recoverable).
- `mark_commission_alerted` twice keeps the first timestamp (proves the fact
  recorded is *when the owner was first told*, not when it was last considered).
- A row already alerted is still returned by `list_missing_commission` (proves
  the terminal state stops the telling, not the trying).
- An order recorded as `SUBMITTING` then confirmed as `FILLED` reports the
  terminal state (happy path).
- Orders left in `SUBMITTING` are returned by the unresolved-orders query
  (proves crash recovery can find them).
- `settle` with a commission persists it; with `None` leaves it unknown, which
  reads back distinctly from zero (proves "not yet reported" and "free" are not
  conflated — they produce different P&L).
- `record_commission` succeeds on a terminal row, and `list_missing_commission`
  stops returning that order afterwards (proves the backfill terminates rather
  than revisiting the same orders forever).
- Recording two orders with the same idempotency key raises `DuplicateOrderError`
  (proves the uniqueness invariant is enforced at the storage layer).
- Recording an `EXIT` without an `exit_trigger` raises `ValueError`, as does an
  `ENTRY` with one (proves the pairing, so no exit can be submitted without its
  reason captured).
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
- **An `INVALID_ARGUMENT` response does not raise `BrokerUnavailable`** — it
  propagates with its cause intact (proves a malformed request is a defect rather
  than weather, which is the confusion that hid #39 for the life of the
  deployment).
- An `AttributeError` raised inside the wrapper — a renamed SDK field — reaches
  the caller as an `AttributeError` with its traceback (proves the catch is
  narrowed to the SDK's own failures and `from None` is gone).
- `get_trading_schedule` requests the exchange named exactly `MOEX`, and a
  Saturday and a Sunday in the result are non-trading days (proves the calendar
  is the main board's and not an extended session that trades weekends, #43).
- `get_trading_schedule` anchors its range to the start of the current UTC day
  (proves the request the broker rejects with `INVALID_ARGUMENT` is not sent:
  mid-day plus 14 days fails, midnight plus 14 days does not, #39).
- A day returned with `is_trading_day` false yields no session even when its
  start and end are `1970-01-01` (proves the flag is honoured before the
  timestamps).
- A `PARTIALLYFILL` report maps to `SUBMITTED` with `filled_lots` below `lots`,
  never to `FILLED` (proves a partial fill stays visible as partial, #10).
- `get_executed_stop_fills` returns the broker's executed price, lots and
  commission for a stop that fired, keyed by `stop_order_id` (proves the exit is
  booked from the broker's own record rather than from a quote, #4).
- A stop whose `exchange_order_id` does not resolve is **omitted** from the
  result rather than returned with a substituted price (proves the caller is left
  to retry rather than handed a guess).
- Nothing fired in the window → empty dict, not `None`.
- Two successive calls reuse one `AsyncClient`, and `close()` then releases it
  (proves the channel is per process rather than per request, #18).
- `config.get()` is called once across a sequence of broker calls (proves the
  configuration is not re-read and re-validated per request).
- A rate-limit response raises `BrokerRateLimited` carrying the retry hint
  (proves the caller can back off correctly).
- A quote object with no `time` attribute raises `AttributeError`, **not**
  `PriceRejected` (proves a renamed SDK field surfaces as the integration break
  it is, rather than as every quote in every cycle looking like bad broker data
  — the state in which no `LOCAL` stop-loss can fire).
- Raising `BrokerUnavailable` emits `broker_unavailable` with `method` and
  `consecutive_failures` (v1.61). Raising `BrokerRateLimited` emits
  `rate_limited` with `retry_after_seconds`.
- A method that fails twice then succeeds reports `consecutive_failures` 1 then
  2, and a later failure reports 1 again (v1.65; proves the count is cleared on
  the success path rather than ratcheting for the life of the process).
- A zero-valued quote raises `PriceRejected`, not `BrokerUnavailable` and not
  `Decimal(0)` (proves the mass-liquidation path is closed at its source, and
  that bad data is distinguishable from an outage).
- A quote older than `price_max_age_seconds` raises `PriceRejected` (boundary:
  exactly at the threshold is accepted, one second beyond is not).
- A price more than `price_max_move_pct` from the last accepted price raises
  `PriceRejected`, and the last accepted price is **unchanged** afterwards
  (proves an implausible value cannot become the baseline that makes the next
  one look reasonable).
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
- Broker and database agreeing produces no adjustments and no alert (happy path)
  and still emits `reconciliation` with `adjustments_count` 0 (v1.61).
- A position open in the database but absent at the broker, whose operations feed
  shows a sale, is closed locally as externally closed and alerted (proves the
  broker is authoritative).
- That close records the sale's price of 110, not the `get_last_price` of 92 at
  the moment of detection, and dates `exit_at` to the sale rather than to `now`
  (proves the exit is booked at what was traded and when — #11's own verification
  case).
- Two sales of quantity 3 and 2 at 100 and 90 record a quantity-weighted 96.00
  and an `exit_commission` summing both fee operations (proves aggregation over
  the feed, and that the fee is no longer lost for want of a closing order row).
  The weights are the broker's `quantity` values whatever unit they are in, so
  the case does not encode an untested assumption about that unit.
- The broker being unavailable leaves the position **open**, reports
  `EXIT_UNRESOLVED` and alerts — and in particular records no exit at
  `entry_price` (proves the zero-P&L fabrication is gone).
- An operations feed containing no covering sale for the figi behaves identically
  (proves absence and unavailability are both "unknown", never "zero").
- A holding present at the broker but absent locally is reported as
  `FOREIGN_HOLDING` naming its ticker, lots and average price, and **no position
  row is written** (proves the bot no longer takes ownership of shares it did not
  buy — the path that adopted a manual holding at its cost basis and sold it on
  the next cycle).
- A holding 40% above its average cost is reported, not adopted, and no exit is
  submitted for it (proves the specific liquidation this policy exists to
  prevent).
- A holding whose ticker has an unresolved `ENTRY` order is adopted rather than
  reported foreign (proves crash recovery still works: the bot bought this, the
  fill landed, and the process died before the row was written).
- The adopted position's `open_order_key` is that unresolved order's key, not a
  synthesised one (proves the adopted row points at the order the bot actually
  submitted — the whole reason the foreign key exists).
- With two unresolved `ENTRY` orders for one ticker, the **oldest** is used
  (proves the documented tie-break, in the state the submission lock is supposed
  to make impossible).
- A holding whose entry order has already reached a terminal status is reported
  foreign (proves the recognition rule is the narrow one, and cannot be widened
  into adopting what the owner bought).
- An externally-closed position is closed with `order = None` and **no row is
  written to `orders`** (proves reconciliation records only what the bot actually
  submitted).
- Two live stops on one open position report `STOP_DUPLICATE` naming both, with
  `keep` set to the one matching the position's `stop_order_key` and `cancel`
  listing the rest (proves the double-sell condition is detected rather than
  half-claimed, and that the caller is told which stop to keep rather than left
  to re-derive the rule).
- With no `stop_order_key` recorded, `keep` is the oldest stop by `created_at`
  (proves the documented tie-break).
- A stop the broker holds one **half** increment below the position's stop price
  reports **no** `STOP_MISPRICED` (proves the tick-snapped price the broker
  actually holds is not read as a discrepancy — the finding that cancelled and
  re-posted all three live stops on every restart).
- A stop a **full** increment away is still reported `STOP_MISPRICED` (proves the
  tolerance is one increment and not an open-ended blur).
- A stop within the increment on a `LOCAL` position is reported `STOP_ADOPTABLE`
  (proves the tolerated stop takes the adoption path, not the replacement one).
- When `get_instrument` fails for the position's ticker, no `STOP_MISPRICED` is
  reported, the failure is alerted, and reconciliation still returns its other
  findings (proves an unmeasurable discrepancy does not become a cancel-and-
  re-post).
- A `LOCAL` position whose increment cannot be read reports **no
  `STOP_ADOPTABLE`** either, and its `stop_protection` is still `LOCAL` after
  reconciliation (proves the price comparison guards adoption as well as
  replacement — the v1.47 defect that would have handed protection to a stop
  standing at a price nobody could verify, and stopped `lifecycle.exits`
  watching the position's own).
- An instrument reporting a `min_price_increment` of zero is treated exactly as
  an unreadable one, alert included (proves the blind path has one entrance,
  not one alerted and one silent).
- A lot-count mismatch adopts the broker's count and alerts (proves quantity
  reconciliation).
- Reconciliation applies each **corrective write** at most once: running it twice
  against an unchanged broker closes, adopts or re-lots nothing the second time
  (proves it does not thrash). Stop-order *findings* are re-reported until the
  caller remedies them, which is correct — this module observes, and an
  unremedied discrepancy is still true on the second pass.
- The module calls `aiosqlite.connect` nowhere; the reconciliation row is written
  on `db.connection.shared()` (proves the shared connection reached the two
  modules outside `db.*` that were opening their own).

**`db.job_runs`**
- A job marked run reports `has_run` true for that period and false for the
  next (happy path).
- Marking the same pair twice keeps the first `ran_at` (proves the record is
  *when it first completed*, which is what makes a late run distinguishable
  from a repeated one).
- `last_run` on a job that has never run returns `None` (proves "did the weekly
  report go out?" is answerable, including when the answer is no).
- A naive `ran_at` raises `ValueError`.
- The module calls `aiosqlite.connect` nowhere and issues no `BEGIN`, `commit`
  or `rollback`.

**`db.trading_days`**
- A window recorded then read back returns the same days, oldest first (happy
  path).
- Recording a day twice keeps the **later** observation, including a day that
  changes from trading to not (proves a holiday announced after the fact
  replaces the earlier answer — the one table here that is not append-only).
- `earliest()` on an empty table returns `None`, not a date (proves the caller
  can distinguish "no history" from "history starting today").
- A window containing a day with no date records the rest and skips it (proves
  a malformed entry cannot take the whole window down).
- The module calls `aiosqlite.connect` nowhere and issues no `BEGIN`, `commit`
  or `rollback` (proves it runs on the shared connection inside `transaction()`).

**`market.session`**
- `refresh` writes every day of the window it received, not only today (proves
  the fourteen-day property the design rests on: one run covers the next
  fortnight, so an outage shorter than that leaves no gap).
- `calendar()` after two refreshes on different days spans **both**, including
  the earlier day now in the past (proves the past is remembered, since it
  cannot be fetched — §2.1).
- A position entered 7 trading days ago reports 7 from
  `clock.trading_days_between` against the calendar `refresh` actually builds,
  not one the test constructed to span the query (proves the seam where #45
  lived while both sides passed their own tests).
- `covers()` is `False` for a day before the earliest record and `True` for one
  after (proves an unmeasurable age is detectable, which is the whole difference
  between this and a silent undercount).
- A history write failing with `aiosqlite.Error` leaves the schedule cached and
  the bot trading (proves degraded age counting does not stop the market session
  working).
- A history write failing with `AttributeError` propagates out of `refresh`
  (v1.59; proves a rename cannot disguise itself as an unmeasurable calendar —
  the catch is exactly as broad as the rule it serves, not broader).
- A refresh that fails, then succeeds, then fails again alerts **twice** (proves
  the latch is per incident: it was set once and never cleared, so every outage
  after the first was silent from this module for the life of the process).
- `calendar()` returns the cached sessions as a `TradingCalendar`, and an empty
  one when the cache is empty rather than `None` (proves the caller has a total
  answer and never has to fetch its own).
- A timestamp inside the main session reports open (happy path).
- Exactly at the session open instant reports open; exactly at the close instant
  reports closed (boundary, proves inclusivity at both ends).
- A Saturday, and a scheduled market holiday, report closed (proves the calendar
  is consulted, not the weekday).
- With the schedule unavailable, reports closed and raises no exception (proves
  the safe default is to not trade).
- With `now` past the last cached session, `is_open` is False **and**
  `cache_exhausted` is True (proves an exhausted calendar is distinguishable
  from a closed market — the difference between a bot resting and a bot that
  has silently stopped trading).
- With an **empty** cache, `cache_exhausted` is True (proves the worst case is
  detected: a cache that never filled, not one that expired).
- `refresh` receiving a schedule with no trading sessions leaves a previously
  populated cache intact and alerts (proves an empty response cannot overwrite
  a good calendar).
- After a rollover refresh, `cache_exhausted` is False again (proves the cadence
  actually reloads).
- A successful refresh of a trading day emits `session_open` with `trade_date`,
  `opens_at`, `closes_at`; a successful refresh of a holiday emits
  `session_closed` (v1.61).

**`market.data`**
- Candles for a watchlist ticker are returned newest-last, timezone-aware
  (happy path and ordering contract).
- Fewer candles available than the longest strategy lookback returns what exists
  and the caller can detect insufficiency (proves partial history is visible, not
  silently padded).
- A ticker returning **no** candles is counted as a degraded call and alerts on
  the third, exactly as a failed fetch does (proves an empty success is not
  mistaken for health — it is the one result that guarantees no strategy can
  evaluate the ticker).
- A ticker returning fewer candles than the requested `lookback` alerts on the
  third consecutive call, and the alert names both numbers (proves a short
  history is reported rather than tolerated forever in silence).
- The short series is still returned to the caller (proves reporting
  insufficiency did not become dropping the ticker: a strategy whose own
  lookback the series does satisfy must still see it).
- Two failed calls followed by a short one alert on the third (proves a failure
  and a shortfall share **one** counter — they are one degradation of one
  ticker, and counting them separately would let a ticker alternate between
  them forever without ever crossing a threshold).
- A ticker whose fetch raises a **broker** failure while others succeed does not
  fail the batch (proves one bad instrument cannot blind the bot to the rest).
- A ticker failing three consecutive calls alerts exactly once, and a fourth
  failure adds no second alert (proves the threshold and the latch together).
- A ticker that recovers and then fails three more times alerts a **second**
  time (proves recovery re-arms the latch; an alert that fires once per process
  and never again is what #32 and #48 both are).
- Two tickers crossing the threshold in the same call produce **one** alert
  naming both (proves the call, not the ticker, is the unit — a watchlist-wide
  outage must not send one message per instrument).
- A ticker whose fetch raises a non-broker exception propagates it rather than
  omitting the ticker (proves a programming error is not disguised as a missing
  instrument, which is the exposure `broker.client`'s narrowing exists to
  create and this module was swallowing).
- An omitted ticker emits `candles_failed` with that `ticker` and `error`
  (v1.61).

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
- `build_features` returns values in `FEATURE_NAMES` order, and raises
  `ValueError` on fewer candles than `lookback` (proves the shared contract that
  training depends on).
- With `ML_MODEL_PATH` unset, the strategy is absent from the registry (proves
  disabled-by-default).
- A missing or unreadable model file raises `ModelLoadError` at startup, not at
  first signal (proves failure is loud and early).
- A model whose feature contract does not match the expected names and order
  raises `ModelContractError` (proves a stale model cannot silently mispredict).
- A prediction below the confidence threshold returns `None` (boundary).

**`risk.sizing`**
- With `open_cost` leaving less headroom than one lot, returns 0 (proves the
  portfolio ceiling binds — the control that replaced a per-position cap which
  could not, #15/#16).
- The reserve is honoured: with cash exactly equal to one lot and a non-zero
  `reserve_pct`, returns 0 rather than an order the broker would refuse.
- `lots × lot_size × price ≤ allocated − open_cost` holds for every input
  (property, not example).
- A standard case returns whole lots at or below the configured percentage
  (happy path).
- A price so high that one lot exceeds the position cap returns zero lots
  (proves the expensive-instrument path, which would otherwise over-allocate).
- Available cash below the cost of one lot returns zero lots (proves cash is
  respected independently of the percentage).
- Rounding is always downward: a budget worth 2.9 lots returns 2 (proves the
  intended direction of error).
- The returned lot count multiplied by lot size and price never exceeds
  `allocated − open_cost` for any input (proves the ceiling is structural, as a
  property over the input space rather than an example).
- Integer division truncates rather than dividing then rounding down. At the
  default decimal context `8.999…9 / 3` evaluates to exactly `3`, so dividing
  first would return three lots costing 9 against 8.999…9 of headroom — one lot
  of real money above the ceiling the function exists to enforce.
- `position_budget` returns `size_pct%` of `allocated` exactly, in `Decimal`
  (proves the formula, and that it is not routed through `float`).
- `size_position`'s budget bound and `position_budget` agree for the same inputs
  (proves the single definition — the seam this extraction exists to close,
  asserted on the caller's output rather than on the callee's, per failure
  class 6).

**`risk.gate`**
- Open positions whose summed cost leaves less than one lot of headroom reject
  with `PORTFOLIO_EXPOSURE` (proves the runtime exposure ceiling exists; before
  v1.30 only a configuration-time bound did).
- `PORTFOLIO_EXPOSURE` is evaluated after `INSUFFICIENT_CASH` and before
  `ZERO_LOTS` (proves the fixed priority, so the recorded reason is
  deterministic).
- No test constructs a `Config` the loader would refuse. The removed
  `POSITION_CAP` case did exactly that — it set `position_size_pct=50` against
  `max_position_pct=20`, a state `config.load()` rejects — so a green test
  asserted behaviour the assembled system could not produce (#15).
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
- A recovered exit fill closes the position with the trigger from its order row —
  a recovered stop-out is recorded as `STOP_LOSS`, not as `TAKE_PROFIT` (proves
  recovery reads the reason instead of defaulting, the defect that would
  otherwise silently corrupt every exit statistic in the weekly report).
- A recovered exit fill whose order row carries no trigger alerts and leaves the
  position open (proves a data defect is surfaced rather than guessed past).
- A recovered entry fill with no matching signal opens the position with strategy
  `UNATTRIBUTED`, and `ma_crossover`'s weekly figures are unchanged by it (proves
  the systematic bias is gone — asserted on the report, which is where the harm
  landed, rather than on the row).
- An entry order created at 23:58 MSK and recovered at 00:05 MSK finds its signal
  (proves the lookup follows the order rather than the calendar).
- A rejected entry records the rejection and opens no position, and is not
  retried (proves entry rejections are terminal).
- A rejected **exit** is retried on the following cycle and alerts immediately
  (proves the documented exception).
- Two concurrent entry attempts for the same ticker result in one order (proves
  the per-ticker lock).
- The order lock is released when the broker call raises (proves the release
  guarantee under failure, not only on success).
- A successful entry emits `order_submitting` then `order_filled` then
  `position_opened` then `stop_order_placed`, each with the §7.1 fields
  (v1.61).
- A rejected entry emits `order_rejected` and no `position_opened`.
- A `LOCAL` degrade after three stop failures emits `stop_protection_degraded`.

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
- `evaluate` with `trading_days_open = None` never returns `MAX_AGE`, and still
  returns `STOP_LOSS` and `TAKE_PROFIT` normally (proves an unmeasured age
  suppresses exactly one trigger, and that a short count can no longer read as a
  young position — the silent shape of #45).
- An unmeasurable age alerts once naming the count, stays silent on a second
  such cycle, and alerts **again** after a cycle in which every position was
  measurable (proves the latch re-arms per incident — it fired once per process,
  which is #32 in a second module).
- A cycle with one measurable and one unmeasurable position does not re-arm the
  latch (proves the whole cycle is the unit, so one covered position cannot
  clear a warning another still needs).
- A `LOCAL` position returns `STOP_LOSS` from `lifecycle.exits`; an `EXCHANGE`
  position never does (proves the trigger has exactly one owner — the test that
  prevents selling a position twice).
- `close_position(position, STOP_LOSS)` on a **`LOCAL`** position submits a market
  sell and closes it (proves the bot can act on the stop it owns — the path that
  makes the `LOCAL` degrade of rule 23 real protection rather than a label).
- `close_position(position, STOP_LOSS)` on an **`EXCHANGE`** position raises
  `ValueError` and submits nothing (proves the bot cannot sell out from under a
  stop the exchange owns).
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
- A `post_market_order` returning `SUBMITTED` with 2 of 3 lots filled cancels the
  order and re-reads it with `get_order_state`; the position is opened from the
  **re-read**, not from the response that came back alongside the cancel (proves
  the number written down is the broker's settled one, per rule 33).
- The re-read showing 2 filled opens a position of 2 with stop and target from
  the achieved price, and places a stop for 2 (proves sizing follows the fill,
  and that the stop quantity matches what is actually held — the mismatch #10
  named).
- No follow-up buy is submitted for the abandoned remainder (proves it is
  cancelled, not chased).
- A partial entry alerts (proves the liquidity signal reaches the owner).
- `cancel_order` raising `BrokerUnavailable` leaves the order unresolved, opens
  no position and writes nothing (proves an unconfirmed outcome is never written
  down, and that the recovery path — not a guess — is what resolves it).
- A `SUBMITTED` entry with **zero** lots filled is not cancelled (proves a merely
  pending market order is not converted into a missed entry).
- A cooldown write failing with `aiosqlite.Error` during `close_position` leaves
  the position `CLOSED`, returns it, and halts trading with an alert rather than
  raising (v1.63; proves a completed exit is never reported as failed, which
  would retry a sell the account cannot cover).
- `close_position` submits exactly one sell order and, when the broker reports a
  partial, raises `ExitFailed` having submitted nothing further (proves the
  slicing loop is gone, and with it the unbounded submission it allowed).
- `resolve_unfinished` settling an `EXIT` order `CANCELLED` with 2 of 5 lots
  filled calls `update_lots(3)`, leaves the position **open**, and alerts (proves
  the book and the account agree on quantity, and that a half-exited position is
  never closed at a price for lots it still holds).
- That same case writes a `LOTS_ADJUSTED` event and no `CLOSED` event (proves the
  unbooked slice is recorded rather than silent).
- `resolve_unfinished` settling an `EXIT` order whose `filled_lots` reaches the
  position's count closes it (proves a cancel landing after a full fill still
  books the exit).

Additionally, on exits booked from an exchange stop:
- `close_executed_stop` records the **broker's** executed price, not the price
  `get_last_price` would return at that moment: a fill at 95.00 while the quote
  says 92.00 records 95.00 (proves the invented-price path is closed — this is
  #4's own verification case).
- `close_executed_stop` with a `fill` whose `filled_price` is `None` raises
  rather than substituting any other number.
- `close_executed_stop` records `fill.key` as the order row's `broker_order_id`
  (proves the row the exchange's execution is filed under can be re-queried —
  without it, `get_order_state` on the bot's invented UUID can only ever return
  `OrderNotFound`).
- `close_executed_stop` with a `fill` whose `commission` is `None` still closes
  the position (proves the commission is a correction, not the substance: a stop
  exit left open would be found by reconciliation and filed as `EXTERNAL`, which
  is the wrong trigger recorded permanently).
- The exit commission on the closed position is the broker's
  `executed_commission`, never zero and never estimated.

**`ops.commissions`**
- A stop-exit row carrying a `broker_order_id` is re-queried through
  `get_order_state_by_broker_id`, and the commission that comes back is written
  and the closed position's `realised_pnl` recomputed (proves the money half of
  #8: the fee on an exchange-fired stop is recoverable at all).
- A row with no `broker_order_id` is still re-queried by `key` (proves the
  ordinary path is unchanged).
- A row whose commission stays unknown past 24 hours alerts on the first run and
  **not** on the second (proves the alert terminates — it fired on every backfill
  run, daily and before every weekly report, once per stop-loss exit ever taken).
- That same row is still re-queried on the second run (proves the terminal state
  is on the telling, not the trying).

**`state.halt`**
- Halting then reading state reports halted with its reason (happy path).
- Halt state survives a simulated restart, where a restart is
  `db.connection.disconnect()` followed by `connect` to the same file — a
  connection left open in-process is not a restart (proves persistence: a crash
  must never be a way to resume trading).
- The module calls `aiosqlite.connect` nowhere (proves it runs on the shared
  connection; `state/halt.py` was one of the eight sites opening its own).
- Resuming clears the halt and records who cleared it (proves auditability).
- Resuming when not halted is accepted and changes nothing (proves idempotency).
- A `DAILY_LOSS_LIMIT` halt arriving during a `MANUAL` halt **replaces** it,
  rewrites the detail and alerts; a `MANUAL` halt arriving during a
  `DAILY_LOSS_LIMIT` halt changes nothing (proves severity ordering — the more
  serious reason was previously discarded, #9).
- A halt does not prevent `lifecycle.exits` from returning triggers, nor
  `execution.orders` from placing an exit (proves the halt-blocks-entries-only
  contract, which is the single most consequential interaction in the system).
- `halt` emits `halt_triggered` with `reason`, `detail`, `daily_loss_pct`;
  `resume` of a halted process emits `halt_cleared` with `actor` (v1.61).

**`pnl`**
- Realised P&L for a closed position matches the arithmetic including commission
  (happy path).
- Unrealised P&L for an open position uses the current price (happy path).
- The daily loss percentage divides by `ALLOCATED_CAPITAL`, not by account
  equity: the same rouble loss on an account holding twice the allocation gives
  the same percentage (proves the limit means a share of the money at risk, #9).
- A cash withdrawal between two calls does not change the daily loss percentage
  (proves broker equity is never read, so a transfer cannot read as a trading
  result — the failure that could halt trading for moving money).
- With no snapshot for the day, the baseline is reconstructed from realised P&L
  before today and the reconstruction is alerted (proves a mid-session start is
  not silently blind to the morning).
- `bot_equity` counts allocated capital plus realised plus unrealised, and is
  unchanged by a deposit.
- With no positions and no trades, all figures are zero rather than `None`
  (proves the empty-portfolio path).
- The buy-and-hold benchmark over a window with a missing price for one
  instrument reports the benchmark as unavailable rather than as zero (proves
  missing data is not silently treated as no return).

**`telegram.commands`**
- Each command from the authorised chat returns its documented content (happy
  path per command).
- Any command from an unauthorised chat identifier returns nothing, emits
  `unauthorised_command` with `chat_id` and `command`, and performs no state
  change (v1.61; proves the single security boundary).
- `/resume` when not halted replies that nothing was halted (proves the
  no-op path).
- A response exceeding the message limit is truncated with an explicit note
  naming how many entries were omitted (proves the truncation contract).
- No command mutates a risk limit (proves the brief's prohibition).

**`telegram.notifier`**
- A failed send is retried and, if still failing, emits `telegram_send_failed`
  without raising (v1.61; proves Telegram outages never reach trading logic).
- A body containing a token is dropped, emits `secret_redacted` with `sink`
  `telegram`, and does not contain the token (v1.61).
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
- A successful send emits `weekly_report_sent` with `period_start` and
  `period_end` (v1.61).

**`app.startup`**
- Startup with valid config, a reachable broker and a clean database completes
  and reports ready (happy path).
- Invalid config aborts before any broker call is made, emits `config_invalid`
  with `variable`, and does not emit `startup_ok` (v1.61).
- `SSL_TBANK_VERIFY` is present in the environment before the first broker call
  (proves the TLS root is available when the channel is built — the failure this
  guards against is a handshake error that looks like a network fault rather
  than a configuration one).
- Starting with `ssl_tbank_verify` false alerts before any broker call, and the
  alert contains no token (proves running without certificate verification is
  something the owner is told about rather than something buried in a log).
- An unresolved order from a previous run is resolved before the first strategy
  evaluation (proves recovery precedes trading — the ordering that prevents a
  duplicate order).
- Reconciliation runs before the first entry is permitted (proves the same for
  position truth).
- A halted-at-shutdown bot starts halted (proves halt persistence end to end).
- A `STOP_DUPLICATE` adjustment causes every identifier in `cancel` to be
  cancelled and `keep` to be retained (proves the remedy is applied — it was
  reported and dropped, and every test still passed).
- A report containing **only** a `STOP_DUPLICATE` still applies it (proves the
  remedy gate does not skip a report that carries no other stop adjustment).
- An adjustment type the executor does not recognise alerts rather than being
  ignored (proves a report it cannot act on is loud).
- An `EXIT_UNRESOLVED` adjustment does **not** raise that alert and does not stop
  startup (proves the observed-not-remedied set includes it, so the loud path
  stays reserved for a report the executor genuinely does not understand).
- A `FOREIGN_HOLDING` adjustment raises `StartupError` naming the ticker, and no
  entry is attempted (proves the account-exclusivity policy is enforced rather
  than documented).
- With `allow_foreign_holdings` true, startup completes, the ready alert names
  the holding, and no stop is placed and no exit submitted for it (proves the
  acknowledged path is observe-only).
- `start` calls `db.connection.connect` **before** `db.migrations.apply`, and
  `apply` receives `db.connection.shared()` (proves the connection is opened by
  startup rather than at import or inside a repository).
- Importing `app.startup` opens no database file.
- With a budget below every watchlist lot cost, startup **completes** and the
  owner is alerted that nothing is affordable (proves the silent permanent
  `ZERO_LOTS` condition is reported, and — the half that matters — that
  reporting it does not stop the bot protecting open positions).
- With one affordable instrument and one not, no blackout alert is raised and the
  ready alert names the unaffordable ticker (proves a partially reachable
  watchlist is normal and does not burn the alert channel).
- A ticker whose price cannot be read is named as unknown and is counted neither
  affordable nor unaffordable (proves missing data is not silently read as either
  answer).
- When no price can be read for any ticker, the check reports itself
  inconclusive rather than reporting a blackout (proves the check cannot
  manufacture the very finding it exists to detect out of a broker outage).
- A broker failure in step 8a does not raise `StartupError` (proves a diagnostic
  cannot become the reason the bot is down).
- A successful start emits one `startup_ok` log record whose fields are
  `version`, `mode`, `halted` and `adjustments_count`, matching what the ready
  alert reports (proves the deploy health gate has something to observe — it
  greps for exactly this event, and nothing emitted it).
- A `FOREIGN_HOLDING` refusal emits `startup_failed` with `stage` `reconcile`
  and no `startup_ok` (v1.61).

**`app.loops` / `app.shutdown`**
- With the session closed, no market data call is made (proves the session guard
  gates the loop).
- A position whose stop is **absent from the active list and whose ticker is
  absent from the portfolio**, with no confirmed execution, stays `OPEN`, submits
  nothing, starts no cooldown, and alerts once (proves an exit is never inferred
  from two eventually-consistent absences — the false positive that fabricated a
  round trip, #5).
- A position is closed when `get_executed_stop_fills` contains the
  `stop_order_id` persisted in its `stop_orders` row, and the exit price recorded
  is the one that result carries (proves execution is confirmed and priced from
  the broker).
- Matching is on the persisted broker `stop_order_id`, not on the locally
  generated key: a stop whose broker-side key differs from our UUID is still
  matched (proves the key mismatch that made a live stop look dead cannot recur).
- Re-running the cycle after a position has been closed this way does not
  reconsider it (proves the re-queried window is idempotent).
- One position's price raising `PriceRejected` leaves the other positions
  evaluated normally, submits no exit for the rejected one, and does not
  increment the outage counter (proves one bad quote cannot abort a cycle or
  masquerade as a broker failure).
- Every price rejected in a cycle produces exactly one alert naming the count
  (proves a correlated failure is reported as one event, not as N).
- A second consecutive cycle with rejections produces **no** further alert, and a
  cycle with none re-arms it (proves the latch — the difference between a
  monitoring channel the owner reads and one they mute).
- Two strategies signalling the same ticker in one pass open **one** position,
  record the second as `DUPLICATE_TICKER`, raise no crash alert, and still
  evaluate the remaining tickers (proves a correct refusal is an ordinary
  outcome — it reached the supervisor, alerted "Background task trading
  crashed", and abandoned the rest of the pass).
- `open_position` raising `PositionStateError` or `DuplicateOrderError` is
  caught and the pass continues (proves both siblings of `OrderRejected` are
  handled, not just the one that had a branch).
- A `BrokerRateLimited` whose `retry_after` exceeds the escalating back-off
  delays the next cycle by the **hint** (proves the broker's own number is used
  at all: it was computed, asserted at the raise site, and read by nothing).
- A hint **shorter** than the escalation leaves the escalation unchanged (proves
  the hint raises the floor and never lowers it — a two-second hint must not
  undo a back-off five failed cycles deep).
- A hint beyond the maximum back-off is capped at it (proves no number from
  outside can hold the exit path asleep).
- A successful cycle clears the hint, so a later failure that carries none is
  delayed by the escalation alone (proves the same reset discipline the failure
  counter has; a remembered hint is stale state shaped like a measurement).
- Three consecutive rate-limited cycles alert **once**, and the alert names
  throttling rather than a market-data outage (proves the cause reaches the
  owner, instead of a bad field reading as weather — #6 and #23's complaint).
- A cycle issues **zero** `get_trading_schedule` calls, and the calendar used for
  `MAX_AGE` is the one `market.session` holds (proves the fourteen-day schedule
  is no longer re-fetched once a minute).
- A process restarted after the Sunday 12:00–12:59 MSK hour still sends that
  week's report, once (proves the skip is gone — the exact-hour condition lost
  the week with no report, no alert and no record, against acceptance criterion
  9).
- Three restarts in one Moscow day produce **one** heartbeat and one rollover,
  not three (proves schedule state survives a restart, which is the ordinary
  case rather than an edge one).
- A job already marked run for its period does not run again on the next tick
  (proves the guard is the record, not the module global that a restart cleared).
- After `stop_entries()`, a cycle evaluates no entries and still submits exits
  (proves shutdown closes the entry window without blocking the closes it exists
  to settle — the half of the contract that was written and never implemented).
- `shutdown` calls `stop_entries` **before** it drains (proves the ordering: a
  drain that runs first has already looked past the position the next cycle
  opens).
- `run` starts the Telegram command listener, and a `/halt` sent afterwards
  halts trading (proves the kill switch exists at runtime — the acceptance
  criterion that a defined-but-uncalled listener left unmeetable while every
  unit test passed).
- An exhausted schedule cache alerts rather than quietly reporting closed
  (proves silent non-trading is detected).
- A shutdown signal during an in-flight order submission waits for a known state
  before exiting (proves the graceful-shutdown contract).
- Shutdown neither cancels nor liquidates positions (proves restarts have no
  financial consequence).
- `shutdown` calls `db.connection.disconnect()`, and `db.connection.shared()`
  raises `DatabaseNotOpenError` afterwards (proves "closes the database" is that
  one call rather than a repository-level close of a connection nobody owns).
- An approved signal emits `signal_generated` before the gate; a rejected
  decision emits `signal_rejected` with `rejection_reason`; `risk.gate` emits
  neither (v1.61).
- A successful heartbeat job emits `heartbeat` with `uptime_seconds`,
  `open_positions`, `halted`.
- A crashing supervised task emits `task_crashed` with `task` and
  `restart_in_seconds`.

**`ops.backup`**
- A backup produces a file that opens as a valid database containing the same
  rows (proves the copy is consistent, not a torn file).
- Backups older than the retention window are removed and newer ones are kept
  (boundary).
- A failing backup alerts, emits `backup_failed`, and does not stop trading
  (v1.61).
- A successful backup emits `backup_ok` with `path` and `bytes`.

**`sandbox.exchange`**
- A market buy is **priced at the next bar's open**, never at the bar the
  decision was made on (proves look-ahead is absent: the price was not knowable
  when the order was placed).
- That fill is returned on the submitting call, not deferred (proves the
  control flow matches live — a deferred fill sends every entry through
  `open_position`'s crash-recovery path and trips the outage counter, which is
  precisely the live/backtest divergence this rebuild removes).
- An order placed on the last bar of history stays `SUBMITTED` and never fills
  (proves history running out is not filled in with an invented price).
- A bar whose **low** is below the stop triggers `STOP_LOSS`, even when its
  close is above it (proves the optimism is gone: on daily bars this is the
  difference between a 5% stop that fires and one that never does).
- A bar whose **high** reaches the target triggers `TAKE_PROFIT` on the same
  rule.
- A bar that gaps **through** the stop fills at its open, not at the stop price
  (proves the exchange cannot fill where the market never traded).
- A bar touching both stop and target books the **stop** (proves the pessimistic
  tie-break, which daily bars cannot resolve any other way).
- A buy for more than the simulated cash raises `OrderRejected` and leaves cash
  and holdings unchanged (proves the double refuses what the broker refuses, and
  that a backtest cannot fund a position the account could not).
- `get_portfolio()` returns without raising after any sequence of fills (proves
  the negative-cash crash is closed at its cause rather than at its symptom).
- `advance(moment, phase)` makes `get_last_price` report that phase of the
  current bar, and its close by default (proves the sub-bar marks reach the code
  that values a position).
- Two instruments advanced to the same phase each report **their own** bar's
  value (proves the phase is resolved per instrument — a scalar price passed in
  by the caller would report one ticker's low as every ticker's).
- A standing stop is checked once per bar however many marks are walked (proves
  four cycles are not four chances to fire).
- Commission is a percentage of turnover with a minimum, charged once per fill
  (proves the tariff shape, and that a round trip is not charged twice for one
  leg).
- Every function it exposes raises the same exception type as its
  `broker.client` counterpart for the same condition (proves it is a double, not
  an approximation — a simulator that cannot fail the way the broker fails
  cannot exercise the code that handles failure).

**`sandbox.backtest`**
- The result is produced by running `app.loops.trading_cycle`, and a run with
  the gate rejecting everything opens no position (proves the gate is in the
  path at all — it was absent entirely, and its absence read as compliance with
  the no-reimplementation rule).
- A whole run sends no real alert, reads no unpatched clock and loads no
  configuration from the environment (proves the guard covers every class of
  escape the seam table covers, not only the broker — the omission that let a
  backtest page the owner from a laptop).
- A bar whose **low** breaches the daily loss limit halts the run, and no entry
  is opened afterwards that day (proves the limit is reachable at all — it could
  not fire, so every result silently assumed it never would).
- A position held past `MAX_HOLDING_DAYS` exits with `MAX_AGE` (proves the
  closing-window mark: with one cycle at the session start, twenty flat bars and
  `max_holding_days=1` produced zero exits).
- A `LOCAL` position whose bar low breaches its stop exits `STOP_LOSS`, on a bar
  that closes above it (proves the close-only optimism is gone from the path
  where the bot owns the stop, not only from the exchange's).
- The day's opening snapshot is written **once** per Moscow date across the four
  cycles (proves the baseline stays the open, which is what makes the intra-day
  loss meaningful).
- A cooldown, a `MAX_POSITIONS` limit and a duplicate ticker each block an entry
  in the backtest exactly as live (proves the whole gate, not a re-derived
  subset).
- Two simultaneous signals with cash for only one → the second is rejected
  (proves capital contention is modelled, which one ticker and one position made
  impossible).
- `max_drawdown` reflects an open position moving against the book, on a run
  with no closed trades at all (proves equity is marked to market rather than
  read off the cash balance, where the number was approximately the position
  size).
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
- `RejectionReason` — `HALTED`, `SESSION_CLOSED`, `INSTRUMENT_NOT_TRADING`, `DUPLICATE_TICKER`, `MAX_POSITIONS`, `COOLDOWN_ACTIVE`, `INSUFFICIENT_CASH`, `ZERO_LOTS`, `PORTFOLIO_EXPOSURE`, `BROKER_LOT_LIMIT`
- `HaltReason` — `DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`
- `StopOrderStatus` — `PLACING`, `ACTIVE`, `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`
- `StopProtection` — `EXCHANGE`, `LOCAL`. Which side owns a position's stop trigger

`POSITION_CAP` was removed in v1.30 and `PORTFOLIO_EXPOSURE` takes its place.
The old reason was unreachable: `config.load()` refused any configuration where
`POSITION_SIZE_PCT` exceeded `MAX_POSITION_PCT`, so the per-position cap was
never the binding minimum and no order could ever be rejected for it — while the
risk summary the bot shows on `/resume` listed it as an active control (#15). The
new reason can bind, because the ceiling it enforces is on the **portfolio**, and
the portfolio grows independently of any one order's size. The `signals` table
does not enumerate rejection reasons in a CHECK constraint, so no migration is
required; a value the schema would still accept but no code can produce is inert.

`BROKER_LOT_LIMIT` covers the broker refusing the size outright — its maximum
for the account is zero lots. It is distinct from `ZERO_LOTS`, which means our
own sizing arithmetic produced nothing affordable; the two have different causes
and only separate reasons make the rejection log diagnostic.

**Frozen dataclasses** — `Candle`, `Instrument`, `Signal`, `Position`,
`OrderRecord`, `StopOrderRecord`, `OperationRecord`, `PortfolioState`,
`SessionInfo`, `RiskDecision`, `HaltState`, `ReconciliationReport`,
`TradingCalendar`, `BacktestResult`.

`OrderRecord` carries `broker_order_id` and `commission_alerted_at` (v1.39).
`key` is the bot's own idempotency key, and for a row describing an execution the
**exchange** performed — a stop the broker fired on the bot's behalf — the broker
has never seen that key, so nothing could ever re-query the row. Its commission
was therefore unrecoverable if it landed late, and the "commission still unknown"
alert repeated on every backfill run, forever, once per stop-loss exit ever taken
(#8). `broker_order_id` is the broker's own identifier for the order where the
bot knows it; `commission_alerted_at` records that the owner has been told once.
Both are `None` where they do not apply. `commission_alerted_at` is
notification bookkeeping rather than a trading fact, and it lives on the row
anyway because an alert that repeats forever is equivalent to no alert, and
"have I already said this" is a fact about the row that must survive a restart.

`OperationRecord` carries `operation_type` and `state` as well as the broker's
actual commission (v1.35). It used to carry neither, so the only way to tell a
sale from a purchase was the sign of `payment`, and the only way to find a fee
was to look for the substring `FEE` in a name the dataclass did not expose. Both
are now explicit and both come from the broker verbatim: `operation_type` is the
`OperationType` member's name (`OPERATION_TYPE_SELL`, `OPERATION_TYPE_BROKER_FEE`
and so on) and `state` is the `OperationState` member's name.
`broker.reconcile` has to identify one specific sale of one specific instrument
to book an external close at the price it actually happened at (#11), and the
sign of a payment is not a thing to build a money number on. `OperationRecord`
also carries `parent_operation_id`, which is how a fee is tied to the trade that
incurred it. `TradingCalendar` is
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
- Resolves `tinvest_token` and `tinvest_account_id` for the mode in force: when
  `TRADING_MODE` is `sandbox`, each is taken from its `*_SANDBOX` variable and
  falls back to the base variable when that is unset or blank. Live mode ignores
  the overrides. The rest of the codebase sees one token and one account id and
  never branches on mode — sandbox remains selected by endpoint alone.
- Adds `price_max_age_seconds` (default 120) and `price_max_move_pct` (default
  20), the bounds `broker.client` validates quotes against.
- **`MAX_POSITION_PCT` is removed** (v1.30), with its cross-field check against
  `POSITION_SIZE_PCT`. It could not bind: the check guaranteed
  `position_size_pct ≤ max_position_pct`, which made the cap unreachable in
  sizing while `/resume` reported it as an active limit (#15). `MAX_OPEN_POSITIONS
  × POSITION_SIZE_PCT ≤ 100` stays — it is a real configuration-time bound.
- Adds `cash_reserve_pct`, default **1**, the slice of cash `risk.sizing` holds
  back so fees and rounding cannot make an approved order unaffordable. Bounded
  0–50; a reserve above half of cash is a configuration error, not a preference.
- Adds `allow_foreign_holdings`, defaulting to **false**. The trading account is
  the bot's alone (brief v1.8); this flag is the owner's explicit acknowledgement
  that it is not, and it is deliberately awkward to set by accident. It is not a
  risk limit, so a missing value takes its default.
- `ssl_tbank_verify` defaults to true. **Setting it false must be loud**:
  `config.load()` logs a CRITICAL line naming the risk, because it disables
  certificate verification on the connection carrying the trading token. A
  security control that one environment variable can switch off silently is a
  control nobody can audit after the fact. `app.startup` raises the matching
  alert — see its own contract.
- Raises `ConfigError` naming the offending variable when: a required variable is
  missing or empty; a numeric value is out of range;
  `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT` exceeds 100; `CASH_RESERVE_PCT` is
  outside 0–50;
  `TAKE_PROFIT_PCT` is not greater than `STOP_LOSS_PCT`; `WATCHLIST` is empty;
  or `ML_MODEL_PATH` is set but unreadable.
- Must never substitute a default for a missing **risk** variable.
- Must never include a token value in an exception message or in `__repr__`.
- Called before any other module is initialised.

**`get() → Config`**
- Returns the process-wide `Config`, loading it once on first call and returning
  the same instance thereafter. `app.startup` calls `load()` first so a bad
  configuration fails before anything else; every later reader uses `get()`.
- Exists because `load()` re-reads every environment variable, re-parses every
  `Decimal` and stats `ML_MODEL_PATH` on each call, and `broker.client` was
  calling it three times per order on the latency-critical path (#18).

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
- **Emits the base record schema of §7.1 on every record (v1.60):**
  `timestamp` (UTC, ISO 8601), `moscow_time` (the same instant as
  `Europe/Moscow`, via `clock.to_moscow`), `level`, `logger` and `message`.
  Moscow time is derived here rather than by producers so that no caller can
  omit it and no caller needs a second timestamp argument.
- **`event` is producer-set and passes through unaltered (v1.60).** A record
  logged with `extra={"event": ...}` carries that field; a record logged without
  one carries no `event` key, and this module never invents a default. §7.1 said
  "all records carry … `event`", which is unsatisfiable for the records that
  third-party libraries emit through the same handler — `httpx`, `telegram.ext`
  and the broker SDK all log here and know nothing of the catalogue. The
  obligation to name an event belongs to each producing module's own contract,
  not to this one, and the absence of `event` is what marks a line as library
  noise rather than a domain event.
- **This module emits no domain event of its own.** It formats and redacts; it
  never originates a catalogued event. A `startup_ok` from here would be a
  fiction.
- The redaction filter applies to `event` as it does to every other structured
  field.
- Timestamps come from the `LogRecord`'s own `created` instant, not from
  `clock.now()` — this module is below `clock` in the dependency order and must
  not reach forward to it for the current time. `clock.to_moscow` is a pure
  conversion and is the one thing it does use.
- Called by `app.startup` immediately after `config.load()` and before any other
  module logs anything.
- **The `secrets` list must include every token and every account identifier the
  process holds** — `tinvest_token`, `tinvest_account_id`, and any sandbox
  counterparts that are set (v1.61). `app.startup` is the caller that knows
  those values; this module only redacts what it is given.

### `zarabot/db/migrations.py`

Owns schema creation and version tracking.

**`async apply(conn: Connection) → int`**
- Applies every migration whose version exceeds the database's recorded version,
  in ascending order, each in its own transaction.
- Returns the resulting schema version.
- Raises `MigrationError` if the recorded version exceeds the highest known
  migration, and makes no modification in that case.
- Idempotent: applying twice is a no-op the second time.
- **The one module exempt from rule 31.** It commits and rolls back the
  connection it is given, one transaction per migration file, because §6 requires
  each file to be applied atomically and because it runs before any other task
  exists. Every other module writes through `db.connection.transaction()`.
- Called by `app.startup` before any repository function, on
  `db.connection.shared()`. This module never calls `aiosqlite.connect` and never
  closes the connection it is given.
- At the start of `apply`, issues `PRAGMA foreign_keys = ON`,
  `PRAGMA busy_timeout = 30000` and `PRAGMA journal_mode = WAL` on that
  connection. `journal_mode` is persistent; the other two are per-connection and
  must be re-issued on every connection, which is why they appear both here and
  in `db.connection.connect`. Setting them here means a test that passes its own
  connection still gets WAL and foreign-key enforcement.
- **Ships `003_position_events.sql`**, which creates the `position_events` table
  that `db.positions` owns. The migration file belongs to this module even though
  the table belongs to that one: `migrations/` is this module's directory, and a
  table specified in §5 with no named file owner reaches no task at all.

### `zarabot/db/connection.py`

**Sole owner of the process-wide SQLite connection.** No other module calls
`aiosqlite.connect`. No other module closes the connection.

This module exists so every repository signature in `interfaces.md` can stay
exactly as recorded: callers keep calling `db.positions.open(...)` with no
connection argument. Threading a connection through every repository would change
every caller in the system. The lifecycle is already half-specified in
`app.startup` ("open the database") and `app.shutdown` ("closes the database");
this module is the named owner of the object those two sentences refer to.

**Must never connect at import.** `AGENTS.md` forbids module-level side effects
outside `config`. Connecting at import would violate that, and would make tests
inherit whichever file the previous importer happened to open.

**`async connect(path: str) → aiosqlite.Connection`**
- Opens the SQLite file at `path`, stores it as the process connection, and
  issues on that connection:
  - `PRAGMA journal_mode = WAL`
  - `PRAGMA foreign_keys = ON`
  - `PRAGMA busy_timeout = 30000`
- `foreign_keys` and `busy_timeout` are per-connection; `journal_mode` is
  persistent. All three are set here so that a forgotten per-connection pragma
  cannot silently disable integrity checking.
- Raises `DatabaseAlreadyOpenError` when a process connection is already open. A
  caller needing a different file must `disconnect` first.
- Returns the connection. Does not apply migrations — `app.startup` calls
  `db.migrations.apply(shared())` next.
- Called only by `app.startup` and by the test fixture below.

**`shared() → aiosqlite.Connection`**
- Returns the open process connection.
- Raises `DatabaseNotOpenError` when `connect` has not been called, or when
  `disconnect` already has.
- Must never open a connection as a side effect of being called. A silent
  reconnect would hide a missing `app.startup` step and would let a test inherit
  a file it did not create.

**`transaction(*, critical: bool = True) → async context manager yielding aiosqlite.Connection`**
- **The sole transaction owner.** Every write in the system runs inside it:
  `async with transaction() as conn:`. It holds one process-wide lock, issues
  `BEGIN IMMEDIATE`, commits on clean exit, and rolls back on exception.
- **Reentrant.** A nested acquisition on the same task joins the outer
  transaction instead of beginning a second one or deadlocking, and only the
  outermost exit commits. `broker.reconcile` calls `db.positions.adopt` and
  `db.positions.close` while doing work of its own, so nesting is the normal
  case, not an edge one.
- **No module may `BEGIN`, `commit` or `rollback` the shared connection
  itself.** A per-module `asyncio.Lock` was sufficient when every call opened its
  own connection; on one shared connection it serialises nothing, because the
  transaction lives on the connection rather than in the module. Two writers in
  different modules previously collided with `cannot start a transaction within a
  transaction`, and — worse — a bare `commit()` in any module made another
  module's in-flight rows durable, so its `rollback()` undid nothing. Both were
  reproduced on the money path (#40).
- A read needs no transaction and must not take one.
- **On `aiosqlite.Error` during a write, emit `db_write_failed` (v1.61)** with
  `table` (the object name when the error names one, otherwise `unknown`) and
  `critical`, then re-raise. This is the single owner of that event;
  repositories that swallow a rule-12 failure still go through this path so the
  event is not optional.
- **`critical` is passed by the caller (v1.64):
  `transaction(*, critical: bool = True)`.** The default is `true`, so a caller
  that says nothing is treated as trading-critical. Rule-12 callers —
  `db.signals`, `db.snapshots`, and the instruments cache when it writes through
  `transaction()` — pass `critical=False`. `db.cooldowns` passes `critical=True`
  (rule 11, v1.63). The exception still propagates out of `transaction()`; the
  repository's own catch is what stops it reaching the trading loop.
- **This replaces the table-derivation of v1.62, which did not work.** v1.61
  said `critical` true unconditionally; v1.62 tried to derive it by parsing the
  table out of the SQLite error text. Measured against real errors on a rule-12
  table, a UNIQUE or NOT NULL violation names the table and derives correctly,
  but `no such column` and `datatype mismatch` do not — and the failures that
  actually occur in production, disk full, `database is locked` and disk I/O
  error, name no table at all. Every one of those falls to `unknown` and reports
  `critical` true, which is the defect v1.62 was written to remove, surviving in
  exactly the cases most likely to happen. The derivation worked for errors a
  test constructs and failed for errors a disk produces.
- **The owner of a write knows which rule it is on; the error text only
  sometimes does.** That is why the value is passed rather than inferred. The
  cost is a keyword argument at the rule-12 call sites, which is small against a
  field an operator filters on to find writes that actually stopped trading.
- `table` is still derived from the error message, and is still `unknown` when
  the message names none. That was never in dispute — it is a label for a human
  reading the log, not a value anything branches on.
- **`cooldowns` is rule 11 (v1.63).** A lost cooldown row lets the bot re-enter
  a ticker it just exited, which is a trading consequence, not an analytics one.
  v1.62 left it unassigned pending this decision; it is now named in rule 11
  rather than reaching `critical` true by the unknown default.

**`async disconnect() → None`**
- Closes the process connection and forgets it. Idempotent when already closed.
- Called only by `app.shutdown` and by the fixture teardown.
- After it returns, `shared()` raises `DatabaseNotOpenError`.

**This module owns `tests/conftest.py`.** The fixture that connects a temporary
file, applies migrations, yields, and disconnects on teardown is defined once
there and used by every database test in the project. `tests/conftest.py` does
not exist today — each test file builds its own temporary database — and leaving
each task in this batch to invent its own fixture is a separate chance in each
one to leak a process connection between test files.

Enabling `foreign_keys` is cheap now: the live database holds zero rows, so
turning enforcement on cannot surface an existing violation. This is the cheapest
moment in the project's life to do it.

### `zarabot/db/positions.py`

**Sole owner of position row mutation.** No other module writes these rows.
**Sole owner of `position_events`.** No other module writes that table.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async open(signal: Signal, order: OrderRecord, instrument: Instrument, stop: Decimal, target: Decimal, opened_at: datetime) → Position`**
- Inserts an open position and returns it with its assigned identifier.
- Inserts with `stop_protection = LOCAL` **always**. The position row is created
  before the standing stop order exists, and for that window the bot itself is
  the only thing watching the stop. Defaulting to `LOCAL` means the position is
  never recorded as protected by something that has not been confirmed to exist;
  the failure direction is a redundant local check, not an unwatched position.
- Raises `PositionStateError` if an open position already exists for the ticker.
- **Only the duplicate-open-position violation becomes `PositionStateError`
  (v1.38).** Both `open` and `adopt` translated *any* `IntegrityError` into
  "open position already exists", on the assumption that the partial unique index
  was the only integrity constraint reachable. That assumption was false, and the
  broad translation turned a foreign-key violation into a plausible-sounding lie
  naming a position that did not exist — failure class 5 in `ops/STATE.md`, and
  what made #42 take a reproduction to diagnose rather than a stack trace. Every
  other integrity failure propagates as itself, cause intact.
- In the same transaction, writes one `position_events` row with
  `event = 'OPENED'`. `occurred_at` is `clock.now()`.

**`async set_stop_protection(position_id: int, protection: StopProtection, stop_order_key: str | None) → Position`**
- Promotes a position to `EXCHANGE` once its standing stop is confirmed active,
  or returns it to `LOCAL` when that stop is cancelled, executed, or found
  missing.
- Raises `PositionStateError` when `EXCHANGE` is requested without a key, or
  `LOCAL` with one — the pairing is the invariant that prevents both owners
  acting on the same position.
- Called only by `execution.orders` and by the startup remediation step.
- In the same transaction as the row update, writes one `position_events` row
  with `event = 'STOP_PROTECTION_CHANGED'` and `detail` naming the previous and
  new protection and stop-order key.

**`async close(position_id: int, trigger: ExitTrigger, exit_price: Decimal, closed_at: datetime, order: OrderRecord | None, exit_commission: Decimal | None = None) → Position`**
- Transitions a position to closed, recording the trigger, exit price, realised
  P&L and the closing order.
- Realised P&L is `(exit − entry) × lots × lot_size` **minus commission on both
  legs** — the opening order's and the closing order's. The opening commission is
  obtained by calling `db.orders.get(open_order_key)`, never by querying the
  `orders` table. A missing row, or a row whose commission is not yet known,
  contributes zero. Netting only the closing
  leg overstates every realised result by the entry commission, permanently and
  invisibly. On an account this size, commission on a round trip is a meaningful
  fraction of a 10% move.
- Also clears `stop_protection` to `LOCAL` and `stop_order_key` to null, since a
  closed position owns no stop. This is recorded here because it is a mutation a
  caller would otherwise not expect.
- `order` is `None` **only** when `trigger` is `EXTERNAL` — a position that
  disappeared at the broker was not closed by an order of ours, and there is
  nothing to record. Any other trigger with `order = None` raises `ValueError`,
  as does `EXTERNAL` **with** an order.
- **`exit_commission` is the closing leg's commission when there is no closing
  order to read it from (v1.35)**, which is exactly and only the `EXTERNAL` case.
  Because `order` was `None` there, the closing commission was zero, and every
  externally closed position overstated its realised result by the broker's fee —
  permanently, since nothing later corrects it. `broker.reconcile` now resolves
  the fee from the operations feed and passes it here. Supplying it with any
  other trigger raises `ValueError`: everywhere else the commission is on the
  order row, and a second source for the same number is a way for the two to
  disagree. `None` with `EXTERNAL` is permitted and means the fee could not be
  resolved; it contributes zero, as an unknown commission always has.
- The value is **stored** in `positions.exit_commission`, not merely folded into
  `realised_pnl`. A realised figure whose inputs are not all recorded cannot be
  checked, and this is the only commission in the system with no order row of its
  own to live on.
- The `orders` table records orders **this bot submitted**. Fabricating a filled
  order row to satisfy a signature would put an order the bot never placed into
  its own audit trail, understate commission, and make "what did the bot do"
  unanswerable. `close_order_key` is nullable in the schema precisely for this
  case.
- Raises `PositionStateError` if the position is already closed or absent.
- The transition is atomic: concurrent calls produce exactly one success.
- The commission reads, the status update and the `position_events` insert run
  inside one `BEGIN IMMEDIATE` on `db.connection.shared()`. The commission was
  previously read on a different connection, outside the transaction that used
  it; `db.orders.get` must not commit the outer transaction.
- In that same transaction, writes one `position_events` row with
  `event = 'CLOSED'`.
- Must never delete a row — history is permanent.

**`async list_open() → list[Position]`**
- Returns all open positions, empty list when none. Never returns `None`.

**`async get(position_id: int) → Position | None`**
- Returns `None` when absent rather than raising.

**`async recompute_realised(position_id: int) → Position`**
- Recalculates and rewrites `realised_pnl` for a **closed** position from the
  commissions currently recorded on its two orders. Called only by the commission
  backfill, after a late commission lands.
- Raises `PositionStateError` when the position is absent or still open.
- Uses the stored `exit_commission` for a position closed `EXTERNAL`, since there
  is no closing order to re-read (v1.35). Without it the backfill would silently
  discard a commission the operations feed had already resolved, turning a
  correct figure back into the overstated one — a recomputation that makes a
  number worse is the failure this function exists to prevent.
- This is the only mutation permitted on a closed position, and it exists because
  a stored figure that silently disagrees with its inputs is worse than one
  corrected once and logged.
- In the same transaction, writes one `position_events` row with
  `event = 'REALISED_RECOMPUTED'` and `detail` naming the previous and new
  `realised_pnl`.

**`async list_closed() → list[Position]`**
- Closed positions, newest exit first. Empty list when none. Consumed by
  `reporter.weekly` and `/history`.

**`async update_lots(position_id: int, lots: int) → Position`**
- Writes the broker's lot count onto an open position during reconciliation.
- Raises `PositionStateError` for a non-positive count, or a position that is
  absent or already closed.
- Never changes entry price, stop or target: the position's risk levels were set
  at entry and a quantity correction does not re-price them.
- In the same transaction, writes one `position_events` row with
  `event = 'LOTS_ADJUSTED'` and `detail` naming the previous and new lot count.

**`async adopt(instrument: Instrument, lots: int, average_price: Decimal, adopted_at: datetime, open_order_key: str) → Position`**
- Creates an open position for a holding discovered at the broker but unknown
  locally, with `adopted = True`, stop and target derived from `average_price`,
  and age counted from `adopted_at`.
- Called only by `broker.reconcile`.
- **`open_order_key` is the key of the bot's own unresolved `ENTRY` order for
  that ticker (v1.38).** It used to synthesise `ADOPTED-{figi}`, a key with no
  order row behind it, while the schema has required
  `open_order_key TEXT NOT NULL REFERENCES orders (key)` since `001`. The
  contract and the schema had disagreed from the beginning and only an unissued
  `PRAGMA foreign_keys` hid it; once #20 turned enforcement on, `adopt` could not
  insert a row at all (#42).

  The real key is available and is the correct one. Since v1.25 this function is
  reached for exactly one condition — a holding whose ticker has an unresolved
  `ENTRY` order of the bot's own, the crash-recovery case — so an order row for
  that ticker always exists. Pointing at it is not a workaround for the foreign
  key: it is the truth the synthetic key was standing in for. It also makes the
  entry commission recoverable, since `db.positions.close` reads it through
  `db.orders.get(open_order_key)` and a synthetic key resolved to nothing.
- Fabricating an `orders` row to satisfy the constraint was the alternative and
  is rejected: the `orders` table records orders this bot submitted, and here it
  did submit one. Inventing a second row describing the same fill would put a
  duplicate of a real order into the bot's own audit trail.
- The position's `strategy` stays `ADOPTED`. This is a second sentinel beside
  rule 35's `UNATTRIBUTED` and they mean different things: `UNATTRIBUTED` is a
  trade whose originating signal could not be found, `ADOPTED` is a row
  reconciliation created rather than the entry path. Neither is ever credited to
  a named strategy, which is what rule 35 actually requires.
- In the same transaction, writes one `position_events` row with
  `event = 'ADOPTED'`.

**`async list_events(position_id: int) → list[PositionEvent]`**
- Returns that position's events oldest-first. Empty list when none, never
  `None`.
- `PositionEvent` is a frozen dataclass owned by this module: `position_id`,
  `occurred_at` (timezone-aware UTC), `event` (one of `OPENED`,
  `STOP_PROTECTION_CHANGED`, `LOTS_ADJUSTED`, `CLOSED`, `REALISED_RECOMPUTED`,
  `ADOPTED`) and `detail` (a JSON object as text).
- Events are never updated and never deleted. This reader is what makes the
  brief's requirement — that a post-incident question be answerable from the
  database alone — true rather than aspirational.

### `zarabot/db/job_runs.py`

**Sole owner of the `job_runs` table.** No other module writes it, and no SQL
for it lives anywhere else.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; every write runs inside
`db.connection.transaction()` (rule 31).

**`async has_run(job: str, period_key: str) → bool`**
- Whether `job` has completed for that period. `period_key` is whatever
  identifies the period the caller schedules on — a Moscow date for a daily job,
  a week-start date for the weekly report.

**`async mark_run(job: str, period_key: str, ran_at: datetime) → None`**
- Records completion. Idempotent: a second call for the same pair keeps the
  first `ran_at`, since when the job *first* completed is the fact worth having.
- Raises `ValueError` on a naive `ran_at`.

**`async last_run(job: str) → datetime | None`**
- The most recent completion of `job`, or `None`. This is what makes "did the
  weekly report go out?" answerable from the database, which it was not while
  the answer lived in a module global (#27).

### `zarabot/db/trading_days.py`

**Sole owner of the `trading_days` table.** No other module writes it, and no
SQL for it lives anywhere else.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; every write runs inside
`db.connection.transaction()` (rule 31).

**`async record_many(sessions: list[SessionInfo]) → int`**
- Upserts one row per day on `trade_date`, returning how many were written. A
  day already recorded is **overwritten** by the newer observation: a holiday
  can be announced after the fact, and the most recent answer from the broker is
  the one to keep.
- Days with no date are skipped rather than stored under a null key.
- Runs in a single transaction, so a partial window is never recorded.

**`async list_since(start: date) → list[SessionInfo]`**
- Recorded days from `start` onwards, oldest first. Empty list when none, never
  `None`.

**`async earliest() → date | None`**
- The oldest recorded date, or `None` when the table is empty. This is what
  `market.session.covers` is built on.

### `zarabot/db/orders.py`

**Sole owner of order rows and of order status transitions.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async record_submitting(key: str, ticker: str, side: Side, lots: int, intent: str, exit_trigger: ExitTrigger | None = None) → OrderRecord`**
- Persists the intent to place an order **before** it is sent.
- `exit_trigger` is required when `intent` is `EXIT` and must be `None` when it is
  `ENTRY`; violating either raises `ValueError`. Recording why an exit is being
  submitted is what allows a recovered fill to be attributed correctly rather
  than guessed.
- Raises `DuplicateOrderError` if the idempotency key already exists.
- Ordering constraint: must complete before `broker.client.post_order` is called
  with the same key. This ordering is what makes a crash mid-submission
  recoverable, and reversing it is a critical defect.

**`async settle(key: str, status: OrderStatus, filled_lots: int, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None, broker_order_id: str | None = None) → OrderRecord`**
- Records a terminal outcome, including the commission the broker reported on the
  order. `None` means not yet known, which is distinct from zero.
- Raises `OrderStateError` on a transition out of a terminal status.

**`async record_commission(key: str, commission: Decimal) → OrderRecord`**
- Writes commission onto an already-terminal order. This is the one field that
  may be set after a row reaches a terminal status, because the broker can report
  it later than the fill. Raises `OrderStateError` when the row is absent.

**`async list_missing_commission(since: datetime, until: datetime) → list[OrderRecord]`**
- `FILLED` orders in the period whose commission is still unknown. Drives the
  daily backfill. Empty list when none.
- Rows already alerted are **still returned**: the point of the terminal state is
  to stop repeating the alert, not to stop trying to resolve the number. A
  re-query is cheap and a commission that finally lands is still worth writing.

**`async mark_commission_alerted(key: str, at: datetime) → OrderRecord`**
- Records that the owner has been told once about this row's unknown commission
  (v1.39). Idempotent: a row already marked keeps its original timestamp, since
  the moment the owner was first told is the fact worth keeping.
- Raises `OrderStateError` when the row is absent. Raises `ValueError` on a naive
  `at`.
- The 24-hour staleness policy stays in `ops.commissions`, which owns it. This
  function records only the fact, so the policy is not split across two
  modules — the mistake that keeps recurring as failure class 2.

**`async get(key: str) → OrderRecord | None`**
- Returns the order or `None` when absent. `None` remains a legitimate answer
  for a key that names no row, but it is no longer *expected* for an adopted
  position: since v1.38 an adopted position points at the bot's own unresolved
  entry order, which exists.
- This is how another repository obtains an order. `db.positions` calls it to
  read the opening commission; it must never query the `orders` table directly.

**`async list_unresolved() → list[OrderRecord]`**
- Returns orders left in `SUBMITTING` or `SUBMITTED`, oldest first.
- Consumed by `app.startup` before trading begins.

### `zarabot/db/stop_orders.py`

**Sole owner of stop-order rows.** No other module writes them.

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

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

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async start(ticker: str, at: datetime) → None`** — records or overwrites with the newer instant.
- **A write failure propagates (v1.63).** This module catches no
  `aiosqlite.Error` and logs no failure of its own: cooldowns are rule 11, and
  `db.connection` already emits `db_write_failed` with `critical` true before
  re-raising. Until v1.63 the implementation swallowed the error and logged an
  unstructured line, which was behaviour the contract never granted — a lost
  cooldown then looked like a successful one and the bot could re-enter a ticker
  it had just exited.

**`async is_active(ticker: str, now: datetime, minutes: int) → bool`**
- True while `now - started_at < minutes`. Exactly at the boundary returns False.

**`async active_until(ticker: str, minutes: int) → datetime | None`** — for display in command replies.

### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

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

**One channel per process, and one `Config`.** The module holds a single
`AsyncClient`, created lazily on first use — never at import, which `AGENTS.md`
forbids outside `config` — and reused for every later call. `app.shutdown` closes
it through `broker.client.close()`. Opening a fresh client per call meant a new
TLS handshake per request, and a single order cost three full `config.load()`
calls, each re-reading every environment variable, re-parsing every `Decimal` and
touching the filesystem to stat `ML_MODEL_PATH` — on the latency-critical path
(#18). Configuration is read through `config.get()`, the memoised accessor, not
`config.load()`.
- **Emits `broker_unavailable` (WARNING) on each `BrokerUnavailable` with
  `method` and `consecutive_failures`, and `rate_limited` (WARNING) on each
  `BrokerRateLimited` with `method`, `retry_after_seconds` (v1.61).** This
  module is the only one that sees those exceptions at the source; callers must
  not re-emit them.
- **`backoff_seconds` is not a field of this event (v1.65).** v1.61 required it
  here, but this module does not back off and cannot know the number: the
  escalating delay, its cap and the broker's `retry_after` hint all belong to
  `app.loops`, whose §3.2 cases pin them. An emitter that cannot know a value
  can only send a constant, and a constant `backoff_seconds: 0` tells an
  operator that no back-off is in effect while `app.loops` may be five cycles
  deep in one — worse than the field's absence.
- `consecutive_failures` **is** knowable here and stays: it is this module's own
  count of consecutive `BrokerUnavailable` raises for that method, reset when
  the method next succeeds. A count that is never reset is a latch, and this one
  must be cleared on the success path.
- If a back-off figure is wanted in the log, it belongs to an event owned by
  `app.loops`, which computes it. Assigning one is a separate amendment; this
  one only stops requiring a field at a site that cannot supply it.

**`async close() → None`**
- Closes the process client and forgets it. Idempotent. Called only by
  `app.shutdown`. A later call creates a new client, so closing is not a
  one-way door for a long-lived process that must reconnect.

**Errors are typed by what they are, not by where they were caught.** Only a
transport failure becomes `BrokerUnavailable`; a `RESOURCE_EXHAUSTED` becomes
`BrokerRateLimited`; `NOT_FOUND` becomes the caller's not-found type. **Every
other gRPC status — `INVALID_ARGUMENT` foremost — and every non-SDK exception
propagates as itself**, with its cause preserved via `raise ... from exc`.

Converting everything into `BrokerUnavailable` is what hid #39 for the whole life
of the deployment: a malformed request was retried forever as though it were
weather, the alert said the broker could not be reached, and the only place the
words `INVALID_ARGUMENT` appeared was the SDK's own log line. A programming error
must not wear an outage's costume — the caller's retry-with-backoff is correct
for an outage and useless for a bug, and the difference between them is exactly
what the type is for (#23). `from None` is forbidden: it discards the traceback
that names the real fault. Token redaction already prevents secret leakage, and
that is what makes preserving the cause safe.

**A quote's timestamp is read directly, never probed for (v1.43).**
`_quote_time` read `raw.time` and fell back to `raw.timestamp`, a field
`LastPrice` has never had (§2.1). The fallback was not merely dead: if `time`
were ever renamed, every quote would come back with no timestamp, be rejected
under rule 9b, and the owner would see "N prices rejected" every cycle — a
message that reads like a broker data problem while the real fault is an
integration break. **No `LOCAL` position's stop-loss would fire again**, because
that exit path needs a price. An `AttributeError` names the field and the line;
a defensive `getattr` chain names nothing. This is failure class 5 in
`ops/STATE.md`, and the rule generalises: where the broker's own field is the
contract, read it, and let its absence be loud.

**A partial fill is not a fill.** `EXECUTION_REPORT_STATUS_PARTIALLYFILL` maps to
`SUBMITTED` — the order is still live at the broker — with `filled_lots` carrying
what has filled so far. `FILLED` means `filled_lots == lots` and nothing further
is coming. Collapsing partial into filled erased the distinction at the boundary,
so no caller could act on it: the bot opened a position for the filled portion
while the remainder stayed live, and the account then held more shares than the
position row recorded (#10). What the *callers* do about a partial fill belongs
to `execution.orders`, and v1.34 settles it there — as neither of the two
remedies this paragraph once anticipated. There is no multi-slice exit loop to
cap and no quantity-weighted price to compute, because a partial no longer
settles as a fill and so never starts a second slice.

**`async get_instrument(ticker: str) → Instrument`**
- Raises `InstrumentNotFound` when the ticker does not resolve, `BrokerUnavailable`
  on transport failure, `BrokerRateLimited` when throttled.

**`async get_candles(figi: str, interval: CandleInterval, since: datetime, until: datetime) → list[Candle]`**
- Returns candles ordered oldest-first with timezone-aware timestamps.
- Returns an empty list when the range contains no trading activity.
- Raises `ValueError` on naive datetimes.

**`PriceRejected`** — a quote arrived but is not usable. **Distinct from
`BrokerUnavailable`**, which means the broker could not be reached. Conflating
them makes a malformed field read as a network outage, so it counts toward the
consecutive-failure alert and is retried as though waiting would help.

**`async get_last_price(figi: str) → Decimal`**
- Validates the quote at the **single point prices enter the system**, and
  treats a bad price as missing data rather than as a signal. Raises
  `PriceRejected` when:
  - the price is **not strictly positive** — `Decimal(0)` currently flows
    straight through to `lifecycle.exits`, where `0 <= stop_price` is true for
    every position, so one degraded response liquidates the whole book at
    market;
  - the quote's timestamp is older than `price_max_age_seconds`;
  - the quote carries **no timestamp, or a naive one** — freshness that cannot
    be verified is not freshness. This is data from an outside system, so it is
    rejected as unusable rather than raising `ValueError` the way a naive
    datetime crossing an internal module boundary does;
  - the price differs from the last accepted price for that instrument by more
    than `price_max_move_pct`.
- Keeps the last accepted price per instrument, which is what makes the move
  check possible. This is the only state this module holds, and it is why the
  check cannot live in `lifecycle.exits`: that module is pure and has no memory
  of the previous tick.
- A rejected quote does not update the last accepted price. Accepting an
  implausible value as the new baseline would make the *next* implausible value
  look reasonable.

**`async get_portfolio() → PortfolioState`**
- **`cash` is RUB buying power**, not `total_amount_currencies`. That field is the
  converted value of *all* currency positions, including blocked and reserved
  funds and any non-RUB balance, so it overstates what an order can actually
  spend and an order sized against it can be refused for insufficient funds
  (#16). Where the response separates available from blocked, available wins.
- Returns cash and holdings as reported by the broker. This is the authoritative
  view referred to throughout the brief.

**`async get_trading_schedule(days: int) → list[SessionInfo]`**
- Session open and close instants per day, timezone-aware, marking non-trading
  days.
- **Requests the exchange by name: `exchange="MOEX"`.** Not a substring filter
  over the returned list. The response carries 147 exchanges, 53 of which contain
  `MOEX`, and taking the first match returned whichever the broker happened to
  order first — in practice `MOEX_MRNG_EVNG_E_WKND_D`, an extended session
  running to 23:49 MSK that reports **Saturday and Sunday as trading days**
  (#43). `MOEX` is the main equity board: 10:00–18:54:59 MSK, weekends closed.
  Naming it returns exactly one exchange, so there is nothing left to choose.
- **The range is anchored to the start of the current UTC day**, and `days` may
  not exceed 14. A mid-day `from_` plus 14 days is rejected by the broker with
  `INVALID_ARGUMENT` / `30002`, which is what left the cache empty and
  `is_open()` false for the whole of the bot's life (#39). Confirmed against the
  live account: `from=now, to=now+14d` fails, `from=now, to=now+13d` succeeds,
  and `from=midnight, to=midnight+14d` succeeds — the horizon is measured from
  the start of the day, not from the instant of the call.
- **A day with `is_trading_day` false yields no session**, whatever its
  timestamps say. A closed day carries `1970-01-01` in both, and some MOEX-
  prefixed exchanges report `is_trading_day` true with those epoch values; either
  read as a session is another way to believe the market is open.
- The SDK marks `trading_schedules` deprecated as of its 1.0.0. It is the only
  calendar surface available today; when a replacement appears this is the
  contract to amend, and V10's recorded SDK version is what makes the change
  visible.

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

**`async cancel_order(key: str) → None`**
- Cancels a live ordinary order by its idempotency key, with
  `order_id_type=ORDER_ID_TYPE_REQUEST` — the same lookup `get_order_state`
  uses, because after a crash the exchange identifier is precisely what was
  lost.
- Idempotent in the same sense as `cancel_stop_order`: an order already filled,
  already cancelled, or unknown to the broker is **not** an error. The caller is
  racing the exchange by definition, and the authoritative answer comes from the
  `get_order_state` that follows it, never from this call's own outcome.
- Raises `BrokerUnavailable` or `BrokerRateLimited` on transport failure, and
  nothing else.
- Added in v1.34 so `execution.orders` can abandon the unfilled remainder of a
  partially filled **entry** (#10). It must never be used on an exit: a
  half-exited position is the one state the system must not rest in, and the
  remainder there is retried, never dropped.

**`async list_stop_orders() → list[StopOrderRecord]`**
- Every standing stop order on the account. Consumed by reconciliation.

**`async get_executed_stop_fills(since: datetime, until: datetime) → dict[str, OrderRecord]`**
- Returns the **actual execution** of every stop order that fired in the window,
  keyed by the broker's `stop_order_id`. Empty dict when none fired; never
  `None`.
- Implemented as two SDK calls, composed here because this is the only module
  permitted to talk to the broker: `get_stop_orders` with
  `StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED`, then each result's
  `exchange_order_id` resolved through `get_order_state` with
  `OrderIdType.ORDER_ID_TYPE_EXCHANGE`.
- The `OrderRecord` carries the broker's own numbers: `filled_price` from
  `executed_order_price`, `filled_lots` from `lots_executed`, and `commission`
  from `executed_commission`. None of the three is estimated, and none comes from
  a quote.
- **A stop whose `exchange_order_id` does not resolve is omitted, not guessed
  at.** The caller leaves the position open and retries. A position closed a
  minute late is recoverable; a position closed at an invented price is not.
- Raises `ValueError` on naive datetimes.

**`async get_order_state_by_broker_id(broker_order_id: str) → OrderRecord`**
- The same lookup as `get_order_state` but with
  `order_id_type=ORDER_ID_TYPE_EXCHANGE`, for a row whose `key` the broker has
  never seen (v1.39). Raises `OrderNotFound` when it does not resolve.
- Exists because a stop the exchange fired is recorded locally under an
  idempotency key the bot invented, so `get_order_state` by that key can only
  ever return `OrderNotFound` — which made the commission on every stop exit
  permanently unrecoverable (#8). It is a separate function rather than a
  parameter on `get_order_state` because the two answer different questions:
  one asks "what happened to the order I sent", the other "what happened to the
  order the exchange placed for me", and only the first is a recovery path.
- The returned record's `key` is the `broker_order_id` it was asked about, as
  `get_executed_stop_fills` already does. This module does not know the local
  row's key and must not guess at one.

**`async get_max_lots(figi: str) → int`**
- The maximum lots the broker will accept for a buy on this account. A pre-submit
  sanity check against `risk.sizing`, which models cash but not settlement or
  instrument-specific restrictions.

**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
- Executed operations including actual commission charged. Used for independent
  reconciliation of costs over a period — **not** as the per-order commission
  source: `OperationRecord` carries no order identifier, so attributing an
  operation to an order would mean matching on instrument, time and quantity,
  which is ambiguous exactly when two similar orders are close together.
- Each record carries `operation_type`, `state` and `parent_operation_id`
  verbatim (v1.35). `commission` is populated for fee operations, identified by
  `operation_type`, and is zero elsewhere. It was identified by testing whether
  the string `FEE` appeared in an attribute the record did not expose, which
  worked only because every fee type happens to contain it.
- **Only `OPERATION_STATE_EXECUTED` operations are returned.** A cancelled or
  still-progressing operation is not something that happened, and counting one
  as a cost or as a sale is the same error in two places.
- v1.35 gives this function a second consumer and a second purpose:
  `broker.reconcile` reads it to answer the one question no order of ours can,
  which is what a sale **the bot did not submit** was actually done at. That does
  not make it the per-order commission source; the paragraph above still holds.

**TLS requires the broker's own root certificate.** T-Bank's endpoint presents a
certificate chaining to the Russian Trusted Root CA, which gRPC's built-in trust
store does not contain. The SDK ships that root at
`t_tech/invest/certs/RussianTrustedRootCA.pem` but loads it **only** when the
environment variable `SSL_TBANK_VERIFY` is `true`; its default is `false`, so
every call otherwise dies in the TLS handshake with
`CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain`.

This is not a property of any particular network — it was reproduced from a
clean machine, and would fail identically on the VPS. `config` therefore exposes
`ssl_tbank_verify`, defaulting to **true**, and `app.startup` writes it into the
process environment immediately after `config.load()` and before any broker call.
`broker.client` must not read the variable itself: the SDK reads it from the
environment when a channel is created, so the only requirement is that it is set
before the first client is constructed.

**Commission comes back on the order itself.** Both `PostOrderResponse` and
`OrderState` carry `executed_commission`, keyed by our own idempotency key.
`post_market_order` and `get_order_state` therefore populate
`OrderRecord.commission` directly, with no matching and no ambiguity. Commission
is never estimated, and never inferred from an operations feed.

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

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31). This module is not a `db.*` repository, but it
was one of the eight sites opening its own connection.

**`async reconcile(now: datetime) → ReconciliationReport`**
- Compares `broker.client.get_portfolio()` against `db.positions.list_open()`.
- **Locally-open but absent at the broker → the sale is resolved from the
  operations feed, or the position is not closed at all (v1.35).** Until now this
  booked the exit at `get_last_price` as of the moment of *detection* — which can
  be hours or days after the sale, and on a different day entirely if the bot was
  down — and fell back to the position's own `entry_price` when the broker was
  unreachable, recording an exit of exactly zero P&L. Both are numbers this
  module made up, which rule 33 forbids (#11).

  The resolution: `broker.client.get_operations(position.entry_at, now)`,
  filtered to the position's `figi` and to the sale operation types
  (`OPERATION_TYPE_SELL` and its `DELIVERY_SELL` and `SELL_MARGIN` variants).
  Over **every** such sale in the window —
    - `exit_price` is their **quantity-weighted average**;
    - `closed_at` is the **latest** of their timestamps, which is when the
      position left the account rather than when the bot noticed;
    - `exit_commission` is the sum of the fee operations whose
      `parent_operation_id` is one of those sales, passed through to
      `db.positions.close`.
  The window starts at `entry_at`, so a sale that happened at all is inside it,
  however long the bot was down — and since at most one position per ticker is
  open at a time, every sale of that instrument inside it belongs to this
  position. Deliberately **no** "take sales until their quantities cover the
  position" cutoff: whether the broker reports an operation's `quantity` in lots
  or in instrument units is an assumption this project has not tested against a
  live account, and it is the same class of assumption that produced #39 and
  #43. A weighted average is correct under either reading, because the units
  cancel; a cutoff is not. This is the one place a weighted price is legitimate,
  and it is legitimate because every input is a number the broker reported about
  a trade that occurred — not, as in #10, a blend across orders invented to fit
  a signature.

  The close still passes `order = None`. This module records **no** order row: it
  did not submit one, and inventing one would contradict its own prohibition on
  trading.
- **A sale that cannot be resolved is reported, not booked.** When the feed
  returns no covering sale, or is unavailable, the position **stays open** and
  the report carries
  `{"type": "EXIT_UNRESOLVED", "ticker", "position_id", "reason"}`, with an
  alert. Rule 33 already settles which way this falls: a position closed a cycle
  late is recoverable and one closed at a substituted number is not, because
  nothing downstream can tell the substituted number from a real one. A covering
  sale that is genuinely absent means the shares left the account by some route
  that was not a trade, and that is the owner's to explain rather than this
  module's to guess.
- `EXIT_UNRESOLVED` does **not** stop the bot, unlike rule 32's foreign holding.
  That refusal exists for shares the bot might trade; this is a row describing
  shares the account no longer has. While it stands, the row still marks to
  market in `pnl.bot_equity` and `lifecycle.exits` may eventually try to sell it,
  which the broker will refuse. Both are visible and alerted, and that is a
  different kind of wrongness from a fabricated exit price written permanently
  into the trade history.
- **Present at the broker but unknown locally → reported as `FOREIGN_HOLDING`,
  never adopted.** This module previously called `db.positions.adopt` here, which
  derived a stop and target from the holding's *average cost* and so handed the
  next trading cycle a position already past its take-profit. Adoption of an
  unknown holding is no longer this module's decision or anyone else's: the
  account is the bot's alone, and a holding it does not recognise is a condition
  to report, not inventory to manage. The adjustment names the ticker, the lot
  count and the average price, so `app.startup` can name them in its refusal.
  `db.positions.adopt` remains in the contract and is still called for a holding
  the bot **does** recognise but whose local row is missing — the crash-recovery
  case it was written for. This module passes it the key of that recognising
  order (v1.38): the recognition rule already identifies exactly one order, so
  the key is in hand at the moment the decision is made, and it is what the
  adopted position must point at. Where more than one unresolved `ENTRY` order
  exists for a ticker — which the per-ticker submission lock should prevent — the
  **oldest by `created_at`** is used, the same tie-break as `STOP_DUPLICATE`.
- **A holding is recognised when the bot has an unresolved `ENTRY` order for that
  ticker** — `SUBMITTING` or `SUBMITTED` in `db.orders.list_unresolved()`. That
  is the residue of exactly one sequence: the bot submitted the buy, the broker
  filled it, and the process died before the position row was written. Anything
  else at the broker is foreign, including a holding whose entry order has
  already reached a terminal status, because the bot then either has its position
  row or has decided it does not. The rule is deliberately the narrowest one that
  covers crash recovery: every widening of it is a way for a holding the owner
  bought to be treated as the bot's.
- Lot mismatch → the broker's count is written locally.
- **Stop orders are reconciled too, but this module does not act on them.**
  Every open position must have exactly one live stop order. This module
  *reports* each discrepancy — a position with no stop, a stop with no position,
  a stop at the wrong price, **or more than one live stop on the same position** —
  and the caller performs the remedy through
  `execution.orders`, which is the only module permitted to place or cancel
  orders. Keeping reconciliation observational is what allows it to run
  anywhere, including read-only diagnostics, without financial side effects.
- On restart an existing stop is **adopted** rather than replaced — two stops on
  one position would sell it twice.
- **A stop is mispriced only when it differs from the position's stop by a full
  price increment or more (v1.55).** The broker snaps a posted stop to the
  instrument's `min_price_increment`, so the price it holds is almost never the
  price the bot computed: on 2026-09-07 the account held GMKN at 125.44 against
  a stored 125.457, SBER at 265.89 against 265.8955 and MTSS at 179.05 against
  179.075. An exact inequality called all three mispriced on every startup, and
  the caller's remedy — cancel then re-post — left three live positions
  momentarily unprotected once per restart, wrote a fresh `stop_orders` row each
  time, and did it for stops the broker had placed exactly as asked. The
  comparison is therefore `abs(broker − local) < min_price_increment`, read from
  `broker.client.get_instrument(ticker)` for the position's ticker. Nothing is
  rounded anywhere: the bot does not know which way the broker rounds, and
  writing a guessed rounded price into `positions` or `stop_orders` would put an
  invented number in the record, which rule 33 forbids. A tolerance costs
  nothing here because the bot never moves a stop after entry — a genuinely
  wrong stop is wrong by the distance between two different prices, not by less
  than one tick.
- **A stop whose price cannot be compared is neither mispriced nor adoptable
  (v1.56).** When `get_instrument` fails for the position's ticker, or reports a
  `min_price_increment` of zero or less, that position's stop price is not
  judged: **no `STOP_MISPRICED` and no `STOP_ADOPTABLE`** are reported for it.
  The failure is alerted and reconciliation continues; `STOP_DUPLICATE` and
  `STOP_ORPHAN` are unaffected, because neither depends on the price.

  Withholding `STOP_MISPRICED` alone was the v1.47 defect. The price comparison
  **is** the guard on adoption — `STOP_ADOPTABLE` means "this stop stands at the
  price the position wants, bind it" — so suppressing only the misprice finding
  routed an unjudged stop into the `elif` beneath it and reported it adoptable.
  `app.startup` then calls `adopt_existing_stop`, which sets
  `stop_protection = EXCHANGE`, and `lifecycle.exits` fires `STOP_LOSS` only
  while protection is `LOCAL`. A stop standing at a price nobody could verify
  would have become the position's sole protection, and the bot would have
  stopped watching its own.

  Not adopting is the safe residual **for a `LOCAL` position**: it stays `LOCAL`,
  `lifecycle.exits` keeps watching `stop_price` itself, and the broker's stop
  stands underneath as well.

  A position already `EXCHANGE`-protected has a different residual, and it is
  weaker: it stays `EXCHANGE` and nothing at the bot verifies its stop until the
  metadata is readable again, because `lifecycle.exits` fires `STOP_LOSS` only
  while protection is `LOCAL`. It is **not** demoted to `LOCAL` to close that
  gap. Demotion would arm the bot's own seller while the exchange's stop is
  still live, which is the double-sell condition the ownership rule exists to
  prevent — a worse failure than an unverified stop that is, after all, still
  standing at the exchange. This is the same trade rule 38 makes about
  `STOP_MISPRICED`, and it is stated here because "the unjudged case protects
  itself" is true of one entrance to this branch and not the other (v1.57).

  Either way the next reconciliation with readable metadata judges the stop
  properly and adopts it, reports it mispriced, or leaves it alone. Neither
  finding is skipped because it is unlikely — both are skipped because both
  remedies act on a price, and the price is exactly what is missing.
- **More than one live stop on a position is reported as `STOP_DUPLICATE`**, and
  is the most serious discrepancy this module can find: it is the double-sell
  condition the ownership design exists to prevent, actually present. The remedy
  keeps the stop whose key matches the position's recorded `stop_order_key`, or
  the oldest if none matches, and cancels every other. A duplicate must never be
  silently skipped as though it were the position's one legitimate stop.
- **The `STOP_DUPLICATE` adjustment names the keeper.** It carries `keep`, the
  identifier of the stop to retain, and `cancel`, the identifiers of every other.
  This module applies the keep-rule because this module is where the rule is
  written and where `stop_order_key` and `created_at` are already in hand;
  emitting an undifferentiated list of identifiers forced the caller either to
  re-derive the rule or, as happened, to skip the adjustment entirely (#35).
- Returns a report enumerating every adjustment; an empty report means agreement.
- **After persist, emit `reconciliation` (INFO) with `adjustments_count` and
  `types` (the distinct adjustment type names) (v1.61).** Empty agreement still
  emits, with count 0 and `types` an empty list — silence here is how a failed
  reconcile looks like a skip.
- **Emits `stop_order_executed` when it books an exchange-fired stop close, and
  `stop_order_orphaned` when it reports `STOP_ORPHAN` (v1.61).** Fields match
  §7.1.
- Idempotent.
- Ordering constraint: runs during `app.startup` after migrations and after
  unresolved-order recovery, and before any entry is permitted.
- Must never place or cancel an order, including stop orders. Reconciliation
  observes and records; it does not trade. Every remedy it identifies is carried
  out by `app.startup` through `execution.orders`.

### `zarabot/market/session.py`

**`async refresh(days: int) → None`**
- Caches the schedule and **persists every day of it** through
  `db.trading_days.record_many`, in one transaction (v1.44). Called at startup
  and once per trading day, so this is one broker call and one write a day.
- Reloads the recorded history into memory afterwards, so `calendar()` stays a
  synchronous read of what is already in hand and costs nothing per cycle.
- **A write failure is `aiosqlite.Error` from `record_many` or from the history
  reload, and only that (v1.59).** It is logged at ERROR and does not propagate:
  an unavailable history degrades age counting, which `covers` then reports, and
  must not stop the bot trading. The schedule itself is already cached by that
  point.
- **Any other exception from that path propagates (v1.59).** Until v1.59 the
  clause above was unqualified and the code caught `Exception`, so an
  `AttributeError` from a rename was logged and dropped, `covers()` then returned
  `False`, and `MAX_AGE` stayed suppressed with nothing raised (#52). A
  programming error is not a degraded calendar, and a catch as broad as this one
  turns the loud failure into the silent one. `refresh` still does not raise for
  an unavailable broker schedule — that is rule 10 and is unchanged.
- **The unavailability latch is cleared on the success path**, next to the cache
  write (v1.40). It was set on the first failure and never cleared, so a schedule
  that went unavailable, recovered, and went unavailable again produced silence
  from this module for the rest of the process lifetime (#32). Rule 10 says
  "alert once" without saying once per incident or once per process; `app.loops`
  resets its equivalent latch on a successful refresh, and that asymmetry is what
  settles the reading — **once per incident**.
- **A response containing no trading sessions is treated as unavailable**, per
  error rule 10: the cache is left as it was, and the owner is alerted once. It
  must never replace a populated cache with an empty one, and it must never
  report success having stored nothing — a schedule fetch that "succeeds" with
  no sessions leaves the bot unable to trade with nothing raised.
- Does not raise on an unavailable schedule. Aborting startup over a transient
  broker blip is worse than starting and reporting the condition, which
  `cache_exhausted` then keeps visible on every cycle until it is fixed.
- **On a successful refresh, emit `session_open` or `session_closed` (v1.61).**
  `session_open` when the first day of the fetched window is a trading session,
  with `trade_date`, `opens_at`, `closes_at` from that `SessionInfo`.
  `session_closed` when that day is not a trading session, with the same three
  fields (`opens_at` / `closes_at` may be null). This is a log, not a Telegram
  alert — the brief deliberately does not alert session open/close.

**`is_open(now: datetime) → bool`**
- True when `now` falls within a main session, inclusive of the open instant and
  exclusive of the close instant.
- Returns `False` when the schedule is unavailable — the safe default is not to
  trade.

**`cache_exhausted(now: datetime) → bool`**
- True when `now` is at or past the last cached session, meaning `is_open` is
  returning `False` because the bot has run out of calendar rather than because
  the market is shut.
- **True when the cache is empty.** An empty cache is the strongest form of
  having run out of calendar: there is no calendar at all. Reporting `False`
  there is the failure this function exists to detect, in its worst form — not a
  cache that expired, but one that never filled, with `is_open` false every
  cycle, nothing raised, and the heartbeat still reporting health.
- This relies on `app.startup` step 5 refreshing the schedule **before**
  `app.loops.run()` starts the trading cycle. Without that ordering an empty
  cache would be the normal state for the first moments of a run and this
  function would alert on every start. The ordering is contractual, not
  incidental; a change that moves the first refresh after `run()` must revisit
  this contract.
- These two states are indistinguishable from `is_open` alone, and conflating
  them is how a bot stops trading silently: every cycle returns "closed", no
  error is raised, and the heartbeat keeps reporting health. `app.loops` checks
  this and alerts.

**Refresh cadence.** The schedule is refreshed at startup **and at every daily
rollover**, always fetching a horizon longer than the gap between refreshes. A
cache filled once at startup expires while the process is still running, which
is the failure this cadence exists to prevent.

**`current_session(now: datetime) → SessionInfo | None`**

**`in_closing_window(now: datetime, minutes: int) → bool`** — true during the final `minutes` of the current session; used only by the maximum-age exit.

**`calendar() → TradingCalendar`**
- The cached schedule as a `TradingCalendar`, for callers that need to count
  trading days rather than ask whether a moment is inside a session. Empty
  calendar when the cache is empty; never `None`.
- **It spans backwards by remembering, not by asking (v1.44).** The broker
  rejects any `from_` before today's midnight with `INVALID_ARGUMENT` / 30003
  (§2.1) — v1.41 assumed otherwise, was deployed, and aborted startup. The past
  cannot be fetched, so it is **recorded**: every `refresh` writes the whole
  window it received to `db.trading_days`, and `calendar()` returns the union of
  that history with the live cache, oldest first.

  The property that makes this sufficient is that the window is **fourteen days
  wide, not one**. A single run records the next fortnight, so a bot that ran at
  any point in the last fourteen days already has every day since on disk —
  including days it was switched off for. Coverage fails only after an outage
  longer than the window, and that case is detectable rather than silent (below).
- **This design adds no new broker assumption.** That is deliberate and is the
  difference from v1.41: the only fetch is the one already made and already
  verified, and everything new is a local table whose behaviour is entirely
  testable. The assumption v1.41 rested on was the one thing not checked against
  the account, and it was false.

**`covers(day: date) → bool`**
- Whether the recorded calendar reaches back to `day`, so a caller can tell a
  count it can stand behind from one it cannot. `False` when the history is
  empty.
- This exists because the failure it guards is silent by nature: an uncovered
  day simply is not counted, `trading_days_open` comes back short, and `MAX_AGE`
  does not fire. Nothing raises. #45 lived for the project's whole life on
  exactly that.
- Added in v1.40 so `app.loops` stops fetching a fourteen-day schedule **once a
  minute** for data that changes at most daily and that this module already
  holds (#19). `_schedule_refresh_loop` refreshes this cache once per Moscow
  day; that is the only fetch there should ever be.
- It also removes a silent failure the caller had no way to see: `app.loops`
  returned an *empty* calendar on a broker error, which made
  `clock.trading_days_between` count zero and disabled `MAX_AGE` exits with no
  alert. Reading the cache cannot produce that state — an unavailable schedule
  leaves the cache as it was and alerts under rule 10, and an empty cache makes
  `is_open` false, so the cycle never reaches the exit step at all.
- **This does not fix #45.** The cached window is the same forward-looking one,
  anchored to the start of the current UTC day, so counting trading days
  *backwards* from a position's entry still finds nothing before today. That is a
  separate defect in what the calendar spans, not in how often it is fetched.

**`next_open(now: datetime) → datetime`** — used by the loop to sleep rather than poll.

### `zarabot/market/data.py`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
- Returns per-ticker candle series, oldest-first.
- A ticker whose fetch fails with a **broker** failure is omitted from the
  result and logged at WARNING; the batch still returns. One unavailable
  instrument must never blind the bot to the rest.
- **A call is degraded for a ticker when the fetch fails, or when it succeeds
  with fewer than `lookback` candles** — no candles at all included. The second
  half is not a lesser case of the first: a fetch that returns nothing looks
  like success to every counter, and the ticker is then skipped by
  `app.loops._evaluate_entries`, or evaluated to `None` by every strategy whose
  lookback exceeds what came back, on every cycle, in silence. That is the same
  blindness #23 names, one layer downstream of it.
- **Consecutive degraded calls are counted per ticker, and a persistent one
  alerts.** On the third consecutive degraded call for a ticker the owner is
  alerted once, naming the ticker and the reason — the failure, or the candle
  count against the count required; nothing further is sent for that ticker
  until a call is not degraded. A good call clears both its count and its
  alerted flag, so a later degradation alerts again. Tickers crossing the
  threshold in the same call share one alert. **A failure and a shortfall share
  one counter**, or a ticker alternating between them would never cross a
  threshold at all. This is rule 9, and rule 36 is the shape it belongs to.
- **A short series is still returned.** Reporting insufficiency must not become
  dropping the ticker: `lookback` is the longest lookback among the enabled
  strategies, so a series too short for that one may still satisfy a shorter
  one, and the caller decides. Only a failed fetch omits a ticker from the
  result.
- **Only the broker's own failures are caught** — `BrokerUnavailable`,
  `BrokerRateLimited` and `InstrumentNotFound`. Every other exception
  propagates: an `AttributeError` from a renamed SDK field or a `ValueError`
  from a malformed candle reaches `app.loops._supervise`, which alerts with a
  traceback and restarts under rule 21. Since v1.27 `broker.client` raises those
  as themselves rather than as `BrokerUnavailable`; catching `Exception` here
  put them straight back in the dark, which is the second half of #23.
- Never pads or interpolates missing candles.
- **Emits `candles_failed` (WARNING) with `ticker` and `error` whenever a ticker
  is omitted or counted as degraded (v1.61).** `error` is the exception type
  name, or `short_history`. This is the structured event; the Telegram alert on
  the third consecutive degradation is unchanged.
- The counters are process-local, like `market.session`'s cache: they measure
  consecutive failures of *this* process, and a restart is entitled to start
  over rather than inherit a count it did not observe.

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
- **Trust assumption:** loading a joblib bundle executes code contained in the
  file. `ML_MODEL_PATH` must therefore point only at a model this project's own
  `sandbox.train` produced and the owner copied across. It is not a path to
  accept from anywhere else, and this is a deployment rule rather than something
  the loader can validate.
- Called once at startup, never on the trading path — a model failure must be
  loud and early, never mid-session.

**`build_features(candles: list[Candle]) → list[float]`**
- Pure. Builds the feature vector in `FEATURE_NAMES` order from the most recent
  `lookback` candles.
- Raises `ValueError` when given fewer candles than `lookback`.
- **Sole owner of feature construction.** `sandbox.train` imports this function;
  no other code computes these features. Duplicating it is a critical defect —
  see the sandbox contract.

**`evaluate(...) → Signal | None`** — as the protocol, returning `None` below
`CONFIDENCE_THRESHOLD`, a module constant rather than an environment variable.
The threshold is a property of the trained model, not of the deployment: moving
it changes what the model means, so it travels with the code and a redeploy, the
same way risk limits do. There is deliberately no `ML_CONFIDENCE_THRESHOLD`. Absent from the registry entirely when `ML_MODEL_PATH` is unset.

### `zarabot/risk/sizing.py`

**`position_budget(allocated: Decimal, size_pct: Decimal) → Decimal`**
- Pure. Returns `size_pct% × allocated` — the intended cost of one position,
  before headroom and the cash reserve narrow it further.
- Exists so that the budget has **one** definition. `size_position` computes it
  to bound an order; `app.startup` compares it against a lot cost to decide
  whether any order is possible at all. Two copies of the formula in two modules
  is a drift hazard on the money path, and the second copy would be in an
  orchestration module with a 70% coverage floor.
- `size_position` **must** obtain its budget from this function rather than
  recomputing it. That is the whole point of extracting it, and a
  reimplementation satisfies the signature while losing the guarantee.
- Never negative. `allocated ≤ 0` is refused by `config.load()` and is not this
  function's concern.

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, open_cost: Decimal, reserve_pct: Decimal) → int`**
- Pure. Returns the number of **whole lots** to buy.
- Rounds down, always. Never returns a negative number.
- Bounded by three quantities, and the smallest wins:
  - **budget** — `position_budget(allocated, size_pct)`, the intended size of
    one position. Obtained from that function, never recomputed here;
  - **headroom** — `allocated − open_cost`, so the portfolio's total cost never
    exceeds the allocated capital (#16). `open_cost` is the summed cost of
    positions already open, supplied by the caller because this function is pure;
  - **spendable** — `cash × (100 − reserve_pct)%`, a buying-power reserve.
- The returned value must satisfy, for every possible input:
  `lots × lot_size × price ≤ allocated − open_cost` and `≤ cash`.
- `reserve_pct` holds back a slice of cash so that fees, price movement between
  sizing and fill, and lot rounding cannot turn an approved order into one the
  broker refuses for insufficient funds. **It is a reserve, not an estimate of
  commission**: nothing here predicts what the fee will be, and nothing derived
  from it is ever recorded as a commission. Commission remains what the broker
  reports it charged, and only that.
- `cap_pct` was removed in v1.30. It could never be the binding minimum, because
  `config.load()` refused any configuration where `size_pct` exceeded it — so the
  per-position cap bounded nothing while appearing in the operator's risk summary
  as an active control (#15). The portfolio headroom replaces it with a ceiling
  that can actually bind.

### `zarabot/risk/gate.py`

**`check(signal: Signal, state: PortfolioState, instrument: Instrument, cooldown_active: bool, session_open: bool, halted: bool, now: datetime, config: Config) → RiskDecision`**
- Pure. Calls `risk.sizing` and returns approval with a lot count, or rejection
  with exactly one reason.
- Rejection reasons are evaluated in this fixed priority order, so that the
  recorded reason is deterministic when several apply:
  `HALTED` → `SESSION_CLOSED` → `INSTRUMENT_NOT_TRADING` → `DUPLICATE_TICKER` →
  `MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` →
  `PORTFOLIO_EXPOSURE` → `ZERO_LOTS`.
- `MAX_POSITIONS` applies at or above the configured maximum.
- **`PORTFOLIO_EXPOSURE`** rejects when the summed cost of open positions leaves
  less headroom than one lot: `allocated − open_cost < lot_cost`.
  **It cannot bind on a portfolio the gate sized by itself**, and that is not a
  defect. If every open position cost at most one budget and at most
  `max_open_positions − 1` are open, the surviving configuration bound
  `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100` guarantees headroom for another.
  It binds on holdings the gate did not size: a position adopted by
  `broker.reconcile` during crash recovery, or `ALLOCATED_CAPITAL` lowered
  between runs. Those are precisely the runtime cases #16 names, and the ones a
  configuration-time check cannot see. The gate
  computes `open_cost` from `state.positions`, which carry entry price, lots and
  lot size, so this stays pure and needs no new argument. Before v1.30 the only
  exposure controls were the duplicate-ticker check and a position count, so
  `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100` bounded *nominal* allocation at
  configuration time and nothing bounded it at runtime (#16).
- A **sector or correlation cap is deliberately not implemented yet.** Ten
  positions in ten Russian banks pass every check above as ten independent bets
  and behave in a drawdown as one position at ten times the size — the largest
  unmodelled risk in the system. It is not implemented because it needs a
  ticker→sector grouping supplied as an input (this module must stay pure), and
  on the current four-instrument watchlist, four distinct sectors, it would bind
  on nothing. It becomes required before the watchlist holds two names in one
  sector, and this paragraph is the reminder.
- Rejects any signal whose side is `SELL`, with `ZERO_LOTS`, evaluated in that
  reason's slot rather than earlier — so the side of a signal cannot change which
  reason is recorded for a state where several apply. Exits never pass through
  this module.
- A rejection caused by the **cash reserve** rather than by raw cash surfaces as
  `ZERO_LOTS`, not `INSUFFICIENT_CASH`: `INSUFFICIENT_CASH` is defined on cash
  before the reserve is applied. The distinction is deliberate but makes a
  near-miss on funds read as a sizing result in the rejection statistics, which
  is worth knowing when reading them.
- Must never perform I/O, and must never mutate `state`.

### `zarabot/lifecycle/exits.py`

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int | None, config: Config) → ExitTrigger | None`**
- Pure. Returns the trigger that fires, or `None`.
- `STOP_LOSS` when `price ≤ position.stop_price` **and only when
  `position.stop_protection == 'LOCAL'`**. When the exchange holds the stop, this
  module must never return `STOP_LOSS`: the trigger has exactly one owner at a
  time, and both acting on the same position would sell it twice. Ownership is
  recorded on the position, not inferred.
- `TAKE_PROFIT` when `price ≥ position.target_price`.
- `MAX_AGE` when `trading_days_open ≥ MAX_HOLDING_DAYS` **and**
  `session.in_closing_window(now)`.
- **`trading_days_open` is `None` when the age could not be measured, and then
  `MAX_AGE` never fires (v1.44).** The recorded calendar may not reach back to a
  position's entry after an outage longer than the schedule window, and the
  caller says so rather than passing a number it knows is short. A short number
  reads as a young position, which is the silent failure #45 was: nothing
  raises, the exit simply never comes. `None` keeps `STOP_LOSS` and
  `TAKE_PROFIT` working — they need only a price — and suppresses exactly the
  one trigger that depends on the count.
- The type is `int | None` rather than a sentinel like `-1` because an unmeasured
  age is a different kind of thing from a measured one, and the type is where
  that belongs.
- Precedence when more than one applies: `STOP_LOSS`, then `TAKE_PROFIT`, then
  `MAX_AGE`. Fixed, so the recorded reason never depends on evaluation order.
- Boundaries are inclusive at the stop and the target.
- Must never place an order, and must never consult a halt — an active halt does
  not suppress exits.

### `zarabot/execution/orders.py`

Owns order submission, the submission locks, and crash recovery.

**Observability (v1.61).** This module emits the money-path events of §7.1. It
does not emit `signal_*` (those are `app.loops`; the gate stays pure) and does
not emit `stop_order_executed` / `stop_order_orphaned` (those are
`broker.reconcile`). Each event's extra fields match the table exactly:

- `order_submitting` before the broker call, after the intent row exists
  (`key`, `ticker`, `side`, `intent`, `lots`)
- `order_filled` after settle records a fill (`key`, `ticker`, `filled_lots`,
  `filled_price`, `commission`)
- `order_rejected` on `OrderRejected` (`key`, `ticker`, `intent`, `broker_reason`)
- `order_unresolved` when recovery finds a still-unknown order (`key`, `ticker`,
  `age_seconds`)
- `order_resolved` when recovery settles one (`key`, `resolved_status`, `source`)
- `position_opened` after the position row exists (`position_id`, `ticker`,
  `strategy`, `lots`, `entry_price`, `stop_price`, `target_price`)
- `position_closed` after close (`position_id`, `ticker`, `exit_trigger`,
  `exit_price`, `realised_pnl`, `gap_vs_stop` — the last only for `STOP_LOSS`)
- `exit_failed` when an exit submit fails (`position_id`, `ticker`, `attempt`,
  `error`)
- `stop_order_placed` when a stop is standing (`position_id`, `ticker`,
  `stop_price`, `stop_order_id`)
- `stop_order_cancelled` after a successful cancel (`position_id`,
  `stop_order_id`, `cause`)
- `stop_protection_degraded` when three stop-place attempts fail and the
  position stays `LOCAL` (`position_id`, `ticker`, `attempts`)
- `partial_fill` when filled lots are below requested (`key`, `ticker`,
  `intent`, `requested_lots`, `filled_lots`)

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
- This is the **bot-initiated** exit path, for any trigger. It applies whenever
  the bot decides to leave a position, including `STOP_LOSS` on a position the
  bot itself is protecting.
- When `position.stop_protection == 'EXCHANGE'`: cancels the standing stop order
  first and demotes the position to `LOCAL`, then submits a market sell, settles,
  closes the position, and starts the cooldown. This order is binding — selling
  before cancelling leaves a live stop order against a position that no longer
  exists, which can sell a quantity the account does not hold.
- When `position.stop_protection == 'LOCAL'`: there is no standing stop to
  cancel; submits the market sell directly.
- **A failed cooldown write halts but does not fail the exit (v1.63).** The
  cooldown is written after the sell has executed and after the position row is
  already `CLOSED`, so raising out of `close_position` would report a completed
  exit as failed and the caller would retry a sell that already happened —
  selling a quantity the account no longer holds. Cooldowns are rule 11, so the
  failure takes the existing rule-11 remedy instead: alert and halt, through the
  same path as any other trading-critical write failure. `close_position` then
  returns the closed position, because it did close.
- **Halting is the remedy that fits, not a lesser one.** What a lost cooldown
  endangers is re-entry into the ticker just exited; halting stops the bot
  opening anything at all, which covers that and more. `db.connection` has
  already emitted `db_write_failed` with `critical` true by this point, so the
  event is on the record whatever the caller does next.
- Submits exactly **one** sell order, for the position's whole lot count. Until
  v1.34 it looped until the position was flat, one order per slice, and then
  booked the close from the *last* slice alone — every earlier slice's price and
  commission dropped out of realised P&L, and nothing capped how many orders the
  loop could submit (#10). Both defects go with the loop, which is unreachable
  now that a partial settles as `SUBMITTED` rather than `FILLED`.
- Records `trigger` on the order row via `record_submitting`, so that an exit
  interrupted by a crash can be attributed correctly on recovery.
- Raises `ValueError` for `STOP_LOSS` **only when the position is `EXCHANGE`**.
  There the exchange owns the trigger and selling here would sell the position
  twice; the exchange's own fill is handled by `close_executed_stop` instead.
- The discriminator is **who acts**, never which trigger fired. A `STOP_LOSS` can
  arrive by either path depending on which side owns the stop at that moment,
  and conflating the two leaves a `LOCAL` position with a breached stop that
  nothing is able to sell.
- Raises `ExitFailed` after alerting, when the broker rejects or is unreachable.
  The caller retries on the next cycle. This is the documented exception to the
  no-retry rule.
- Must never be blocked by halt state, cooldown, or any risk limit.

**`async close_executed_stop(position: Position, fill: OrderRecord) → Position`**
- Books the close of a position whose **exchange** stop fired. Never submits a
  sell — the exchange already did.
- **`fill` is the broker's own record of that execution**, obtained from
  `broker.client.get_executed_stop_fills`. The exit price is
  `fill.filled_price` and the exit commission is `fill.commission`. Neither may
  come from a quote.
- Raises `ValueError` when `fill.filled_price` is `None`. There is no fallback
  price: a stop exit with no confirmed fill is not bookable, and the caller
  leaves the position open and retries.
- **Records `fill.key` as the order row's `broker_order_id` (v1.39).**
  `get_executed_stop_fills` returns records keyed by the broker's
  `exchange_order_id`, so the identifier is already in hand; the local row's own
  `key` is a UUID this module invented and the broker has never seen. Writing it
  down is what makes a late commission on this row recoverable at all (#8).
- A `fill.commission` of `None` does **not** block the close. Unlike the price,
  the commission is a correction rather than the substance of the exit, and
  refusing to book would leave a position the broker has already closed open
  locally until reconciliation found it and recorded it as `EXTERNAL` — a
  stop-out filed under the wrong trigger, which corrupts the exit-trigger
  distribution permanently. It is booked with the commission unknown, netted as
  zero, and corrected by `ops.commissions` when it lands.

Until v1.28 this function took a `Decimal` fill price, and `app.loops` passed it
the value from `get_last_price` at the top of the cycle — the market price at the
moment of *detection*, up to a poll interval after the fill, and on a gap-down
open potentially far from what the broker actually got. Every stop-loss exit's
realised P&L was wrong, and the weekly report's gapped-exit section measured a
difference the bot had manufactured rather than slippage the market caused
(#4).

**Partial fills (v1.34).** Since v1.27 the broker layer reports a partial as
`SUBMITTED`, not `FILLED`, so a partial never reaches the code that books a
close. That one change removed both of the defects #10 named in this module — the
exit loop that sliced, and the aggregation it would have needed — and exposed a
third that had been hiding behind them: nothing decided what to *do* with the
partial. This is that decision.

**On entry.** A `post_market_order` returning `SUBMITTED` with `filled_lots > 0`
is a live order holding shares the bot has no position row for and no stop
against. The remainder is abandoned — the strategy's entry price is stale by then
and topping up would breach the one-open-position-per-ticker invariant — but
abandoning it means *cancelling* it, not ignoring it:

1. `broker.client.cancel_order(key)`.
2. `broker.client.get_order_state(key)` — the settled truth. The lots, price and
   commission written down come from this read and never from the pre-cancel
   response, which was already stale when it arrived (rule 33).
3. Re-read shows `filled_lots > 0` → settle the order `FILLED` for those lots,
   open the position for them, size stop and target from the achieved price,
   place the stop for that quantity, and alert. The alert is not optional: on a
   watchlist chosen for liquidity, a partial says the instrument is thinner than
   the watchlist assumes.
4. Re-read shows nothing filled → settle `CANCELLED`; open no position.
5. Either call fails → **write nothing**. The order stays unresolved and
   `resolve_unfinished` repeats this sequence on the next cycle. An unresolved
   order with shares behind it is recoverable; a position row written from a
   number nothing confirmed is not.

A `SUBMITTED` response with `filled_lots == 0` is **not** cancelled. Nothing is
held, so nothing is unprotected, and cancelling a market order that is merely
pending would turn every slow fill into a missed entry. It is left unresolved and
settled by `resolve_unfinished`, which is where a zero-fill order was already
settled.

**On exit.** One sell order per `close_position` call, for the position's whole
lot count. There is no loop: a partial settles nothing, so there is no second
iteration to reach and no unbounded submission to cap. `close_position` raises
`ExitFailed` and the caller retries on the next cycle — rule 4's existing
behaviour, needing no exception of its own.

**A terminal exit that sold only part of a position reduces the position to the
unsold remainder and leaves it open.** When `resolve_unfinished` settles an
`EXIT` order as `CANCELLED` or `REJECTED` with `0 < filled_lots < position.lots`,
it calls `db.positions.update_lots` with the remainder and alerts, naming the
ticker, the lots sold, the lots left and the price. It does **not** close the
position. The position is not flat, and closing it would leave shares at the
broker with no local row — which the next reconciliation reports as a foreign
holding and `app.startup` then refuses to start on (rule 32). A `filled_lots` at
or above the position's count does close it: that is the race where a cancel
lands after a full fill, and the exit really did complete.

The sold slice's profit or loss is therefore **not booked**. That gap is
deliberate, and the alternative is worse: `db.positions.close` computes realised
P&L for a whole position against one exit price, and feeding it a blended figure
would write down a price no order achieved. The remaining lots still mark to
market against the original entry price, so only the sold slice's contribution is
missing from `pnl.bot_equity`, bounded above by one position's stop loss —
`position_size_pct × stop_loss_pct` of allocated capital, comfortably inside the
daily loss limit's own margin. It is visible in the alert and permanently in
`position_events` as a `LOTS_ADJUSTED` row. A gap that is bounded, alerted and
recorded is a different kind of thing from a wrong number that looks right.

**`async resolve_unfinished(now: datetime) → list[OrderRecord]`**
- For every unresolved order, queries `broker.client.get_order_state` by key and
  settles it; `OrderNotFound` settles it as never-placed.
- Opens or closes the corresponding position when a fill is discovered. A
  discovered **exit** fill closes the position with the `exit_trigger` recorded
  on its order row. It must never fall back to a default trigger: a guess here
  writes a permanent, plausible-looking lie into the trade history. A row with
  `intent = 'EXIT'` and no trigger is a data defect — alert and leave the
  position open for the owner to resolve.
- **A discovered entry fill with no matching signal is attributed to
  `UNATTRIBUTED`, never to a strategy (v1.35).** It was attributed to
  `ma_crossover` — a real strategy whose weekly figures decide whether it stays
  enabled — so every crash-recovered trade biased the evidence for one named
  strategy, systematically and always in the same direction (#11).
  `UNATTRIBUTED` is a sentinel in the same family as `ADOPTED`: the `positions`
  schema already accepts it, `telegram.commands` iterates the *enabled*
  strategies and so never shows it under one, and `reporter.weekly` groups by the
  stored name and so shows it under a heading of its own. It is reported, and it
  is never credited.
- **The signal lookup spans the order's life, not one calendar date.** It reads
  `db.signals.list_for_period(moscow_date(order.created_at), moscow_date(now))`.
  Searching only today's Moscow date meant an order that filled at 23:58 MSK and
  was recovered at 00:05 could never match the signal that produced it — the case
  where recovery matters most was the one it failed on.
- The reconstructed signal's `reference_price` is the order's `filled_price`.
  There is no `Decimal("0")` fallback: this path is reached only for an order
  that filled, and a zero reference price would be a second invented number on
  the same few lines as the first.
- Applies the entry cancel-and-re-read sequence above to any `ENTRY` order the
  broker still reports as `SUBMITTED` with lots filled, and reduces the position
  to its unsold remainder for any terminal `EXIT` order that sold part of it
  (v1.34). Both are the same principle as the rest of this function: an order
  whose outcome is uncertain is resolved by asking the broker, and only what the
  broker answers is written down.
- Ordering constraint: completes before any new order is submitted in the
  process's lifetime.
- Must never resubmit an order.

### `zarabot/state/halt.py`

**Sole owner of the halt flag.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31). This module is not a `db.*` repository, but it
was one of the eight sites opening its own connection and it owes the same
obligation.

**`async is_halted() → bool`** · **`async current() → HaltState | None`**

**`async halt(reason: HaltReason, detail: str, at: datetime, daily_loss_pct: Decimal) → None`**
- Persists the halt so it survives a restart. Idempotent when already halted
  **for the same or a more severe reason**.
- **Severity order: `DAILY_LOSS_LIMIT` > `RECONCILIATION_MISMATCH` > `MANUAL`.**
  A halt for a strictly more severe reason replaces a weaker one, rewrites the
  detail and re-alerts. Returning early regardless of reason meant a daily-loss
  breach arriving during a manual halt was silently discarded, so `/resume`
  cleared a halt whose real cause nobody had been told about (#9).
- Suspends **entries only**. Never affects `lifecycle.exits` or
  `execution.orders.close_position`.
- **Emits `halt_triggered` (CRITICAL) after a halt is persisted or upgraded,
  with `reason`, `detail`, and `daily_loss_pct` (v1.61).** `daily_loss_pct` is
  a required argument of `halt` (`Decimal`); callers that already computed the
  day's loss pass it, and `/halt` passes the current figure from `pnl`.
  `risk.gate` stays pure and emits nothing.

**`async resume(actor: str, at: datetime) → bool`**
- Clears the halt, recording who cleared it. Returns `False` when not halted.
- **Emits `halt_cleared` (INFO) with `actor` when a halt was actually cleared
  (v1.61).** A no-op resume emits nothing.

### `zarabot/pnl.py`

**`realised(position: Position) → Decimal`** · **`unrealised(position: Position, price: Decimal) → Decimal`** — both net of commission.

Commission is the **actual figure reported by the broker** via
`broker.client.get_operations`, recorded on the order row when the order settles.
It is never estimated from a rate. On a small account, commission is a
material fraction of a 10% move, and an estimated figure would make every
realised P&L slightly and permanently wrong.

**`async bot_equity() → Decimal`**
- `allocated_capital + realised P&L of every closed position + unrealised P&L of
  every open position at current prices`.
- **Never reads broker cash or broker equity.** That is the whole point: the
  broker's equity moves when money is paid in or taken out, and those movements
  are not trading results. Reading them made a withdrawal look like a loss large
  enough to halt trading, and a deposit mask a real one (#9).

**`async daily_loss_pct(now: datetime) → Decimal`**
- `(opening bot equity − bot equity now) / ALLOCATED_CAPITAL × 100`. Positive
  means a loss.
- **The denominator is allocated capital**, the money actually at risk — not
  account equity. On an account holding twice the allocation, dividing by equity
  let a "5% daily limit" permit a 10% loss of the capital the bot was given
  (#9). `DAILY_LOSS_LIMIT_PCT` now means what an operator reads it to mean:
  a percentage of what they handed the bot.
- **The baseline is bot equity at the session open**, written to
  `daily_snapshots.opening_equity` when the session opens rather than lazily on
  whichever call happened to be first. A process that started at 14:00 previously
  seeded the baseline at 14:00 and was structurally blind to the morning's
  drawdown, and returned zero on the call that established the day — so the limit
  could not trip on the cycle that created it.
- **When no snapshot exists for the day** — the bot started mid-session and
  missed the open — the baseline is reconstructed as
  `allocated_capital + realised P&L of every position closed before today`, and
  the reconstruction is alerted once. It is not exact: unrealised movement on
  positions carried overnight is attributed to today. That direction is
  deliberate, because it makes the limit tighter rather than looser, and a limit
  that halts early is recoverable by `/resume` while one that halts late is not.

**Interaction with an existing halt.** `state.halt.halt()` returns early when
already halted, so a `DAILY_LOSS_LIMIT` breach arriving during a `MANUAL` halt
was discarded — the more serious reason and its detail lost. A halt reason of
strictly greater severity must replace a weaker one and re-alert;
`DAILY_LOSS_LIMIT` outranks `MANUAL` and `RECONCILIATION_MISMATCH`. **`halted_at`
keeps its original value across an upgrade**: trading has been suspended
continuously since the first halt, and moving the timestamp forward would assert
it was live in between. The moment the more severe condition arrived reaches the
owner in the alert. Re-halting
for a reason already recorded stays a no-op, so this adds no alert noise.

**`async benchmark_return(start: date, end: date) → Decimal | None`**
- Buy-and-hold return over the watchlist for the period.
- Returns `None` when any constituent price is missing — an unavailable benchmark
  is reported as unavailable, never as zero.

### `zarabot/telegram/notifier.py`

**`async alert(text: str, urgent: bool = False) → None`**
- Sends to the configured chat. Retries on failure, then logs and returns.
- **After the last failed attempt, emit `telegram_send_failed` (WARNING) with
  `attempt` and `error` (v1.61).** Never raises.
- **When a body contains a configured secret, drop it, emit `secret_redacted`
  (ERROR) with `sink` equal to `telegram` — never the secret, never its length —
  and send a substitute incident alert without the secret (v1.61, rule 19).**
- Never includes a token or account identifier in a message.

### `zarabot/telegram/commands.py`

One handler per command in the brief's command table.

- Every handler first checks the sender against `TELEGRAM_CHAT_ID`; a mismatch
  emits `unauthorised_command` (INFO) with `chat_id` and `command` (v1.61), then
  returns without replying and without any state change. `chat_id` is not a
  brokerage secret; it is the field that makes the event answerable.
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
- **On a successful send, emit `weekly_report_sent` (INFO) with `period_start`
  and `period_end` (v1.61).** A failed send emits nothing of this name.

### `zarabot/app/startup.py`

**`async start() → AppContext`**

Fixed ordering; each step completes before the next begins:
1. `config.load()` — abort on failure before anything else, including any network call.
   **When `load()` raises `ConfigError`, this module is still the owner of
   `config_invalid` (v1.61).** Logging is not configured yet. `start()` therefore
   calls `logging_setup.configure` with whatever token and account-id values are
   already in the environment (empty list if none), emits `config_invalid`
   (CRITICAL) with `variable` from the `ConfigError`, then raises `StartupError`.
   It never proceeds to a broker call. `config` itself does not emit the event:
   it has no logger of its own by design.
1b. Write `SSL_TBANK_VERIFY` into the process environment from
   `config.ssl_tbank_verify`. This must precede every broker call; a channel
   created before it is set fails its TLS handshake.
1c. **When `ssl_tbank_verify` is false, alert the owner before the first broker
   call**, saying that certificate verification is disabled on the connection
   carrying the trading token. `config` logs it; a log line on a server nobody
   is watching is not a security control. The alert must never contain the
   token.
2. `logging_setup.configure()`, passing every token and account identifier on
   the loaded `Config` as `secrets`.
3. `db.connection.connect(config.db_path)`, then
   `db.migrations.apply(db.connection.shared())`. The connection is opened here —
   not at import, and not inside a repository — and `apply` receives the shared
   connection rather than opening a second one.
4. `strategies.registry.enabled()`, including model load if configured.
5. `market.session.refresh()`.
6. `execution.orders.resolve_unfinished()`.
7. `broker.reconcile.reconcile()`, then apply its remedies via
   `execution.orders`: re-protect unprotected positions, cancel orphaned stops,
   replace mispriced ones, and **resolve duplicates — for a `STOP_DUPLICATE`
   adjustment, cancel every identifier in its `cancel` list and retain `keep`.**
   Reconciliation identifies; the executor acts. Duplicates are cancelled
   through `execution.orders.cancel_orphaned_stop`, which is the only cancel
   primitive that module exposes; a duplicate settles as `ORPHANED` as a result,
   which is imprecise — it was a duplicate, not an orphan. A dedicated primitive
   belongs with the `execution.orders` batch rather than as a change made in
   passing to the module that moves money. The behaviour is safe meanwhile:
   `cancel_orphaned_stop` demotes a position to `LOCAL` only when the cancelled
   stop is the one recorded in `stop_order_key`, and that is the stop
   reconciliation chose to keep, so the retained stop is never disturbed.
   **An identifier in `cancel` that matches no known stop alerts and the sequence
   continues** — skipping it silently would reproduce #35 exactly, and refusing
   to start would leave a duplicate standing rather than remove the ones that can
   be removed. **Every adjustment type the report can carry is handled here.** An adjustment with no branch is silently
   dropped, which is what happened to `STOP_DUPLICATE`: the double-sell condition
   was detected, reported, and then ignored, and the ready alert counted it as
   one more adjustment (#35). An unrecognised adjustment type must alert rather
   than pass, so a report the executor does not understand is loud.

7a. **`EXIT_UNRESOLVED` is observed, not remedied, and does not stop startup
   (v1.37).** It is a position the broker no longer holds whose sale could not
   be found in the operations feed, so there is nothing for `execution.orders`
   to do about it — the shares are already gone. It belongs with
   `CLOSED_EXTERNALLY`, `ADOPTED`, `LOTS_ADJUSTED` and `FOREIGN_HOLDING` in the
   set of types this step recognises without acting on, precisely so it does not
   trip the "adjustment types this build cannot act on" alert, which is reserved
   for a report the executor genuinely does not understand.

   Its consequence is named here rather than left to be discovered: the position
   stays open, so if it was `EXCHANGE` the same report will carry `STOP_MISSING`
   for it and this step will try to place a stop against shares the account does
   not hold. The broker refuses, the position stays `LOCAL`, and the owner is
   alerted — the degrade path rule 23 already defines. That is noisy and correct;
   the alternative was a fabricated exit price written permanently into the trade
   history.

7b. **Refuse to start on a `FOREIGN_HOLDING` adjustment**, unless
   `config.allow_foreign_holdings` is true. Raise `StartupError` naming every
   ticker reported, after alerting. The account is the bot's alone (brief v1.8),
   and a holding the bot does not recognise means either that someone traded in
   it by hand or that local state is wrong — and the bot cannot tell which. When
   the flag is set, the holdings are named in the ready alert instead and are
   never traded: no stop is placed, no exit is evaluated, no sale is made.
   Refusing is the correct failure direction. The alternative failure is selling
   something the owner chose to hold, at a price they did not choose.
8. Restore halt state.

8a. **Report a position budget that cannot buy one lot.** For each ticker in
   `config.watchlist`, read the instrument and its last price, and compare
   `instrument.lot × price` against
   `risk.sizing.position_budget(config.allocated_capital, config.position_size_pct)`.

   - When **no** watchlist instrument is affordable, alert the owner that the bot
     cannot open a position in anything it is watching, naming the budget and the
     cheapest lot cost found.
   - When **some** are affordable, name the unaffordable ones in the ready alert
     of step 9 rather than raising a separate alert. A partially reachable
     watchlist is a normal operating condition — an instrument's price rises
     through the budget without anything being wrong — and must not train the
     owner to ignore the channel.
   - A ticker whose instrument or price cannot be read is **excluded from the
     judgement and named separately**, never counted as affordable and never
     counted as unaffordable. If no price could be read for any ticker, the check
     is **inconclusive** and says so; it must not report a blackout it did not
     observe, and it must not stay silent as though it had confirmed health.

   **This step never raises `StartupError`, and never prevents startup.** An
   unaffordable budget stops *new entries only*. Refusing to start would
   additionally abandon every open position — no exit evaluation, no stop
   management, no `MAX_AGE` — converting a benign no-op into an unmanaged holding
   with real money in it. The bot must keep running to protect what it holds.

   It runs after step 7 so reconciliation has settled position truth first, and
   before step 9 so the finding can be folded into the ready alert. A broker
   failure in this step is alerted and startup continues; this is a diagnostic,
   and it must never be the reason the bot is not running.

9. Alert the owner that the bot is running, reporting version, mode, halt state
   and any reconciliation adjustments, **and emit the `startup_ok` log event of
   §7.1 carrying the same four facts** — `version`, `mode`, `halted`,
   `adjustments_count` (v1.58). The event is stated here, in the contract of the
   module that owes it, because §7.1 is a table of formats and a module never
   reads it as a work item: `startup_ok` was specified there from the first
   version and emitted by nothing, while `scripts/deploy/update.sh` greps the
   container's logs for it as its health gate and would have rolled back every
   deploy it ever ran. The Telegram alert and this event are deliberately
   redundant: one is for a person who may be asleep, the other for a deployer
   that cannot read Telegram.

- Raises `StartupError` on any failure, having alerted if Telegram credentials
  were valid. No entry may be attempted before step 9 completes.
- **On any `StartupError` after logging is configured, emit `startup_failed`
  (CRITICAL) with `stage` (the step name: `config`, `logging`, `database`,
  `strategies`, `session`, `recovery`, `reconcile`, `halt`, `ready`) and
  `reason` (v1.61).** No `startup_ok` on that path. `__main__` does not emit
  either event; it only sleeps and exits.

### `zarabot/app/loops.py`

**`async trading_cycle(ctx: AppContext) → None`** — one iteration, in this fixed order:
1. If the session is closed, return without any broker call.
2. Refresh prices for open positions, and poll standing stop orders for
   execution. A stop filled by the exchange closes its position here.

   **Execution is confirmed, never inferred.** The cycle calls
   `broker.client.get_executed_stop_fills` once, over the window from the start
   of the current Moscow trading day to now, and closes a position **only** when
   that result contains the `stop_order_id` recorded in its own `stop_orders`
   row. Matching is on that persisted broker identifier, never on the UUID we
   generated: `list_stop_orders` builds its key from `order_request_id` when the
   broker supplies one and from `stop_order_id` otherwise, so our UUID may match
   nothing even for a stop that is perfectly alive.

   The previous rule concluded that a stop had fired from **two absences** — the
   stop missing from the active list, and the ticker missing from the portfolio —
   each an eventually-consistent read, and correlated rather than independent
   when the broker hiccups. A false positive closed a live position at an
   invented price, started a cooldown on an instrument the bot still held, and
   left the shares to be re-adopted as a fresh position at a new cost basis: one
   phantom round trip in the P&L from two reads that merely lagged (#5).

   **An absence is a discrepancy, not an exit.** A position whose stop is no
   longer live and for which no execution is confirmed stays open, and is
   alerted once so reconciliation and the owner can see it. The window is
   re-queried every cycle, which is harmless: a position already closed is not
   reconsidered.
   **A `PriceRejected` for one position omits that ticker and continues with the
   rest** — it must not abort the cycle, and must not count toward the
   consecutive-failure outage alert, which exists for a broker that cannot be
   reached. Positions with no price are skipped by the exit evaluation that
   follows, which already tolerates a missing entry. Rejections **latch**, like
   every other alert in this module: one alert when a cycle first rejects
   anything, naming the count, and none further until a cycle rejects nothing
   and re-arms it. An alert every cycle would be roughly 510 messages in an
   8.5-hour session for one permanently stale instrument, and the brief is
   explicit that a bot which cries wolf gets muted, and a muted bot is
   unmonitored. The count still matters — every price rejected at once is a
   different event from one instrument going quiet — so it is named in the alert
   that does fire.
3. Evaluate the remaining exits — take-profit, maximum age, and stop-loss only
   for `LOCAL`-protected positions — and submit them. **Before** any halt check,
   and before entries.
4. Recompute daily P&L; halt if the daily loss limit is breached.

   **Write the day's opening snapshot on the first in-session cycle of a day,
   and only when this process was already running when the session opened.** A
   bot restarted at 14:00 finds the snapshot written that morning and measures
   against it. A bot whose *first* cycle is at 14:00 writes **nothing** and lets
   `pnl` reconstruct the baseline.

   That second case is the whole point, and it is easy to get backwards. Writing
   at 14:00 would store bot equity as of 14:00 — a figure that already contains
   the morning's losses — so the baseline would hide exactly the drawdown the
   limit exists to catch. That is #9 restated, not fixed. `pnl`'s reconstruction
   is deliberately tight and alerts; a late snapshot is loose and silent, and on
   a limit that bounds real money the tight, loud option wins.

   **When the loss cannot be measured, do not trade on.** `pnl.bot_equity` marks
   open positions to market, so a `PriceRejected` or `BrokerUnavailable` on any
   one of them makes the day's loss unknowable rather than merely imprecise. The
   cycle then **skips entries for that cycle and alerts, latched**, without
   halting: exits have already run at step 3 and must not be blocked, and a
   halt would persist past a condition that is usually momentary. This is
   deliberately stricter than step 2, where one rejected quote omits its ticker
   and the cycle continues — there, a missing price costs one position's exit
   evaluation; here it costs the measurement that bounds the whole day.
4b. If shutdown has been requested, return; entries stop here and exits do not
   (v1.45). The same shape as the halt check below, for the same reason: a
   process on its way down must not open what nobody will be watching, and must
   not be stopped from closing what is already open.
5. If halted, return; entries stop here.
6. Fetch candles, evaluate strategies, and pass each signal through the gate.
7. Record every signal with its decision; execute the approved ones.

   **`signal_generated` / `signal_rejected` are emitted here (v1.61), not in
   `risk.gate`.** The gate stays pure. Each non-`None` strategy result logs
   `signal_generated` (`ticker`, `strategy`, `reference_price`) before the gate
   runs; a rejected decision logs `signal_rejected` (`ticker`, `strategy`,
   `rejection_reason`). Approved entries that submit are not a second
   `signal_generated`.

   **After a close that starts a cooldown, emit `cooldown_started` (`ticker`,
   `active_until`) (v1.61).** `execution.orders` owns the cooldown write;
   this module owns the event because the gate cannot log.

   **A ticker already opened earlier in this same pass is skipped before the
   gate, and recorded as `DUPLICATE_TICKER` (v1.40).** Strategies are looped
   outer and tickers inner, so two strategies can signal one ticker in a single
   pass. The gate's duplicate check reads `state.positions` from
   `get_portfolio()`, and the broker's portfolio lags a market order that has
   only just filled, so the second signal could pass a gate that was working
   correctly. `execution.orders.open_position` then refused it — also correctly —
   with `PositionStateError`, which nothing in this module caught (#24).

   The set of tickers opened in the pass lives here, not in `risk.gate`, which
   stays pure. It is the local record, which is immediately consistent, deciding
   a question the broker's eventually-consistent one cannot answer in time.

   **A refused entry is an ordinary outcome, not a fault.** `OrderRejected`,
   `PositionStateError` and `db.orders.DuplicateOrderError` are all caught here,
   logged, and the pass continues to the next ticker. Only `OrderRejected` was,
   so a correct refusal propagated to `run`'s supervisor, which logged
   `task_crashed`, alerted **"Background task trading crashed"**, slept, restarted
   the loop — and skipped every remaining ticker in the pass. The guard was
   doing its job; the caller was routing its success through the crash path.

   The portfolio is still re-read from the broker after each successful open.
   That read is what keeps `MAX_POSITIONS`, `PORTFOLIO_EXPOSURE` and available
   cash correct within a pass, and dropping it to save a call would trade a
   spurious alert for a breached risk limit. Opens are rare; the per-cycle cost
   #19 is about is elsewhere.

7b. **A position whose entry the recorded calendar does not reach is evaluated
   with `trading_days_open = None`, and the owner is alerted, latched (v1.44).**
   `market.session.covers` answers the question. Stop-loss and take-profit still
   evaluate normally — only the age trigger is suppressed, and only for that
   position.

   **The latch re-arms when a cycle measures every open position (v1.48).** It
   was set and never reset, so the alert fired once per process and a second
   occurrence after recovery was silent — which is #32 exactly, reintroduced in
   this module hours after #32 was closed for it. The condition is not
   permanent: it clears as soon as the recorded calendar reaches back far
   enough, which after an outage is the next refresh.

   It takes the shape of `_prices_for`'s rejection latch, deliberately: one alert
   when a cycle first cannot measure an age, **naming the count**, and none until
   a cycle measures them all. Re-arming per position would let one covered
   position clear a latch while an uncovered one is still suppressed, so the
   whole cycle is the unit.

8. The calendar handed to `lifecycle.exits` comes from `market.session.calendar()`
   (v1.40), never from a fetch of this module's own. It fetched a fourteen-day
   schedule **every cycle** — once a minute, for data that changes at most daily
   and that `market.session` already held, refreshed daily by task 3 of `run`
   (#19). On a broker error that fetch returned an *empty* calendar, so
   `clock.trading_days_between` counted zero and `MAX_AGE` exits silently stopped
   firing; reading the cache cannot reach that state.

Steps 3 and 4 running before step 5 is what implements the brief's
halt-blocks-entries-only rule, and their order is binding.

**Scheduling is "due and not yet done", recorded in the database (v1.45).**
Every periodic job — rollover, backup, weekly report, schedule refresh,
heartbeat — asks `db.job_runs.has_run(job, period_key)` and records completion
with `mark_run`. Two defects go with that change (#27):

- **Exact-hour matching is gone.** The weekly report fired only if the loop
  observed an instant inside the 12:00–12:59 MSK hour on a Sunday. A process
  down, restarting, or backing off through that hour skipped the week entirely,
  with no report, no alert and no record — against acceptance criterion 9. It is
  now due from 12:00 MSK Sunday onward and runs the moment the process is up. A
  report delivered at 14:00 after a restart is strictly better than none.
- **Schedule state survives restarts.** It lived in module globals, so every
  restart re-armed every job. Three restarts in a day produced three heartbeats
  and could repeat a rollover; a restart through the report hour lost the week.
  Restarts are routine — six in one evening during the #45 work — so this is the
  ordinary case, not an edge one.

**Back-off takes the broker's hint when the broker gives one (v1.53).** After a
failed cycle the delay escalates as rule 1 describes; when the failure was a
`BrokerRateLimited` carrying `retry_after`, the delay is **the longer of** that
hint and the escalation, capped by the same maximum. A hint shorter than the
escalation changes nothing — the escalation already reflects how many cycles
have failed — and a hint beyond the cap is bounded by it, because this loop
submits exits and no external number may hold it asleep.

The hint is overwritten on **every** failure, not only on the ones that carry
it, and cleared by a successful cycle alongside the failure counter. A hint
remembered from a rate limit two cycles ago would otherwise still be delaying a
plain outage, which is stale state wearing the shape of a measurement (rule 36).

**Not every latch moves.** `_first_cycle_at` stays process-local and must: it
means "was *this process* running when the session opened", which is exactly
what decides whether the day's opening snapshot may be written (step 4).
Persisting it would let a restarted process claim an origin it did not have. The
alert latches stay process-local too — they suppress repetition within a run,
and a restart is a reasonable moment to speak up again.

**There is no separate "overdue" alert**, because due-and-not-yet-done removes
the condition it would guard: a job is no longer skipped, only run late. The
weekly report carries its own timestamp, so lateness is visible in the artefact
rather than in a second alert with a threshold nobody chose.

**`async run(ctx) → None`** — **the sole owner of composition.** Every
long-running task in the system is started here and nowhere else, and this list
is exhaustive:

1. the trading cycle
2. the daily rollover
3. **the trading-schedule refresh** (see `market.session`)
4. the commission backfill — at rollover, and immediately before the weekly
   report so the report is never composed from figures a pending commission
   would move
5. the nightly backup
6. the weekly report
7. the heartbeat
8. **the Telegram command listener**, via
   `telegram.commands.build_application()`

A module whose entry point appears in no list is dead code that passes its own
tests. `telegram.commands.build_application` was defined, covered by tests, and
never called — so `/halt` did not exist at runtime and the kill switch was
unreachable, while every test was green. Anything added to this system that must
run continuously is added to this list in the same change, or it does not run.

A failure in one task must never terminate another; each is supervised and
restarted with backoff. A failure in one task must never terminate another.

**Observability of the supervisor (v1.61):**
- Each heartbeat job emits `heartbeat` (INFO) with `uptime_seconds`,
  `open_positions`, `halted`.
- A supervised task that raises emits `task_crashed` (ERROR) with `task`,
  `error`, `restart_in_seconds` before the backoff sleep.
- When `clock.now()` is not UTC-aware, emit `clock_drift` (WARNING) with
  `drift_seconds` 0 and refuse to run the cycle — a naive "now" is a contract
  violation, not weather (v1.61). Do not call `datetime.now()` here to "check"
  the clock; that would itself violate the clock-ownership rule.

### `zarabot/app/shutdown.py`

**`async shutdown(ctx, signal) → None`**
- **Stops entries before it drains, and now actually does (v1.45).** It calls
  `app.loops.stop_entries()` first, then settles. This contract and the
  function's own docstring both claimed it stopped accepting new signals, and
  nothing implemented that half: `shutdown` ran as a task *concurrently with*
  `run`, and the runner was cancelled only after the drain returned, so for the
  whole thirty-second window the trading loop kept cycling and could open a
  position the drain had already looked past (#21).
- The flag is process-local and deliberately does **not** persist: a restarted
  process must accept entries again. It is the one piece of loop state that
  would be wrong to keep in `db.job_runs`.
- Exits are unaffected. A cycle already past the entry check completes and its
  order is drained; the flag closes the window before the *next* entry.
- Stops accepting new signals, waits for in-flight submissions to reach a known
  state or a bounded timeout, settles what it can, records state, calls
  `db.connection.disconnect()` and `broker.client.close()`, and exits. Closing the
  database means that call and nothing else — no repository closes a connection
  it did not open — and closing the broker channel likewise belongs to the module
  that owns it.
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
- **It emits no log event of its own (v1.61).** `startup_failed` / `config_invalid`
  belong to `app.startup`, which must have emitted them before raising.

### `zarabot/ops/commissions.py`

Fills in commissions the broker reported after the fill, and corrects the P&L
that depended on them.

**`async backfill(since: datetime, until: datetime) → int`**
- For every order from `db.orders.list_missing_commission`, re-queries the
  broker and records any commission now present. **By `broker_order_id` through
  `get_order_state_by_broker_id` when the row has one, and by our own `key`
  through `get_order_state` otherwise** (v1.39) — either way by an identifier,
  never by matching on instrument, time and quantity, which is ambiguous exactly
  when two similar orders are close together.
- Recomputes `realised_pnl` via `db.positions.recompute_realised` for every
  closed position whose orders changed, and returns the number of orders updated.
- Alerts only when an order's commission is still unknown more than 24 hours
  after its fill: that is a broker or integration problem, not ordinary lag.
- **Alerts once per order, not once per run** (v1.39). Before alerting it checks
  `commission_alerted_at`, and after alerting it calls
  `db.orders.mark_commission_alerted`. `backfill` runs daily from the rollover
  loop and again before every weekly report, so the same row alerted on every
  run — indefinitely, once per stop-loss exit ever taken — into a channel whose
  whole design premise is that silence means healthy (#8). An alert that repeats
  forever is equivalent to no alert.
- It keeps **re-querying** an alerted row. The terminal state is on the telling,
  not on the trying: the number is still worth having if it arrives.
- The 24-hour threshold lives here. `db.orders` records only whether the owner
  has been told.
- Must never place, cancel or modify an order.

### `zarabot/ops/backup.py`

**`async run(db_path: Path, backup_dir: Path) → Path`** — produces a consistent copy using SQLite's own backup mechanism, never a raw file copy of a live database.
- **On success, emit `backup_ok` (INFO) with `path` and `bytes` (v1.61).** On
  failure, emit `backup_failed` (ERROR) with `error`, alert, and return; it must
  never stop trading.

**`async prune(backup_dir: Path, retention_days: int) → int`** — removes backups strictly older than the window; returns the count removed.

- Failure alerts and returns; it must never stop trading.

### `sandbox/` — laptop research (never imported by server code)

**`async load(ticker: str, start: datetime, end: datetime, interval: CandleInterval, cache_dir: Path = Path("sandbox/cache")) → list[Candle]`**
- Async, because it calls `broker.client` on a cache miss. In a notebook this is
  awaited directly.
- `start` and `end` are timezone-aware; a naive value raises `ValueError`.
- Returns candles oldest-first, empty list when the range holds none.
- Caches to `<cache_dir>/<ticker>_<interval>.parquet`, writing through after a
  fetch. `sandbox/cache/` is gitignored: it is derived data, and committing a
  year of candles would bloat the repository for no benefit.
- A cached range that does not cover the request is extended by fetching only
  the missing span, never by refetching the whole range.
- Must never be imported by `zarabot/`.

**`sandbox/exchange.py` — a simulated broker (v1.46)**

A double for every function of `broker.client` the trading cycle calls, backed
by historical bars. It is the piece that makes a backtest mean something,
because with it the simulation does not *resemble* the live path — it **is** the
live path, with only the broker and the clock replaced.

- **`SimulatedExchange(bars, instruments, cash, slippage, commission)`** holds
  simulated cash, holdings, submitted orders and standing stop orders, and a
  cursor into the bars. `advance(moment, phase)` moves the cursor and reports
  that **phase** of each instrument's current bar as its last price — `OPEN`,
  `LOW`, `HIGH` or `CLOSE`, defaulting to `CLOSE`.

  A phase rather than a price (v1.50): the marks are per instrument, and a
  backtest runs the whole watchlist, so a single price passed by the caller
  would report one ticker's low as every ticker's. The exchange holds the bars
  and is the only thing that can resolve a phase per instrument. Standing stops are checked **once per bar**, on first entry to it, so
  four cycles do not become four chances to fire.
- It exposes `get_candles`, `get_last_price`, `get_instrument`, `get_portfolio`,
  `get_max_lots`, `get_order_state`, `post_market_order`, `post_stop_loss`,
  `cancel_stop_order`, `cancel_order`, `list_stop_orders`,
  `get_executed_stop_fills` and `get_trading_schedule` with the signatures and
  the failure types `interfaces.md` records for the real ones. Where the real
  module raises, this raises the same exception.
- **The seam table covers four kinds of escape, and the guard checks all
  four (v1.48).** Broker, clock, configuration **and alerts**. It patched
  `alert` in four modules while eleven import it, and `state.halt` — which
  `trading_cycle` reaches on the daily loss limit — was not among them, so a
  backtest run where credentials happen to be present sent real messages to the
  owner (#49). `telegram.notifier.alert` is patched at its source as well as in
  each importer, so a module that starts importing it later is covered by
  default.
- **A guard that covers one class of escape reads as covering all of them.**
  The guard test asserted only that no broker call escaped, while describing
  itself as proving the table complete. It now asserts, for a whole run, that
  no real alert is sent, no unpatched clock is read and no configuration is
  loaded from the environment. This is the same omission-reads-as-compliance
  shape as the defect #12 was closed for, reproduced inside #12's own fix.
- **`market.session` is driven, not stubbed.** The simulator answers
  `get_trading_schedule`, and the real `refresh` / `is_open` / `calendar` /
  `covers` run on top. A backtest that stubbed those would not exercise the
  code that decides whether the market is open, which is where #39 and #43
  lived.

**Fill model.** The four rules below are where a backtest is honest or is not:

1. **Decide at a bar's close, fill at the next bar's open.** A strategy sees
   bars up to and including the one just closed, and the order it produces is
   **priced at the next bar's open**. This removes look-ahead completely: the
   price the order gets was not knowable when the decision was made. It is
   *conservative relative to live*, which polls intra-day and can act within the
   bar — that gap is #13, and it is now a measurable difference rather than a
   hidden one.

   The fill is returned **synchronously**, on the cycle that submitted it, even
   though its price comes from the following bar (v1.47). Holding the order
   `SUBMITTED` until the cursor advanced was the first design and it was wrong:
   `execution.orders.open_position` treats an unfilled submission as an unknown
   outcome and raises `BrokerUnavailable`, so **every** entry would have gone
   through the crash-recovery path and every third one would have tripped the
   market-data outage counter. A backtest whose control flow differs from live
   on the ordinary path is the exact failure this rebuild exists to remove.

   What that costs is one bar of precision in `entry_at`, and therefore in the
   age `MAX_AGE` counts. It is named here rather than hidden because it is a
   real difference; it is the smaller of the two, and the alternative distorted
   the whole control flow to protect it.

   An order placed on the **last** bar of history has no next open, so it stays
   `SUBMITTED` and never fills. That is correct: history ran out, and inventing
   a price for it would be the look-ahead this rule exists to prevent.
2. **Stops are checked against the bar's low, take-profits against its high.**
   Checking the close, as the old code did, means a day that traded 8% down
   intraday and closed at −1% never triggers a 5% stop — while the exchange stop
   fires on the intraday print. Backtested stop-hit rates were systematically
   optimistic, which is the worst direction for them to be wrong in.
3. **A gapped open fills worse than the trigger.** A sell stop fills at
   `min(stop_price, bar.open)`; a take-profit at `max(target, bar.open)`. The
   exchange cannot fill at a price the market never traded at.
4. **When one bar touches both the stop and the target, the stop wins.** Daily
   bars cannot say which came first, and the pessimistic reading is the only one
   that cannot flatter the result.

**The simulator refuses what the broker would refuse (v1.48).** A market buy
whose turnover plus fee exceeds simulated cash raises `OrderRejected`, leaving
cash and holdings untouched — as `broker.client` does, and as `get_max_lots`
exists to make avoidable. It previously debited unconditionally, so cash went
negative and the next `get_portfolio()` raised `ValueError: cash must not be
negative` (#47). Beyond the crash: a simulator that funds any order cannot
demonstrate that the gate and sizing keep the bot solvent, which is one of the
things a backtest is for.

**Four cycles per bar, at the open, the low, the high and the close (v1.49).**
One cycle per bar made two of the system's controls structurally unreachable,
and neither absence was visible in a result:

- **The daily loss limit could never fire.** One bar is one cycle *and* one
  Moscow date, so step 4 wrote the day's opening snapshot and measured against
  it in the same instant; the intra-day loss was always zero (#53). On any run
  where the strategy would have breached the limit, live stops trading for the
  rest of the day and the backtest kept going — optimistic in exactly the
  scenario the limit exists for.
- **`MAX_AGE` could never fire.** `lifecycle.exits` requires
  `session.in_closing_window(now)`, the final fifteen minutes, and the single
  cycle sat at the session start. Measured: `max_holding_days=1` over twenty
  flat bars with stop and target 50% away produced **zero exits**.

The four instants are the session start, two marks a third and two thirds
through it, and one **inside the closing window**, which is what makes
`MAX_AGE` reachable. The day's opening snapshot is still written once, on the
first of the four, so the loss is measured against the open rather than against
the previous mark.

**The order is open, low, high, close** — the drawdown before the recovery.
That is the same pessimism as the stop-beats-target tie-break above, and for the
same reason: a daily bar cannot say which came first, and only the pessimistic
reading cannot flatter the result.

It also fixes a third thing that was never filed: `get_last_price` returned the
bar's **close**, so a `LOCAL` position's stop was checked against the close
only — the exact optimism the exchange-stop rule above removes, still present on
the path where the bot owns the stop itself.

**Commission is the broker's tariff, not a flat fee** — a percentage of turnover
with a minimum, applied per fill. The old flat figure was also applied twice to
one round trip.

**`async backtest.run(bars, config, strategies, commission, slippage) → BacktestResult`**
- **Drives `app.loops.trading_cycle` itself, four times per bar**, against a
  temporary database with the migrations applied and a `SimulatedExchange` in
  place of `broker.client`. Live and backtest cannot diverge, because they are the same
  code: the gate, the sizing, the exits, the cooldowns, the halt, the
  duplicate-ticker rule and the portfolio-exposure ceiling are all the live ones,
  reached the way live reaches them.
- This replaces a module that imported `strategies`, `risk.sizing` and
  `lifecycle.exits` but **not `risk.gate`** — obeying "never reimplement" while
  omitting the gate entirely, so that cooldowns, `max_open_positions`,
  duplicate-ticker rejection, halt and session state played no part in any
  result. An omission reads as compliance, which is why it survived (#12).
- Runs the **whole watchlist** with concurrent positions against one shared cash
  balance. One ticker and one position modelled away capital contention,
  correlation and portfolio drawdown — the three things a portfolio-level risk
  answer depends on.
- **Equity is marked to market on every bar.** `max_drawdown` came from the cash
  balance, appended only on trade events; cash *falls* when you buy, so the
  reported figure was approximately the position size and meant nothing.
- Returns trades, P&L, win rate, maximum drawdown, exit-trigger distribution and
  the buy-and-hold benchmark, as before.

**What this costs, stated plainly.** The simulator is a second implementation of
the *exchange*, and it can be wrong in ways that flatter or punish a strategy
without either being detectable from the result. It is not a substitute for the
verification suite against the live account: it answers "what would this strategy
have done", never "does the broker behave as we think". Those are different
questions and #44 is still the other one.

**`fit(candles_by_ticker: dict[str, list[Candle]], horizon_days: int, folds: int, seed: int) → FittedModel`**
- Trains a buy/no-buy classifier. The label is whether the take-profit level is
  reached before the stop level within `horizon_days`, so the model is trained on
  the question the live system actually asks it.
- `seed` is required and recorded in the export: an unreproducible model cannot
  be audited after a losing week.
- Uses **walk-forward** validation across `folds`; a single train/test split on a
  time series leaks the future into the past and is not acceptable.
- Returns the fitted model with its validation scores per fold. Reporting one
  averaged number hides a model that works in one regime and fails in another.

**`export(model: FittedModel, path: Path) → Path`**
- Writes a joblib bundle `{"model", "features", "seed", "trained_at"}` where
  `features` is `strategies.ml_model.FEATURE_NAMES` in order, which
  `strategies.ml_model.load` validates.

**Feature construction has one owner.** `strategies.ml_model.build_features`
builds the feature vector, and `sandbox.train` **imports it** rather than
rebuilding the same four features for training. This is the same rule as the
backtester importing the live strategies, for the same reason: features computed
one way at training and another way at inference produce a model that scores well
offline and behaves differently on real money, and nothing in the manifest check
would catch it — the names would still match.

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
| `exit_commission` | TEXT NULL | Decimal string. Set only for an `EXTERNAL` close, where there is no closing order row to carry it |
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

### `job_runs`

When each periodic job last completed, per period. Owned by `db.job_runs`.

| Column | Type | Notes |
|---|---|---|
| `job` | TEXT | Part of the primary key. `rollover`, `backup`, `weekly_report`, `schedule_refresh`, `heartbeat` |
| `period_key` | TEXT | Part of the primary key. A Moscow date for a daily job, a week-start date for the weekly report |
| `ran_at` | TEXT NOT NULL | UTC. The moment the job **first** completed for that period |

**Invariants.**
- Primary key `(job, period_key)`. A second completion for the same period keeps
  the first `ran_at`: when it first ran is the fact worth having.
- Rows are never deleted. Growth is a handful of rows a day.
- It exists because this state was process-local, so every restart re-armed
  every job — three restarts in a day meant three heartbeats, and a restart
  through the report hour lost the week silently (#27).

### `trading_days`

What the broker said about each calendar day, recorded when it said it. Owned by
`db.trading_days`.

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. Moscow calendar date, ISO-8601 |
| `is_trading_day` | INTEGER NOT NULL | 1 or 0 |
| `session_start` | TEXT NULL | UTC. Null on a non-trading day |
| `session_end` | TEXT NULL | UTC. Null on a non-trading day |
| `observed_at` | TEXT NOT NULL | UTC. When the broker was asked |

**Invariants.**
- A day is overwritten by a newer observation. A holiday can be announced after
  the fact, and the broker's most recent answer is the one to keep — unlike
  every other table here, where history is append-only, because this records
  *what is true about a date* rather than *what happened*.
- Rows are never deleted. The table grows by one row a day.
- It exists because the broker serves no schedule before today (§2.1), so the
  only way to know whether last Tuesday was a trading day is to have been told
  at the time and to have written it down (#45).

### `position_events`

Append-only history of every mutation to a position. Owned by `db.positions`,
written in the same transaction as the row change it describes.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `position_id` | INTEGER NOT NULL | FK → `positions(id)` |
| `occurred_at` | TEXT NOT NULL | UTC ISO-8601, from `clock.now()` |
| `event` | TEXT NOT NULL | CHECK IN (`OPENED`, `STOP_PROTECTION_CHANGED`, `LOTS_ADJUSTED`, `CLOSED`, `REALISED_RECOMPUTED`, `ADOPTED`) |
| `detail` | TEXT NOT NULL | JSON object: previous and new values, and the order key where one applies |

**Invariants.**
- Rows are never updated and never deleted.
- Every successful mutation of a position inserts exactly one row; a mutation
  that rolls back inserts none. The trail cannot disagree with the row, because
  the two are written in one transaction.
- The `positions` row answers "what is true now"; this table answers "how did it
  get there". The second question is the one an incident asks, and before v1.23
  nothing in the database could answer it.

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
| `exit_trigger` | TEXT NULL | CHECK IN (`STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`). The trigger this exit was submitted for. Non-null exactly when `intent = 'EXIT'` |
| `broker_reason` | TEXT NULL | Broker's rejection text, verbatim |
| `created_at` | TEXT NOT NULL | Written **before** submission |
| `settled_at` | TEXT NULL | |
| `broker_order_id` | TEXT NULL | The broker's own identifier, where the bot knows it. Set for a row describing an execution the exchange performed on the bot's behalf, whose `key` the broker has never seen |
| `commission_alerted_at` | TEXT NULL | UTC. Set once, when the owner is first told this row's commission is still unknown |

**Invariants.** `FILLED`, `REJECTED` and `CANCELLED` are terminal — no row leaves
them. A row in `SUBMITTING` means the outcome is unknown and must be resolved by
querying the broker with `key`, never by resubmitting.

`intent = 'EXIT'` requires `exit_trigger` non-null; `intent = 'ENTRY'` requires it
null. The trigger is recorded **when the exit is submitted**, before its outcome
is known, because that is the only moment the reason is in hand. A process that
dies mid-exit and recovers later has no other way to learn why it was selling,
and a recovered exit attributed to the wrong trigger corrupts the exit-trigger
distribution, the per-strategy statistics, and the gap-versus-stop measurement
permanently — mislabelled history cannot be repaired.

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
- closed externally: {"type": "CLOSED_EXTERNALLY", "ticker": "...", "position_id": 12, "exit_price": "123.45", "exit_at": "...", "exit_commission": "1.25"}
- exit unresolved:   {"type": "EXIT_UNRESOLVED", "ticker": "...", "position_id": 12, "reason": "..."}
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
- `002_order_exit_trigger.sql` adds `exit_trigger` to `orders` with its CHECK
  constraint. It is a separate migration rather than an edit to `001` because
  `001` has been applied — in tests, and potentially on a developer machine — and
  the forward-only rule holds without exception.
- `007_job_runs.sql` creates `job_runs`. Empty at first, which simply means
  every job is due once after the migration — correct, not a gap.
- `006_trading_days.sql` creates `trading_days`. It holds no history at first,
  and that is correct rather than a gap to backfill: there is nowhere to backfill
  *from*, since the broker will not serve a past schedule at all. The table fills
  from the first `refresh` onwards, fourteen days at a time.
- `005_order_broker_id.sql` adds `broker_order_id` and `commission_alerted_at`
  to `orders`. Both nullable, no default, no backfill: the live database holds
  zero orders, and there is nothing historical to reconstruct.
- `004_position_exit_commission.sql` adds `exit_commission` to `positions`. It
  exists because an `EXTERNAL` close has no closing order row, so its commission
  had nowhere to live and was simply lost (#11). Nullable with no default and no
  backfill: the live database holds zero closed positions, and inventing a
  historical value would be the same mistake in a new place.
- `003_position_events.sql` creates `position_events`. Forward-only, as above:
  `001` is already applied in tests and on the deployed database. The live
  database holds zero rows, so adding the table and enabling foreign-key
  enforcement cannot conflict with existing data — this is the cheapest moment in
  the project's life to turn enforcement on.
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

Every event is one structured JSON record on stdout. `logging_setup` puts
`timestamp` (UTC), `moscow_time`, `level`, `logger` and `message` on **every**
record it formats, including those from third-party libraries; its contract in
§4 owns that schema. `event` is set by the module emitting it, and the table
below lists the additional fields each event **must** include. A record missing
a required field is a defect — these fields are what makes the log answerable
after the fact.

**Each event is owed by exactly one module, named in the table (v1.60).** Until
v1.60 this catalogue was the only place most events appeared: the task generator
cuts by heading, so a module's agent never saw the obligation, and the events
were never emitted while every test stayed green. An event listed here without a
sentence in its owning module's §4 contract is a spec defect, not an
implementation gap.

| Event | Owner | Level | Required data fields |
|---|---|---|---|
| `startup_ok` | `app.startup` | INFO | `version`, `mode`, `halted`, `adjustments_count` |
| `startup_failed` | `app.startup` | CRITICAL | `stage`, `reason` |
| `config_invalid` | `app.startup` | CRITICAL | `variable` |
| `session_open` / `session_closed` | `market.session` | INFO | `trade_date`, `opens_at`, `closes_at` |
| `candles_failed` | `market.data` | WARNING | `ticker`, `error` |
| `signal_generated` | `app.loops` | INFO | `ticker`, `strategy`, `reference_price` |
| `signal_rejected` | `app.loops` | INFO | `ticker`, `strategy`, `rejection_reason` |
| `order_submitting` | `execution.orders` | INFO | `key`, `ticker`, `side`, `intent`, `lots` |
| `order_filled` | `execution.orders` | INFO | `key`, `ticker`, `filled_lots`, `filled_price`, `commission` |
| `order_rejected` | `execution.orders` | ERROR | `key`, `ticker`, `intent`, `broker_reason` |
| `order_unresolved` | `execution.orders` | WARNING | `key`, `ticker`, `age_seconds` |
| `order_resolved` | `execution.orders` | INFO | `key`, `resolved_status`, `source` |
| `position_opened` | `execution.orders` | INFO | `position_id`, `ticker`, `strategy`, `lots`, `entry_price`, `stop_price`, `target_price` |
| `position_closed` | `execution.orders` | INFO | `position_id`, `ticker`, `exit_trigger`, `exit_price`, `realised_pnl`, `gap_vs_stop` |
| `exit_failed` | `execution.orders` | ERROR | `position_id`, `ticker`, `attempt`, `error` |
| `stop_order_placed` | `execution.orders` | INFO | `position_id`, `ticker`, `stop_price`, `stop_order_id` |
| `stop_order_cancelled` | `execution.orders` | INFO | `position_id`, `stop_order_id`, `cause` |
| `stop_order_executed` | `broker.reconcile` | INFO | `position_id`, `ticker`, `fill_price`, `gap_vs_stop` |
| `stop_protection_degraded` | `execution.orders` | ERROR | `position_id`, `ticker`, `attempts` |
| `stop_order_orphaned` | `broker.reconcile` | ERROR | `stop_order_id`, `ticker` |
| `partial_fill` | `execution.orders` | WARNING | `key`, `ticker`, `intent`, `requested_lots`, `filled_lots` |
| `cooldown_started` | `app.loops` | INFO | `ticker`, `active_until` |
| `halt_triggered` | `state.halt` | CRITICAL | `reason`, `detail`, `daily_loss_pct` |
| `halt_cleared` | `state.halt` | INFO | `actor` |
| `reconciliation` | `broker.reconcile` | INFO | `adjustments_count`, `types` |
| `broker_unavailable` | `broker.client` | WARNING | `method`, `consecutive_failures` |
| `rate_limited` | `broker.client` | WARNING | `method`, `retry_after_seconds` |
| `db_write_failed` | `db.connection` | ERROR | `table`, `critical` |
| `telegram_send_failed` | `telegram.notifier` | WARNING | `attempt`, `error` |
| `unauthorised_command` | `telegram.commands` | INFO | `chat_id`, `command` |
| `secret_redacted` | `telegram.notifier` | ERROR | `sink` — never the secret, never its length |
| `backup_ok` / `backup_failed` | `ops.backup` | INFO / ERROR | `path`, `bytes` / `error` |
| `task_crashed` | `app.loops` | ERROR | `task`, `error`, `restart_in_seconds` |
| `clock_drift` | `app.loops` | WARNING | `drift_seconds` |
| `heartbeat` | `app.loops` | INFO | `uptime_seconds`, `open_positions`, `halted` |
| `weekly_report_sent` | `reporter.weekly` | INFO | `period_start`, `period_end` |

`gap_vs_stop` on `position_closed` is populated only for `STOP_LOSS` exits and
carries the difference between the actual exit price and the stop price. It is
the field the gap-cost query aggregates, and omitting it makes the true cost of
overnight holding unmeasurable.

**Host export (v1.61).** `scripts/deploy/export_health.py` groups on `event`.
A record with no `event` key is library noise and is omitted from domain
counts — it is not `unknown`. An `event` value that is not in this table is
`unknown` and the export exits non-zero. Heartbeat uptime uses `heartbeat`
events only.

---

## 8. Error handling rules

Applies across all modules. Every external failure mode has exactly one rule.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.
2. **Broker rate limited** → WARNING, back off **for at least as long as the
   broker's own hint**, and alert once when it has persisted for three
   consecutive cycles, naming throttling rather than an outage. Never treat as
   fatal.

   `BrokerRateLimited.retry_after` carries the hint when the broker sends one.
   It is the only party that knows when it will accept calls again, so it raises
   the floor under the escalating back-off of rule 1 and never lowers it: the
   delay is the longer of the two. It is still bounded by the same ceiling,
   because this loop is also the **exit** path — no number supplied from outside
   may keep the bot from closing a position indefinitely.

   Until v1.53 the hint was computed, asserted at the raise site, and read by
   nothing: the bot backed off on its own schedule while the broker's answer sat
   unused on the exception. This rule previously said "alert once if sustained
   beyond five minutes", which named a threshold no code implemented and no test
   could fail — the alert has always come from rule 1's consecutive-cycle
   counter. It now says what happens.
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
9. **Candle fetch fails for one ticker, or returns too little history** → omit a
   failed ticker, WARNING, continue the batch. Both are **degraded calls** and
   both count on **one** per-ticker consecutive counter: on the **third**
   consecutive degraded call, alert **once**, naming the ticker and the reason —
   the failure, or the candles returned against the candles required — and send
   nothing further for it until a call is not degraded. A good call clears both
   the count and the alerted flag. Tickers crossing the threshold in the same
   call share one alert. Only `BrokerUnavailable`, `BrokerRateLimited` and
   `InstrumentNotFound` are handled as failures; every other exception
   propagates under rule 21.

   A ticker that fails forever is a delisting, a rename or a wrong class code —
   not weather — and before this rule it was dropped from every batch in
   silence, so the watchlist could shrink to nothing while the bot reported
   itself healthy (#23). **A fetch that succeeds and returns nothing is the same
   blindness wearing the opposite disguise**: it looks like success to every
   counter, while the ticker is skipped or evaluated to `None` on every cycle.
   The two must share a counter, or a ticker alternating between them crosses no
   threshold ever.
9b. **A quote is rejected as non-positive, stale, or an implausible move** →
    WARNING, omit that instrument for the cycle, alert once per cycle with the
    count. It is **not** a broker outage: it must not increment the consecutive
    failure counter of rule 1, and it must not be retried, because the next
    reading arrives on the next cycle anyway. Treating bad data as an outage is
    how a malformed field becomes an alert about the network.
10. **Trading schedule unavailable, or returned with no trading sessions** →
    treat the market as closed, WARNING, alert once, and leave any existing
    cache intact. The safe default is not to trade. An empty result is a form of
    unavailable, not a valid schedule: treating it as success stores a cache
    that makes `is_open` false forever with no error anywhere.
11. **Database write failure on a trading-critical path** (orders, positions,
    halt state, **cooldowns** — v1.63) → hard error: halt trading, alert, stop opening anything. The bot
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
19. **Secret exposure** → no token **and no account identifier** is ever written
    to a log, an exception message, or a Telegram message. If the redaction
    filter detects a secret in an outgoing Telegram message, the message is
    **dropped**, `secret_redacted` is emitted, and an alert reporting the
    incident without the secret is sent in its place.
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
    polling: `lifecycle.exits` returns `STOP_LOSS` for that position and
    `execution.orders.close_position` sells it. Never unwind a sound position
    because a secondary order failed.
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
29. **Clock accuracy is a host requirement, verified at deployment, not a
    runtime rule.** V9 confirms the host clock is NTP-synchronised before the bot
    is deployed, and `app.startup` logs the observed system time in UTC and MSK
    so a skewed clock is visible in the first log line after every restart.

    There is deliberately **no runtime skew check**. The broker exposes no server
    wall-clock: the only timestamp available is `LastPrice.time`, which is the
    time of the last *trade* and lags arbitrarily when a market is quiet. Halting
    trading because nobody traded for ninety seconds would be a worse failure
    than the drift it guards against, and the alternative — shipping a
    hand-written NTP client into a system that moves money — is more risk than a
    correctly configured time daemon warrants.
30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

31. **A module begins, commits or rolls back the shared connection itself** →
    programming defect, in the same family as rules 22 and 30. Every write runs
    inside `db.connection.transaction()`; nothing else touches transaction state.
    A `commit()` is connection-wide, so a module committing on its own behalf
    commits whatever another module has in flight, and that module's `rollback()`
    then undoes nothing. This is not a runtime condition to handle — it is a rule
    the code must not violate, and §3.2 pins it per module.

    **`db.migrations` is the single exemption**, and it is narrow: `apply`
    receives a connection rather than taking one, applies each file in its own
    transaction as §6 requires, and runs during `app.startup` step 3 — before any
    other task exists, so there is nothing in flight for it to commit. Every
    other module, without exception, uses `transaction()`.

32. **The broker reports a holding the bot has no record of at startup** →
    refuse to start, alert, and name every ticker, unless
    `config.allow_foreign_holdings` is true. The account is the bot's alone
    (brief v1.8). The bot cannot distinguish "someone bought this by hand" from
    "local state is wrong", and both readings forbid trading it. When the flag is
    set, the holdings are named in the ready alert and are never traded: no stop
    placed, no exit evaluated, no sale made. Never adopt one — adoption derived a
    stop and target from the holding's average cost, which handed the next cycle
    a position already past its take-profit.

33. **A recorded price comes from the broker, or the record stays pending.**
    Realised P&L, exit prices and commissions are written from what the broker
    reports it did — an order state, an executed stop, an operation — and never
    from a quote, a stop price, an entry price, or any other number the bot has
    to hand. Where the broker's own record is not yet available, the position
    stays open and the read is retried on the next cycle; after a bounded number
    of cycles the owner is alerted. A position closed a minute late is
    recoverable and a position closed at an invented number is not, because
    nothing downstream can tell the invented one from a real one. This rule
    generalises #4, #5, #8 and #11, which are four instances of the same
    mistake.

34. **An order the bot submitted is partially filled** → the filled part is real
    and the remainder is not, and which of the two is left exposed depends on the
    direction. On an **entry**, cancel the remainder, re-read the order, and
    write the position from that read; if either call fails, write nothing and
    let recovery repeat it. On an **exit**, never abandon the remainder: the
    position stays open, reduced to the lots still held, and the exit is retried
    under rule 4. A partial fill is alerted either way. An abandoned entry
    remainder leaves cash unspent; an abandoned exit remainder leaves shares held
    against a decision to sell them, which is the state this system must not rest
    in.

35. **A trade whose originating strategy is unknown is recorded as
    `UNATTRIBUTED`**, and is never credited to a named strategy. This covers
    crash-recovered entries with no matching signal, and any later path that
    books a position the bot did not itself decide on. Per-strategy figures are
    the evidence for enabling and disabling strategies: a default that names a
    real strategy is not a missing datum, it is a wrong one, and it is wrong in
    the same direction every time.

36. **A degraded state that persists must alert; only a transient one may be
    logged.** Wherever this system absorbs a failure — a retry, a skipped
    instrument, a partial result — the absorption needs three things *together*:
    a threshold at which continuing to absorb stops being reasonable, exactly
    one alert when it is crossed, and a reset on recovery that re-arms that
    alert. Rules 1, 2 and 9 are the instances; the shape recurs wherever a loop
    tolerates a failure it cannot fix.

    Any two of the three are not enough, and each missing part has already cost
    this project an issue. With no threshold the log is the only record and
    nobody reads it, which is how a permanently broken ticker was dropped from
    every batch for as long as it took to notice (#23). With no reset the second
    incident is silent, which is #32 and #48 — the same defect written twice,
    hours apart, in two modules. With no single alert the channel fills and
    stops being read, which is the "commission still unknown" alert that fired
    on every backfill run forever (#8).

    Whenever a latch is added, list its set-sites against its reset-sites and
    look for the asymmetry. That check is mechanical, it takes a minute, and it
    is the only thing that has ever caught this class.

37. **No instrument on the watchlist costs less than one position budget** →
    alert at startup, and continue running. The bot is watching a list it cannot
    afford to buy any of, and every signal it generates will be rejected
    `ZERO_LOTS` forever. The rejection itself is correct and stays correct — this
    rule governs only whether the owner is told. It is reported once per start
    rather than per cycle or per signal: at one poll a minute the per-signal
    alternative is several hundred identical messages a day, and an alert that
    repeats forever is equivalent to no alert in a channel whose premise is that
    silence means healthy.

    Where **some** instruments are affordable and some are not, the unaffordable
    ones are named in the ready alert and nothing is escalated. A watchlist the
    budget only partly reaches is a normal operating state, not a fault: on
    2026-08-28 MGNT and LKOH were out of reach while SBER and GAZP were buyable,
    and that configuration was working as intended.

38. **A protective stop's price cannot be compared to the increment it was
    snapped to** → report **no price-based finding** for it, alert, continue.
    The broker rounds every posted stop to the instrument's
    `min_price_increment`, so `broker.reconcile` judges a stop mispriced only at
    a difference of a full increment or more, reading the increment from
    `get_instrument`. When that read fails, or returns an increment of zero or
    less, neither `STOP_MISPRICED` nor `STOP_ADOPTABLE` is reported for that
    position (v1.56). Findings that do not depend on the price —
    `STOP_DUPLICATE`, `STOP_ORPHAN` — still stand.

    Both withheld findings have remedies that act on the price. `STOP_MISPRICED`
    is cancel-then-re-post, which leaves a live position unprotected for the gap
    between the two calls; spending that on a difference the bot cannot measure
    is a worse trade than leaving a possibly-stale stop standing, which at least
    still protects. `STOP_ADOPTABLE` is `adopt_existing_stop`, which makes the
    exchange the position's sole protection at a price this module just failed
    to check — and `lifecycle.exits` then stops firing `STOP_LOSS` for it. The
    unjudged position stays `LOCAL` and is judged on the next pass.

---

## 9. Dependencies

Versions below are floors. The **resolved set is pinned in `requirements.lock`**,
generated from a clean 3.12 environment on 2026-08-20 with every floor satisfied
and V10 passing: Python 3.12.14, SDK 1.49.1, pytest 9.1.1, pytest-asyncio 1.4.0,
mypy 2.3.1, ruff 0.16.4, numpy 2.5.2, scikit-learn 1.9.0, aiosqlite 0.22.1,
structlog 26.1.0, python-telegram-bot 22.8. The application is built from the
lockfile, not from these floors.

The broker SDK is pinned by the vendored wheel and its checksum rather than by a
lockfile line, because a local file path is not portable across machines.

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

`requirements.lock` pins the **server and development** sets, which is what the
image is built from. The sandbox set is deliberately unpinned: it is a research
environment on one laptop, where a newer pandas is a convenience rather than a
risk, and nothing in it can reach the trading path.

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
| `pyarrow` | ≥ 25.0 | Parquet engine for the candle cache. pandas 3.0 does **not** require it — verified, `Required-by` is empty — so it is a genuine addition, permitted because the sandbox is laptop-only and never installed on the server. Parquet is chosen over CSV because it round-trips decimal and timezone-aware timestamp types, which a candle cache of `Decimal` prices needs |
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
- `env_file: .env`, beside the compose file, mode `600`, never baked into the
  image.
- Volume `./data` → `/data`, holding the database and backups. Relative to the
  compose file, so the deploy is not pinned to one host path.
- `stop_grace_period: 60s` — long enough for `app.shutdown` to settle an in-flight
  order rather than being killed mid-submission.
- Log driver with size-based rotation: **five files of 10 MB** (`max-size: 10m`,
  `max-file: "5"`). The brief requires size-based rotation so logs cannot fill
  the disk; three files was a deploy-compose drift (v1.61). `docker-compose.yml`
  and `docker-compose.deploy.yml` must match.
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