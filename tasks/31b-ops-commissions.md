# Task 31b/46: Implement `zarabot/ops/commissions.py`

## Product context

Records commissions the broker reported after the fill and corrects the profit figures that depended on them. Without it a trade's cost stays permanently understated.

## Build order position

Module **31b** of 46 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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
- **The order state is not the last word, and a zero there is not an answer
  (v1.91, #246).** `broker.client` now records `None` rather than `Decimal(0)`
  when the broker reports a zero commission on a fill, which is what makes these
  rows visible to `list_missing_commission` at all — but re-querying the same
  order state returns the same zero, so resolving from it alone would write the
  original defect back in a second place. When the state carries a number, that
  number wins and nothing else is read. When it does not, **the operations feed
  is the arbiter.**
- **One feed read per run, not one per order.** `backfill` calls
  `broker.client.get_operations(since, until)` **once**, after
  `list_missing_commission` returns a non-empty list, and never at all when it
  returns nothing. The window is the caller's own, and it needs no margin: the
  fee posts a measured **one second** after its trade (§2.1), and the trade is
  inside `[since, until]` by construction because that is the window the order
  was selected in.
- **Resolution is three-way, and the third case is what stops the alert.** For
  one order, given the feed:
  1. the order's `trade_ids` appear in one or more operations' `trade_ids`, and
     at least one operation's `parent_operation_id` is one of those trades →
     **the sum of those fees**, recorded;
  2. the trades are found, no fee operation names any of them as a parent, and
     the latest of those trades occurred at least `_FEE_SETTLE` before `now` →
     **`Decimal(0)`, a measured zero**, recorded. The row leaves
     `list_missing_commission` forever and is never asked about again. This is
     the commission-free trade, and it is the case that keeps v1.91's "a zero at
     fill is unknown" from becoming an alert that repeats forever (failure class
     14);
  3. anything else — no `trade_ids` on the order state, no matching trade in the
     feed, or a matching trade younger than `_FEE_SETTLE` → **unknown**. The row
     is asked about again on the next run, and alerts once at 24 hours as before.
- **`_FEE_SETTLE` is five minutes, and it is set from the measurement.** The
  observed trade→fee lag is min 1s, median 1s, max 1s over the full account
  history (§2.1); five minutes is 300× the worst observed. It exists for exactly
  one hazard: a trade that fills seconds before `until`, whose fee has not posted
  yet, must not be read as case 2 and written off as free. It is **not** a
  settlement window and it is not a reason to widen
  `app.loops._BACKFILL_LOOKBACK`, which at 7 days is already five orders of
  magnitude wider than the lag and stays where it is.
- **Failure of the feed read is not failure of the run.** `BrokerUnavailable`
  and `BrokerRateLimited` from `get_operations` are caught, logged at WARNING,
  and the run proceeds with an empty feed — every unresolved row is then case 3,
  unknown, and is retried tomorrow. That is the same posture `broker.reconcile`
  takes on the same two classes for the same feed, and the narrowness is the
  point: **every other exception propagates** under rule 21 (failure class 5).
  `OrderNotFound` continues to mean "unknown" for the order-state read alone.
- **A write failure here propagates. It is not swallowed (rule 11).**
  `record_commission` and `mark_commission_alerted` write the `orders` table,
  which rule 11 names as trading-critical; this module catches no
  `aiosqlite.Error` and adds no `except` around either call. Rule 12's
  non-critical swallow does not reach this module or `db.orders`.
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

    **All three paths have a writer as of v1.85.** Until then "instruments cache"
    named nothing: the table had no writer anywhere in `zarabot/` (#102), so this
    rule listed a path that could not fail. `broker.client` owns it now, and the
    swallow there returns the `Instrument` the broker just supplied — the caller
    asked for metadata, not for a cache (#46).

    **Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error`
    (v1.75)** The swallow exists for a database that will not take the row, not
    for every way the call site can be wrong. Any other exception propagates and
    reaches rule 21's supervisor with its traceback. Unqualified, this rule reads
    as `except Exception: pass` on the analytics path, and an analytics path is
    exactly where a silently dropped `TypeError` survives longest — nothing
    downstream misses the row until a weekly report is composed from it.

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
- **A row settled from a real zero-at-fill — commission unknown, the re-queried
  order state still reporting no commission and carrying the fill's `trade_ids` —
  resolves from the operations feed: the trade operation bearing one of those
  `trade_ids` has a fee child, the fee is written, and the closed position's
  `realised_pnl` is recomputed net of it** (v1.91; proves #246 is fixed where it
  broke. A test that starts from a commission of `None` and a state that reports
  a number proves nothing about this bug: that path already worked, and the row
  never reached it).
- **A trade found in the feed with no fee child, older than `_FEE_SETTLE`, is
  recorded as `Decimal(0)` and never selected again — no alert, on that run or
  any later one** (v1.91; proves the commission-free trade terminates. Without
  this case "a zero at fill is unknown" is an alert that repeats forever, which
  is failure class 14 and is what the previous remedy for #8 was written to end).
- **The same trade younger than `_FEE_SETTLE` is left unknown and re-queried**
  (v1.91; proves the terminal zero is a measurement about a settled trade and not
  a race with the fee that posts a second later).
- **`get_operations` raising `BrokerUnavailable` leaves every row unknown and
  raises nothing**, and the run still alerts on a row past 24 hours (v1.91;
  proves the feed is an arbiter the run can do without for a day, not a new way
  for the backfill to fail).
- The feed is read **once** for a run with several unresolved orders, and **not
  at all** when nothing is missing (v1.91; proves the daily job costs one call,
  not one per order and not one on an empty day).

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
