# Task 7/39: Implement `zarabot/db/orders.py`

## Product context

Sole owner of order rows. Records intent BEFORE the broker is called, which is what makes a crash mid-submission recoverable.

## Build order position

Module **7** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

**`async settle(key: str, status: OrderStatus, filled_lots: int, filled_price: Decimal | None, commission: Decimal | None, broker_reason: str | None) → OrderRecord`**
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

**`async get(key: str) → OrderRecord | None`**
- Returns the order or `None` when absent. `None` is expected and not an error:
  an adopted position's synthetic open key has no order row, because the bot
  never placed one.
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
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

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
