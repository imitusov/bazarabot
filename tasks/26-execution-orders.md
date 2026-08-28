# Task 26/40: Implement `zarabot/execution/orders.py`

## Product context

Where money moves. Write-then-send ordering, the submission locks, crash recovery, and the standing stop-loss. Highest-risk module in the project. 95% coverage.

## Build order position

Module **26** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

### `cooldowns`

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT | Primary key |
| `started_at` | TEXT NOT NULL | UTC, instant the position closed |

## Module contract

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
- Applies the entry cancel-and-re-read sequence above to any `ENTRY` order the
  broker still reports as `SUBMITTED` with lots filled, and reduces the position
  to its unsold remainder for any terminal `EXIT` order that sold part of it
  (v1.34). Both are the same principle as the rest of this function: an order
  whose outcome is uncertain is resolved by asking the broker, and only what the
  broker answers is written down.
- Ordering constraint: completes before any new order is submitted in the
  process's lifetime.
- Must never resubmit an order.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

3. **Entry order rejected** → ERROR, record `broker_reason`, alert, open no
   position. **Never retried.**

4. **Exit order rejected or broker unreachable during an exit** → ERROR, alert
   **immediately**, retry on every following cycle until the position closes or
   the owner intervenes. The documented exception to rule 3.

5. **Order submission times out or the outcome is unknown** → leave the row
   `SUBMITTING`, resolve by querying with the idempotency key on the next cycle
   or at next startup. **Never resubmit.**

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

23. **Protective stop order rejected or unplaceable** → retry three times, then
    mark the position `stop_protection = 'LOCAL'`, alert, and enforce the stop by
    polling: `lifecycle.exits` returns `STOP_LOSS` for that position and
    `execution.orders.close_position` sells it. Never unwind a sound position
    because a secondary order failed.

26. **Stop order executed by the exchange** → not an error. Close the position
    from the fill with `exit_trigger = STOP_LOSS`, start the cooldown, alert.

27. **Exit order partially filled** → retry the remainder until flat. A
    half-exited position must never be a resting state.

28. **Any code path that would set `confirm_margin_trade=True`** → rejected in
    review, not at runtime. There is no runtime condition under which this is
    correct; it is listed here because the failure mode it would produce —
    losses exceeding allocated capital — is the one failure the brief promises
    cannot happen.

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

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

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

## Expected output

- `zarabot/execution/orders.py` implementing the contract exactly
- `tests/test_execution_orders.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_execution_orders.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/execution/orders.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
