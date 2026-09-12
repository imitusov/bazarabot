# Task 8/42: Implement `zarabot/db/stop_orders.py`

## Product context

Sole owner of stop-order rows. Tracks the standing stop the exchange holds for each open position.

## Build order position

Module **8** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

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

Owned by `db.stop_orders`, which is the only module that writes it.

**Invariants.** At most one stop order in `ACTIVE` or `PLACING` per open
position, enforced by the partial unique index `idx_stop_orders_one_live`
recorded below. `EXECUTED` means
the exchange sold the position; the corresponding position must be closed with
`exit_trigger = 'STOP_LOSS'`.

**Constraints (v1.80).** Both are created by `001_initial.sql` and both are part
of this contract, not incidental schema. They were unwritten until v1.80 (#206):
a migration dropping either would have violated no stated line, and the only
change an owner would see is that a class of row the database currently refuses
becomes merely detectable afterwards.

- **`idx_stop_orders_one_live`** — `CREATE UNIQUE INDEX
  idx_stop_orders_one_live ON stop_orders (position_id) WHERE status IN
  ('ACTIVE', 'PLACING')`. What it makes impossible: a second live stop for a
  position ever reaching the table. The
  duplicate `INSERT` fails, so two rows can never each claim one position's
  trigger, and `record_placing` for a position that already has a live stop
  raises at the database — before the broker is called, which is the moment at
  which a duplicate would otherwise become a real second stop order standing at
  the exchange. This is the storage half of the one-owner rule; the detection
  half is `active_for_position` raising `OrderStateError` when two standing rows
  are found (§3.2), which is a second line of defence and not the primary one —
  its test must `DROP INDEX` to reach the case at all, and a reader who saw only
  that test would conclude duplicates are possible and merely caught.
- **`position_id INTEGER NOT NULL REFERENCES positions (id)`** — what it makes
  impossible: a stop row that names no position, or names one that does not
  exist. Every row therefore answers "whose trigger is this?" from the row
  itself, which is what lets reconciliation match standing stops to open
  positions and cancel the ones that match nothing. Enforcement is
  per-connection and depends on `PRAGMA foreign_keys = ON`, which
  `db.connection` issues on the shared connection and `db.migrations` issues on
  its own (§4); without that pragma SQLite parses the clause and ignores it.

## Module contract

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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state, **cooldowns** — v1.63) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

    **A write failure is `aiosqlite.Error`, and only that (v1.75)** That is the
    class `db.connection.transaction()` already emits `db_write_failed` for
    before re-raising, and the class a repository's caller may act on. **Every
    other exception propagates unchanged** — an `AttributeError` from a rename is
    not a database that is unavailable, and a `halt trading` path that cannot
    tell the two apart converts a programming error into a plausible degraded
    state (failure class 5). This is the narrowing already applied at the §4
    level to `db.connection` (v1.61) and `market.session` (v1.59) and never
    carried into §8, which is the text agents implement from (#107).

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

    **Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error`
    (v1.75)** The swallow exists for a database that will not take the row, not
    for every way the call site can be wrong. Any other exception propagates and
    reaches rule 21's supervisor with its traceback. Unqualified, this rule reads
    as `except Exception: pass` on the analytics path, and an analytics path is
    exactly where a silently dropped `TypeError` survives longest — nothing
    downstream misses the row until a weekly report is composed from it.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- `record_placing` writes a `PLACING` row that `active_for_position` then returns
  (happy path — proves the intent is durable before the broker is called, which
  is the whole reason the row exists).
- `activate` moves that row to `ACTIVE` and reads the broker's `stop_order_id`
  back off it (proves the identifier the cancel path later needs is stored, not
  reconstructed).
- `record_placing` twice with the same key raises `DuplicateOrderError` and
  leaves one row (proves the uniqueness invariant is enforced at the storage
  layer, as it is for `db.orders`).
- `settle` to each of `CANCELLED`, `EXECUTED`, `ORPHANED` and `FAILED` reads back
  that status with its `settled_at` (proves all four terminal outcomes are
  recordable — a stop the exchange fired and a stop that never stood must be
  distinguishable afterwards).
- `settle` on an already-settled row raises `OrderStateError` and leaves the
  first outcome in place (proves the transition out of a terminal status is
  refused, per the contract, so a late duplicate report cannot rewrite what
  happened).
- `settle` to a non-terminal status raises `OrderStateError` (proves the status
  argument is validated rather than written through).
- `activate` on an already-terminal row raises `OrderStateError` (proves a stop
  that has been cancelled cannot be resurrected as standing — the state in which
  both owners would believe they hold the trigger).
- `activate` on a key that was never recorded raises `OrderStateError` (proves
  an unknown key is an error, never a silent no-op that would leave the caller
  believing a stop it never wrote down is standing).
- `settle` on a key that was never recorded raises `OrderStateError` (split from
  the case above in v1.80: two calls, two cases, so neither can be half-enforced
  behind one bullet's word "or").
- `settle` with a naive `settled_at` raises `ValueError` (proves rule 22 at this
  boundary).
- `active_for_position` returns `None` for a position with no standing stop, and
  the single row when one stands (proves the "never a list" contract is a scalar
  result, so no caller branches on a collection).
- With two standing rows forced onto one position, `active_for_position` raises
  `OrderStateError` (proves the invariant is detected rather than resolved by
  picking one — this is the table that decides who holds a position's stop, and
  guessing here is how a position ends up sold twice).
- `list_active` returns only `ACTIVE` rows, oldest first, and an empty list when
  none stand (proves reconciliation sees standing stops and never `None`).
- A settled stop disappears from `list_active` and from `active_for_position`
  (proves settling is what retires a stop from both reconciliation views, so a
  fired stop is not re-adopted on the next restart).

## Expected output

- `zarabot/db/stop_orders.py` implementing the contract exactly
- `tests/test_db_stop_orders.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_stop_orders.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/stop_orders.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
