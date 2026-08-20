# Task 8/38: Implement `zarabot/db/stop_orders.py`

## Product context

Sole owner of stop-order rows. Tracks the standing stop the exchange holds for each open position.

## Build order position

Module **8** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

**Invariants.** At most one stop order in `ACTIVE` or `PLACING` per open
position, enforced by a partial unique index on `position_id`. `EXECUTED` means
the exchange sold the position; the corresponding position must be closed with
`exit_trigger = 'STOP_LOSS'`.

## Module contract

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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception.

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
