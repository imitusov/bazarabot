# Task 27/38: Implement `zarabot/broker/reconcile.py`

## Product context

Compares broker truth against local belief on startup. Observes and reports only - it never places or cancels an order.

## Build order position

Module **27** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

## Module contract

### `zarabot/broker/reconcile.py`

**`async reconcile(now: datetime) → ReconciliationReport`**
- Compares `broker.client.get_portfolio()` against `db.positions.list_open()`.
- Locally-open but absent at the broker → closed as `EXTERNAL` at the last known
  price, passing `order = None`. This module records **no** order row: it did not
  submit one, and inventing one would contradict its own prohibition on trading.
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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

6. **Order found at the broker that is unknown locally** → adopt the position,
   alert. Never ignore.

7. **Broker and database disagree on positions or quantities** → the broker wins,
   the local record is corrected, and the owner is alerted with specifics.

24. **Stop order found with no matching open position** → cancel it as an orphan
    and alert. A live stop against a position that no longer exists can sell
    stock the account does not hold.

25. **Open position found with no live stop order** while
    `stop_protection = 'EXCHANGE'` → place a replacement immediately and alert.
    An unprotected position is the state this whole mechanism exists to prevent.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Broker and database agreeing produces no adjustments and no alert (happy path).
- A position open in the database but absent at the broker is closed locally as
  externally closed and alerted (proves the broker is authoritative).
- A position present at the broker but absent locally is adopted with the
  broker's average price as entry price, marked adopted, and alerted (proves
  unknown holdings are managed rather than ignored).
- An externally-closed position is closed with `order = None` and **no row is
  written to `orders`** (proves reconciliation records only what the bot actually
  submitted).
- A lot-count mismatch adopts the broker's count and alerts (proves quantity
  reconciliation).
- Reconciliation is idempotent: running it twice against an unchanged broker
  produces adjustments once (proves it does not thrash).

## Expected output

- `zarabot/broker/reconcile.py` implementing the contract exactly
- `tests/test_broker_reconcile.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_broker_reconcile.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/broker/reconcile.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
