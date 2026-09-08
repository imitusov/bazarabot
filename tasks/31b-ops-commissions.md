# Task 31b/42: Implement `zarabot/ops/commissions.py`

## Product context

Records commissions the broker reported after the fill and corrects the profit figures that depended on them. Without it a trade's cost stays permanently understated.

## Build order position

Module **31b** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

## Module contract

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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

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

## Expected output

- `zarabot/ops/commissions.py` implementing the contract exactly
- `tests/test_ops_commissions.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_ops_commissions.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/ops/commissions.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
