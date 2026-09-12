# Task 26/42: Implement `zarabot/execution/orders.py`

## Product context

Where money moves. Write-then-send ordering, the submission locks, crash recovery, and the standing stop-loss. Highest-risk module in the project. 95% coverage.

## Build order position

Module **26** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

### `cooldowns`

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT | Primary key |
| `started_at` | TEXT NOT NULL | UTC, instant the position closed |

## Module contract

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
- **Slippage is alerted, never unwound (v1.74).** After the entry settles, this
  module — the only one that sees both `signal.reference_price` and the actual
  fill price — compares them. When
  `abs(fill_price - signal.reference_price) / signal.reference_price` exceeds
  `config.fill_slippage_alert_pct`, it calls `telegram.notifier.alert` naming the
  ticker, the reference price, the fill price and the difference as a percentage.
  The position is opened, stopped and managed exactly as any other. It is **not**
  sold back, under any tolerance: unwinding is a second real trade (cancel the
  stop, market sell, start a cooldown) and the brief's policy is alert-and-keep.
  Stop and target are already derived from the fill, so the percentage risk is
  correct; what is wrong is the position's rouble size, which is reportable, not
  tradeable. The comparison is a pure `Decimal` calculation on values already in
  hand — no extra broker call — and a failure to send the alert must never fail
  the entry, which has already executed. There is no §7.1 event for this: the
  numbers are already in `order_filled` and `position_opened`, and the obligation
  is that the owner is told.
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
- **The database-failure halt passes no `daily_loss_pct` (v1.69).**
  `_halt_on_db_failure` runs because a database write just failed, and
  `pnl.daily_loss_pct` reads that same database — calling it there would query
  the thing that is broken, on the path that exists to handle its being broken.
  `halt_triggered` for this halt omits the field, and that absence is correct.
  This module does not import `pnl`, and must not start.
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
- **Starts the cooldown for the position's ticker, exactly as `close_position`
  does (v1.78).** Rule 26 has always required it — "close the position from the
  fill with `exit_trigger = STOP_LOSS`, start the cooldown, alert" — but this
  contract never said so, and the obligation reached the code only because both
  functions happen to share a private helper (#109, failure class 2). A rebuild
  of this function from this contract alone would drop the cooldown, and the bot
  would re-enter on the next cycle the ticker the exchange had just stopped it
  out of. The cooldown is started from the same instant the close is booked at.
- **A failed cooldown write halts but does not fail the close**, on exactly the
  terms `close_position` states above: the position row is already `CLOSED` and
  the exchange has already sold, so raising here would report a completed exit
  as failed. Cooldowns are rule 11, so the failure takes the rule-11 remedy —
  alert and halt — and this function returns the closed position, because it did
  close.
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
- **Rule 33's bound lands here (v1.75).** An order this function cannot settle
  because the broker cannot be reached stays unresolved and is retried on the
  next cycle; on the **third consecutive cycle** in which it is still unknown,
  alert **once** for that order key, and re-arm when the order settles by any
  route — a fill, a rejection, or `OrderNotFound`. The count and the alert are
  per order key, not per cycle: two stuck orders are two alerts, and one stuck
  order is one. Nothing new needs recording to do it. `order_unresolved` already
  carries `age_seconds`, computed from the order's own `created_at`, so the
  age is in hand at the site that would alert. Three matches rules 1, 2 and 9
  deliberately; a second threshold in this system would be a second number to
  remember.

**This module owns rules 3, 5, 26, 27 and 34 (v1.75).** All five are the same
subject from different sides — what the bot does when the broker's answer to a
submission is a refusal, a silence, or a fraction — and this is the only module
that submits:

- **Rule 3** — an entry the broker rejects is recorded with its `broker_reason`,
  alerted, and opens no position. It is **never retried**, on this cycle or any
  later one. The strategy that produced the signal will produce another if the
  condition still holds, and a retry loop on an entry is how a rejection becomes
  a position nobody decided to take.
- **Rule 4 is the documented exception to that**, and it is already stated above
  under `close_position`: an exit is retried on every following cycle until the
  position closes.
- **Rule 5** — a submission that times out or whose outcome is unknown leaves the
  row `SUBMITTING` and is resolved by `resolve_unfinished` querying with the
  idempotency key, which is the whole of rule 5's remedy, on the next cycle or at the next startup. **Never resubmit**,
  and note that resubmission is not merely forbidden but useless: §2.1 measured
  that a duplicate key is refused with `INVALID_ARGUMENT`/`30057` rather than
  returning the existing order.
- **Rule 26** — a stop the exchange executed is not an error. The position is
  closed from the fill with `exit_trigger = STOP_LOSS`, the cooldown starts, and
  the owner is alerted; rule 26 is a normal outcome with a bookkeeping remedy,
  not a failure path. The fill is the source of the exit price, per rule 33;
  the stop price the bot asked for is not.
- **Rules 27 and 34** — a partial fill, whose remedy depends on direction and is
  specified in full under `open_position` and `close_position` above: an entry
  remainder is cancelled and the position written from a re-read, an exit
  remainder is never abandoned and the position stays open reduced to the lots
  still held. rule 27 is a pointer to rule 34's exit clause and adds nothing of
  its own. A partial fill is alerted either way.

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

23. **Protective stop order rejected or unplaceable** → retry three times, then
    mark the position `stop_protection = 'LOCAL'`, alert, and enforce the stop by
    polling: `lifecycle.exits` returns `STOP_LOSS` for that position and
    `execution.orders.close_position` sells it. Never unwind a sound position
    because a secondary order failed.

26. **Stop order executed by the exchange** → not an error. Close the position
    from the fill with `exit_trigger = STOP_LOSS`, start the cooldown, alert.

27. **Exit order partially filled** → **see rule 34's exit clause, which is the
    live rule** (v1.73). An exit the bot is still pursuing is retried under rule
    4 for the lots still held; the position stays open, reduced to those lots.
    Until v1.73 this rule said "retry the remainder until flat. A half-exited
    position must never be a resting state", which contradicted the §4
    `execution.orders` contract for a **terminal** partial exit — an `EXIT` order
    settled `CANCELLED` or `REJECTED` with `0 < filled_lots < position.lots`
    "reduces the position to the unsold remainder and leaves it open… The sold
    slice's profit or loss is therefore **not booked**". That is the resting
    state this rule denied existed, and it is the implemented behaviour. A
    pointer rather than a deletion: the ordinal is frozen and rule 27 is cited
    elsewhere (#94). Whether the unbooked slice is acceptable is a separate,
    still-open money question; it is not settled by this amendment, and nothing
    here authorises a slicing loop in `close_position`.

33. **A recorded price comes from the broker, or the record stays pending.**
    Realised P&L, exit prices and commissions are written from what the broker
    reports it did — an order state, an executed stop, an operation — and never
    from a quote, a stop price, an entry price, or any other number the bot has
    to hand. Where the broker's own record is not yet available, the position
    stays open and the read is retried on the next cycle; **after three
    consecutive cycles in which the read is still unavailable, the owner is
    alerted once for that order, and the alert re-arms when the order settles
    (v1.75)**. A position closed a minute late is
    recoverable and a position closed at an invented number is not, because
    nothing downstream can tell the invented one from a real one. This rule
    generalises #4, #5, #8 and #11, which are four instances of the same
    mistake.

    **Where the bound applies, and where "cycle" is the wrong unit (v1.75).**
    "A bounded number of cycles" named no bound until v1.75, which is precisely
    what rule 2's own post-mortem calls the defect it was rewritten to remove —
    "a threshold no code implemented and no test could fail" — live one rule
    family over, on the path that decides whether a position stays open with real
    money in it (#112). Three, matching rules 1, 2 and 9, because they are the
    same shape and a second threshold in the same system is a second thing to
    remember. **The counted path is `execution.orders.resolve_unfinished`**,
    which runs once per cycle, already logs `order_unresolved` with an
    `age_seconds` computed from the order's own `created_at`, and already
    distinguishes "the broker cannot be reached" from "the broker says this order
    was never placed". That is the full set of three parts rule 36 requires:
    threshold, one alert, reset on settlement.

    **`broker.reconcile`'s `EXIT_UNRESOLVED` is not on this counter and is not
    a cycle.** Reconciliation runs at `app.startup` step 7 and appears in no
    `app.loops` task list, so its retry cadence is one per process start, not one
    per minute, and it alerts once per pass by construction. Suppressing it
    across *restarts* would require durable state — restarts are routine (failure
    class 15) — and whether an operator should stop being told about an
    unresolved exit because the process has bounced is a judgement about a real
    money-path alert, not a bug to be fixed in passing. **It is left alerting
    once per pass**, and the question of a durable, restart-surviving latch is
    recorded here as open and unowned. It is entangled with rule 25's open
    decision, which is what would put a reconciliation task on a cadence in the
    first place, and should be settled with it rather than before it.

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
- An entry whose fill price differs from `signal.reference_price` by more than
  `FILL_SLIPPAGE_ALERT_PCT` alerts the owner, and the position is still opened,
  still has its stop placed, and is **not** sold back — no cancel, no sell, no
  cooldown, and `close_position` is never called (proves the brief's alert-only
  policy; unwinding here would be a second real trade).
- A fill inside the tolerance raises no such alert, and the alert never changes
  the outcome of the entry either way (proves it is an alert, not a control).

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

**partial fills** (`execution.orders`)
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
