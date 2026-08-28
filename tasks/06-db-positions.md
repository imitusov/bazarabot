# Task 6/40: Implement `zarabot/db/positions.py`

## Product context

Sole owner of position rows. Positions are never deleted; closing is a state transition, because the history is the point of the project.

## Build order position

Module **6** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

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

## Module contract

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

**`async adopt(instrument: Instrument, lots: int, average_price: Decimal, adopted_at: datetime) → Position`**
- Creates an open position for a holding discovered at the broker but unknown
  locally, with `adopted = True`, stop and target derived from `average_price`,
  and age counted from `adopted_at`.
- Called only by `broker.reconcile`.
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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

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
- No function in this module calls `aiosqlite.connect`, and none issues `BEGIN`,
  `commit` or `rollback` (proves it runs on `db.connection.shared()` inside
  `db.connection.transaction()` — the defect that opened a connection per call,
  and then the one where a bare commit made another module's half-written rows
  durable).

## Expected output

- `zarabot/db/positions.py` implementing the contract exactly
- `tests/test_db_positions.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_positions.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/positions.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
