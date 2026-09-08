# Task 7/42: Implement `zarabot/db/orders.py`

## Product context

Sole owner of order rows. Records intent BEFORE the broker is called, which is what makes a crash mid-submission recoverable.

## Build order position

Module **7** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

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

## Module contract

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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

5. **Order submission times out or the outcome is unknown** → leave the row
   `SUBMITTING`, resolve by querying with the idempotency key on the next cycle
   or at next startup. **Never resubmit.**

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state, **cooldowns** — v1.63) → hard error: halt trading, alert, stop opening anything. The bot
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

## Expected output

- `zarabot/db/orders.py` implementing the contract exactly
- `tests/test_db_orders.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_orders.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/orders.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
