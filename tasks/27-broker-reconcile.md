# Task 27/42: Implement `zarabot/broker/reconcile.py`

## Product context

Sole owner of the reconciliations table, which it writes itself - there is no db.reconciliations repository. Compares broker truth against local belief on startup. Observes and reports only - it never places or cancels an order.

## Build order position

Module **27** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

### `reconciliations`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ran_at` | TEXT NOT NULL | |
| `adjustments` | TEXT NOT NULL | JSON array. Empty array means agreement |

**Content structure** of `adjustments` — one object per adjustment. **These ten
types are all of them (v1.77).** Until v1.77 this list held four, and the six it
omitted are exactly the ones §4 defines under `broker.reconcile`; `reporter` and
`app.startup` branch on `type`, and "an adjustment with no branch is silently
dropped" is what already happened to `STOP_DUPLICATE` (#35, #101). Every value
that is money or a price is a **decimal string**, never a number; `position_id`
and `lots` are integers; stop identifiers are the broker's `stop_order_id` where
it is known and our own key otherwise.

```
- closed externally: {"type": "CLOSED_EXTERNALLY", "ticker": "...", "position_id": 12, "exit_price": "123.45", "exit_at": "...", "exit_commission": "1.25"}
- exit unresolved:   {"type": "EXIT_UNRESOLVED", "ticker": "...", "position_id": 12, "reason": "..."}
- adopted:           {"type": "ADOPTED", "ticker": "...", "lots": 3, "average_price": "123.45"}
- foreign holding:   {"type": "FOREIGN_HOLDING", "ticker": "...", "lots": 3, "average_price": "123.45"}
- lot mismatch:      {"type": "LOTS_ADJUSTED", "ticker": "...", "position_id": 12, "from": 3, "to": 2}
- stop missing:      {"type": "STOP_MISSING", "ticker": "...", "position_id": 12}
- stop orphaned:     {"type": "STOP_ORPHAN", "ticker": "...", "stop_order_id": "..." | null, "key": "..."}
- stop mispriced:    {"type": "STOP_MISPRICED", "ticker": "...", "position_id": 12, "expected": "123.45", "actual": "123.40"}
- stop adoptable:    {"type": "STOP_ADOPTABLE", "ticker": "...", "position_id": 12, "stop_order_id": "..." | null}
- duplicate stops:   {"type": "STOP_DUPLICATE", "ticker": "...", "position_id": 12, "keep": "...", "cancel": ["...", "..."]}
```

- `ADOPTED` and `FOREIGN_HOLDING` carry the same fields and mean opposite
  things. `ADOPTED` is the crash-recovery case of rule 6 — a holding the bot
  recognises from its own unresolved `ENTRY` order, adopted into `positions`.
  `FOREIGN_HOLDING` is rule 32 — a holding the bot does not recognise, reported
  and **never** adopted, which `app.startup` names in its refusal. A reader that
  treats them alike undoes rule 32 (#91).
- `STOP_ORPHAN` names no `position_id`: the defining condition is that no open
  position matches it. It carries both identifiers, because `stop_order_id` is
  null for a stop that never reached the broker and `key` is then the only name
  it has. `STOP_ADOPTABLE`'s `stop_order_id` is null on the same terms.
- `STOP_DUPLICATE` is the only type carrying a list. `keep` is the single
  identifier to retain — the one matching the position's `stop_order_key`, or
  the oldest by `created_at` — and `cancel` holds every other identifier for that
  position. Both are identifiers in the sense above. The keeper is named here
  rather than derived by the caller, because the rule lives in `broker.reconcile`
  and a caller re-deriving it is how this type came to be skipped entirely (#35).
- No type carries a remedy, an instruction, or a broker call. Every one of them
  is an observation; `app.startup` step 7 performs the remedies through
  `execution.orders`.

**Retention.** No table is ever pruned. Growth is a few megabytes a year and the
historical record is the purpose of the project. Backups are retained 30 days.

---

## Module contract

### `zarabot/broker/reconcile.py`

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31). This module is not a `db.*` repository, but it
was one of the eight sites opening its own connection.

**Sole owner of the `reconciliations` table (v1.77).** This module writes it
with its own SQL — one `INSERT` per run, from `_persist` — and there is no
`db.reconciliations` repository. It is the only module that can produce the
row, because the row is the record of the comparison only this module performs.
The rulebook permits a module to own its own table; ownership was previously
recorded only in `interfaces.md`, which `make_tasks.py` does not read, so this
module's own task never carried it (#177). No other module writes
`reconciliations`; readers go through this module.

**`async reconcile(now: datetime) → ReconciliationReport`**
- Compares `broker.client.get_portfolio()` against `db.positions.list_open()`.
- **Locally-open but absent at the broker → the sale is resolved from the
  operations feed, or the position is not closed at all (v1.35).** Until now this
  booked the exit at `get_last_price` as of the moment of *detection* — which can
  be hours or days after the sale, and on a different day entirely if the bot was
  down — and fell back to the position's own `entry_price` when the broker was
  unreachable, recording an exit of exactly zero P&L. Both are numbers this
  module made up, which rule 33 forbids (#11).

  The resolution: `broker.client.get_operations(position.entry_at, now)`,
  filtered to the position's `figi` and to the sale operation types
  (`OPERATION_TYPE_SELL` and its `DELIVERY_SELL` and `SELL_MARGIN` variants).
  Over **every** such sale in the window —
    - `exit_price` is their **quantity-weighted average**;
    - `closed_at` is the **latest** of their timestamps, which is when the
      position left the account rather than when the bot noticed;
    - `exit_commission` is the sum of the fee operations whose
      `parent_operation_id` is one of those sales, passed through to
      `db.positions.close`.
  The window starts at `entry_at`, so a sale that happened at all is inside it,
  however long the bot was down — and since at most one position per ticker is
  open at a time, every sale of that instrument inside it belongs to this
  position. Deliberately **no** "take sales until their quantities cover the
  position" cutoff: whether the broker reports an operation's `quantity` in lots
  or in instrument units is an assumption this project has not tested against a
  live account, and it is the same class of assumption that produced #39 and
  #43. A weighted average is correct under either reading, because the units
  cancel; a cutoff is not. This is the one place a weighted price is legitimate,
  and it is legitimate because every input is a number the broker reported about
  a trade that occurred — not, as in #10, a blend across orders invented to fit
  a signature.

  The close still passes `order = None`. This module records **no** order row: it
  did not submit one, and inventing one would contradict its own prohibition on
  trading.

  **This close starts the ticker's cooldown, and that is a write this contract
  now admits (v1.78).** `db.cooldowns.start(position.ticker, sale.occurred_at)`
  is called immediately after the close, from the instant of the *sale* and not
  the instant of detection, for the same reason the exit price comes from the
  operations feed. A position that left the account is a position the bot just
  exited, and every other exit path starts a cooldown; skipping it here would let
  the next cycle re-enter a ticker the account sold minutes ago. Until v1.78 the
  write was in the code and in no contract (#110, failure class 2), which is the
  worst arrangement available: the next agent rebuilding this module from its
  contract deletes the line, and nothing says it was ever required.

  **A failed cooldown write is not remedied here — it propagates (v1.78).**
  Cooldowns are rule 11, and this module has no halt path of its own. The
  `aiosqlite.Error` leaves `reconcile`, `app.startup` fails at the `reconcile`
  stage and refuses to start, which satisfies rule 11's "stop opening anything"
  by the strongest means available: the bot does not run. This is sound **only
  while reconciliation runs exclusively at startup**, which is what rule 25's
  open decision (a) currently fixes. **Open decision — not settled here.** If
  rule 25 is ever resolved as (b), a named `app.loops` task calling reconcile on
  a cadence, then a propagating cooldown failure mid-session is an exception in a
  supervised loop and not a halt, and this module needs an explicit rule-11
  remedy of the shape `execution.orders` has. That is a money-path decision, it
  is the owner's, and it is bound to rule 25's: whoever settles rule 25 settles
  this. No agent may add a halt path here on its own reading.
- **A sale that cannot be resolved is reported, not booked.** When the feed
  returns no covering sale, or is unavailable, the position **stays open** and
  the report carries
  `{"type": "EXIT_UNRESOLVED", "ticker", "position_id", "reason"}`, with an
  alert. Rule 33 already settles which way this falls: a position closed a cycle
  late is recoverable and one closed at a substituted number is not, because
  nothing downstream can tell the substituted number from a real one. A covering
  sale that is genuinely absent means the shares left the account by some route
  that was not a trade, and that is the owner's to explain rather than this
  module's to guess.
- `EXIT_UNRESOLVED` does **not** stop the bot, unlike rule 32's foreign holding.
  That refusal exists for shares the bot might trade; this is a row describing
  shares the account no longer has. While it stands, the row still marks to
  market in `pnl.bot_equity` and `lifecycle.exits` may eventually try to sell it,
  which the broker will refuse. Both are visible and alerted, and that is a
  different kind of wrongness from a fabricated exit price written permanently
  into the trade history.
- **Present at the broker but unknown locally → reported as `FOREIGN_HOLDING`,
  never adopted.** This module previously called `db.positions.adopt` here, which
  derived a stop and target from the holding's *average cost* and so handed the
  next trading cycle a position already past its take-profit. Adoption of an
  unknown holding is no longer this module's decision or anyone else's: the
  account is the bot's alone, and a holding it does not recognise is a condition
  to report, not inventory to manage. The adjustment names the ticker, the lot
  count and the average price, so `app.startup` can name them in its refusal.
  `db.positions.adopt` remains in the contract and is still called for a holding
  the bot **does** recognise but whose local row is missing — the crash-recovery
  case it was written for, and the whole subject of **rule 6** (v1.73). Rule 6
  covers the recognised-order path only; the unrecognised holding is rule 32's,
  and the two must not be collapsed into one. This module passes it the key of that recognising
  order (v1.38): the recognition rule already identifies exactly one order, so
  the key is in hand at the moment the decision is made, and it is what the
  adopted position must point at. Where more than one unresolved `ENTRY` order
  exists for a ticker — which the per-ticker submission lock should prevent — the
  **oldest by `created_at`** is used, the same tie-break as `STOP_DUPLICATE`.
- **A holding is recognised when the bot has an unresolved `ENTRY` order for that
  ticker** — `SUBMITTING` or `SUBMITTED` in `db.orders.list_unresolved()`. That
  is the residue of exactly one sequence: the bot submitted the buy, the broker
  filled it, and the process died before the position row was written. Anything
  else at the broker is foreign, including a holding whose entry order has
  already reached a terminal status, because the bot then either has its position
  row or has decided it does not. The rule is deliberately the narrowest one that
  covers crash recovery: every widening of it is a way for a holding the owner
  bought to be treated as the bot's.
- Lot mismatch → the broker's count is written locally.
- **Stop orders are reconciled too, but this module does not act on them.**
  Every open position must have exactly one live stop order. This module
  *reports* each discrepancy — a position with no stop, a stop with no position,
  a stop at the wrong price, **or more than one live stop on the same position** —
  and the caller performs the remedy through
  `execution.orders`, which is the only module permitted to place or cancel
  orders. Keeping the *order* paths out of this module is what allows every
  remedy to be applied by the one module permitted to trade.

  **"Observational" means it places no orders. It is not read-only (v1.78).**
  This module closes positions, adopts them, adjusts lot counts, starts
  cooldowns and writes `reconciliations` — it is a writer of trading-critical
  state under rule 11, and running it is not a diagnostic. The earlier claim
  that it could "run anywhere, including read-only diagnostics, without
  financial side effects" was true of the class it checked, orders, and read as
  covering all writes (#110, failure class 6). Only the never-place-or-cancel
  line below is binding; nothing in this module is safe to run against a live
  account for a look.
- On restart an existing stop is **adopted** rather than replaced — two stops on
  one position would sell it twice.
- **A stop is mispriced only when it differs from the position's stop by a full
  price increment or more (v1.55).** The broker snaps a posted stop to the
  instrument's `min_price_increment`, so the price it holds is almost never the
  price the bot computed: on 2026-09-07 the account held GMKN at 125.44 against
  a stored 125.457, SBER at 265.89 against 265.8955 and MTSS at 179.05 against
  179.075. An exact inequality called all three mispriced on every startup, and
  the caller's remedy — cancel then re-post — left three live positions
  momentarily unprotected once per restart, wrote a fresh `stop_orders` row each
  time, and did it for stops the broker had placed exactly as asked. The
  comparison is therefore `abs(broker − local) < min_price_increment`, read from
  `broker.client.get_instrument(ticker)` for the position's ticker. Nothing is
  rounded anywhere: the bot does not know which way the broker rounds, and
  writing a guessed rounded price into `positions` or `stop_orders` would put an
  invented number in the record, which rule 33 forbids. A tolerance costs
  nothing here because the bot never moves a stop after entry — a genuinely
  wrong stop is wrong by the distance between two different prices, not by less
  than one tick.
- **A stop whose price cannot be compared is neither mispriced nor adoptable
  (v1.56).** When `get_instrument` fails for the position's ticker, or reports a
  `min_price_increment` of zero or less, that position's stop price is not
  judged: **no `STOP_MISPRICED` and no `STOP_ADOPTABLE`** are reported for it.
  The failure is alerted and reconciliation continues; `STOP_DUPLICATE` and
  `STOP_ORPHAN` are unaffected, because neither depends on the price.

  Withholding `STOP_MISPRICED` alone was the v1.47 defect. The price comparison
  **is** the guard on adoption — `STOP_ADOPTABLE` means "this stop stands at the
  price the position wants, bind it" — so suppressing only the misprice finding
  routed an unjudged stop into the `elif` beneath it and reported it adoptable.
  `app.startup` then calls `adopt_existing_stop`, which sets
  `stop_protection = EXCHANGE`, and `lifecycle.exits` fires `STOP_LOSS` only
  while protection is `LOCAL`. A stop standing at a price nobody could verify
  would have become the position's sole protection, and the bot would have
  stopped watching its own.

  Not adopting is the safe residual **for a `LOCAL` position**: it stays `LOCAL`,
  `lifecycle.exits` keeps watching `stop_price` itself, and the broker's stop
  stands underneath as well.

  A position already `EXCHANGE`-protected has a different residual, and it is
  weaker: it stays `EXCHANGE` and nothing at the bot verifies its stop until the
  metadata is readable again, because `lifecycle.exits` fires `STOP_LOSS` only
  while protection is `LOCAL`. It is **not** demoted to `LOCAL` to close that
  gap. Demotion would arm the bot's own seller while the exchange's stop is
  still live, which is the double-sell condition the ownership rule exists to
  prevent — a worse failure than an unverified stop that is, after all, still
  standing at the exchange. This is the same trade rule 38 makes about
  `STOP_MISPRICED`, and it is stated here because "the unjudged case protects
  itself" is true of one entrance to this branch and not the other (v1.57).

  Either way the next reconciliation with readable metadata judges the stop
  properly and adopts it, reports it mispriced, or leaves it alone. Neither
  finding is skipped because it is unlikely — both are skipped because both
  remedies act on a price, and the price is exactly what is missing.
- **More than one live stop on a position is reported as `STOP_DUPLICATE`**, and
  is the most serious discrepancy this module can find: it is the double-sell
  condition the ownership design exists to prevent, actually present. The remedy
  keeps the stop whose key matches the position's recorded `stop_order_key`, or
  the oldest if none matches, and cancels every other. A duplicate must never be
  silently skipped as though it were the position's one legitimate stop.
- **The `STOP_DUPLICATE` adjustment names the keeper.** It carries `keep`, the
  identifier of the stop to retain, and `cancel`, the identifiers of every other.
  This module applies the keep-rule because this module is where the rule is
  written and where `stop_order_key` and `created_at` are already in hand;
  emitting an undifferentiated list of identifiers forced the caller either to
  re-derive the rule or, as happened, to skip the adjustment entirely (#35).
- Returns a report enumerating every adjustment; an empty report means agreement.
- **After persist, emit `reconciliation` (INFO) with `adjustments_count` and
  `types` (the distinct adjustment type names) (v1.61).** Empty agreement still
  emits, with count 0 and `types` an empty list — silence here is how a failed
  reconcile looks like a skip.
- **Emits `stop_order_executed` when it books an exchange-fired stop close, and
  `stop_order_orphaned` when it reports `STOP_ORPHAN` (v1.61).** Fields match
  §7.1.
- Idempotent.
- Ordering constraint: runs during `app.startup` after migrations and after
  unresolved-order recovery, and before any entry is permitted.
- Must never place or cancel an order, including stop orders. Reconciliation
  observes and records; it does not trade. Every remedy it identifies is carried
  out by `app.startup` through `execution.orders`.

**This module owns rules 7, 24 and 25 as their detector (v1.75).** All three are
disagreements between broker truth and local belief, and this is the only module
that compares the two:

- **Rule 7** — broker and database disagree on a position or a quantity. The
  broker wins, the local record is corrected, and the report names specifics so
  the owner's alert says which ticker moved from what to what. A quantity
  mismatch is `LOTS_ADJUSTED`; a position the broker does not hold is the
  operations-feed path above.
- **Rule 24** — a stop order with no matching open position is reported
  `STOP_ORPHAN` and alerted. A live stop against a position that no longer exists
  can sell stock the account does not hold, which is why it is remedied rather
  than merely noted.
- **Rule 25** — an open `EXCHANGE`-protected position with no live stop is
  reported `STOP_MISSING`. Rule 25's own text carries the open decision about
  *when* the replacement happens and it is not settled here.

The cancel in rule 24 and the placement in rule 25 are **not** performed here:
this module's never-place-or-cancel line is binding and older than either rule.
`app.startup` step 7 applies both remedies through `execution.orders`. The
detector owns the rule because the detector is the module that can be wrong about
whether the condition exists at all — the remedy is a call it names, and step 7
already names it.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

6. **A holding at the broker that an unresolved `ENTRY` order of ours
   recognises** → adopt the position from that order's key, alert. Never ignore.
   This is the crash-recovery case and nothing else: the bot submitted the buy,
   the broker filled it, and the process died before the position row was
   written, so an unresolved `SUBMITTING`/`SUBMITTED` `ENTRY` order for that
   ticker is still standing. `broker.reconcile` recognises exactly this case,
   reports it, and `app.startup` applies the remedy through
   `db.positions.adopt`; the recognition test and its tie-break are in
   `broker.reconcile`'s §4 contract. **A holding no such order recognises is not
   covered by this rule — it is rule 32's, and is never adopted** (v1.73).
   Until v1.73 this rule read "order found at the broker that is unknown locally
   → adopt the position", which covered rule 32's foreign holding as well and so
   ordered the adoption from average cost that rule 32 exists to prevent (#91).
   The two failures are distinct: this rule is about an order the bot itself
   submitted, rule 32 about inventory the bot has no record of asking for.

7. **Broker and database disagree on positions or quantities** → the broker wins,
   the local record is corrected, and the owner is alerted with specifics.

24. **Stop order found with no matching open position** → cancel it as an orphan
    and alert. A live stop against a position that no longer exists can sell
    stock the account does not hold.

25. **Open position found with no live stop order** while
    `stop_protection = 'EXCHANGE'` → place a replacement and alert. An
    unprotected position is the state this whole mechanism exists to prevent.
    **Who detects, who remedies, and when (v1.73).** The detector is
    `broker.reconcile`, which reports the discrepancy and, by its own contract,
    **must never place or cancel an order**. The remedy is applied by the caller
    through `execution.orders`, the only module permitted to place orders —
    today that caller is `app.startup` step 7, and `app.loops.run`'s task list
    contains no reconciliation task. So "immediately" today means "at the next
    process start", and a stop cancelled at the exchange mid-session leaves the
    position unprotected until then: `stop_protection` still reads `EXCHANGE`,
    so `lifecycle.exits` declines to fire `STOP_LOSS` and neither side is
    watching (#95). Two things hold under either resolution below and are
    binding now: `broker.reconcile` never places or cancels, and
    `lifecycle.exits` never sells an `EXCHANGE` position because its stop
    vanished — the exchange may still hold it, and a double sell is worse than
    an unprotected one.
    **Open decision — not settled here.** Either (a) rule 25 means "before the
    process serves traffic", the startup-only reading, and the mid-session hole
    is accepted until restart; or (b) a named `app.loops` task calls a narrow
    replace path on a cadence — place a stop only, never sell — which requires a
    new loops/reconcile split. This is a money-path behaviour decision and is
    the owner's to make. Until it is made, implement (a): that is what the built
    code does, and no agent may add a mid-session replace path on its own
    reading of the word "immediately". §3.2 gains a test once the choice is
    made — under (a) that loops do not replace, under (b) that a missing stop
    mid-session is replaced without a sell.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

32. **The broker reports a holding the bot has no record of at startup** →
    refuse to start, alert, and name every ticker, unless
    `config.allow_foreign_holdings` is true. The account is the bot's alone
    (brief v1.8). The bot cannot distinguish "someone bought this by hand" from
    "local state is wrong", and both readings forbid trading it. When the flag is
    set, the holdings are named in the ready alert and are never traded: no stop
    placed, no exit evaluated, no sale made. Never adopt one — adoption derived a
    stop and target from the holding's average cost, which handed the next cycle
    a position already past its take-profit.

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

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Broker and database agreeing produces no adjustments and no alert (happy path)
  and still emits `reconciliation` with `adjustments_count` 0 (v1.61).
- A position open in the database but absent at the broker, whose operations feed
  shows a sale, is closed locally as externally closed and alerted (proves the
  broker is authoritative).
- That close records the sale's price of 110, not the `get_last_price` of 92 at
  the moment of detection, and dates `exit_at` to the sale rather than to `now`
  (proves the exit is booked at what was traded and when — #11's own verification
  case).
- Two sales of quantity 3 and 2 at 100 and 90 record a quantity-weighted 96.00
  and an `exit_commission` summing both fee operations (proves aggregation over
  the feed, and that the fee is no longer lost for want of a closing order row).
  The weights are the broker's `quantity` values whatever unit they are in, so
  the case does not encode an untested assumption about that unit.
- The broker being unavailable leaves the position **open**, reports
  `EXIT_UNRESOLVED` and alerts — and in particular records no exit at
  `entry_price` (proves the zero-P&L fabrication is gone).
- An operations feed containing no covering sale for the figi behaves identically
  (proves absence and unavailability are both "unknown", never "zero").
- A holding present at the broker but absent locally is reported as
  `FOREIGN_HOLDING` naming its ticker, lots and average price, and **no position
  row is written** (proves the bot no longer takes ownership of shares it did not
  buy — the path that adopted a manual holding at its cost basis and sold it on
  the next cycle).
- A holding 40% above its average cost is reported, not adopted, and no exit is
  submitted for it (proves the specific liquidation this policy exists to
  prevent).
- A holding whose ticker has an unresolved `ENTRY` order is adopted rather than
  reported foreign (proves crash recovery still works: the bot bought this, the
  fill landed, and the process died before the row was written).
- The adopted position's `open_order_key` is that unresolved order's key, not a
  synthesised one (proves the adopted row points at the order the bot actually
  submitted — the whole reason the foreign key exists).
- With two unresolved `ENTRY` orders for one ticker, the **oldest** is used
  (proves the documented tie-break, in the state the submission lock is supposed
  to make impossible).
- A holding whose entry order has already reached a terminal status is reported
  foreign (proves the recognition rule is the narrow one, and cannot be widened
  into adopting what the owner bought).
- An externally-closed position is closed with `order = None` and **no row is
  written to `orders`** (proves reconciliation records only what the bot actually
  submitted).
- Two live stops on one open position report `STOP_DUPLICATE` naming both, with
  `keep` set to the one matching the position's `stop_order_key` and `cancel`
  listing the rest (proves the double-sell condition is detected rather than
  half-claimed, and that the caller is told which stop to keep rather than left
  to re-derive the rule).
- With no `stop_order_key` recorded, `keep` is the oldest stop by `created_at`
  (proves the documented tie-break).
- A stop the broker holds one **half** increment below the position's stop price
  reports **no** `STOP_MISPRICED` (proves the tick-snapped price the broker
  actually holds is not read as a discrepancy — the finding that cancelled and
  re-posted all three live stops on every restart).
- A stop a **full** increment away is still reported `STOP_MISPRICED` (proves the
  tolerance is one increment and not an open-ended blur).
- A stop within the increment on a `LOCAL` position is reported `STOP_ADOPTABLE`
  (proves the tolerated stop takes the adoption path, not the replacement one).
- When `get_instrument` fails for the position's ticker, no `STOP_MISPRICED` is
  reported, the failure is alerted, and reconciliation still returns its other
  findings (proves an unmeasurable discrepancy does not become a cancel-and-
  re-post).
- A `LOCAL` position whose increment cannot be read reports **no
  `STOP_ADOPTABLE`** either, and its `stop_protection` is still `LOCAL` after
  reconciliation (proves the price comparison guards adoption as well as
  replacement — the v1.47 defect that would have handed protection to a stop
  standing at a price nobody could verify, and stopped `lifecycle.exits`
  watching the position's own).
- An instrument reporting a `min_price_increment` of zero is treated exactly as
  an unreadable one, alert included (proves the blind path has one entrance,
  not one alerted and one silent).
- A lot-count mismatch adopts the broker's count and alerts (proves quantity
  reconciliation).
- Reconciliation applies each **corrective write** at most once: running it twice
  against an unchanged broker closes, adopts or re-lots nothing the second time
  (proves it does not thrash). Stop-order *findings* are re-reported until the
  caller remedies them, which is correct — this module observes, and an
  unremedied discrepancy is still true on the second pass.
- The module calls `aiosqlite.connect` nowhere; the reconciliation row is written
  on `db.connection.shared()` (proves the shared connection reached the two
  modules outside `db.*` that were opening their own).

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
