# Task 1/42: Implement `zarabot/models.py`

## Product context

Shared domain types. Every other module's signatures are written in these types, so this is the vocabulary the whole system speaks.

## Build order position

Module **1** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/models.py`

Domain types shared across every module. Contains validation only, never logic.

**Owns rule 22 (v1.75).** Every model carrying a `datetime` rejects a naive one
with `ValueError` at construction, and no model coerces one to a guessed
timezone. This is where the rule lands because these types are the boundary every
module's signature is written in: a naive instant that reaches a model has
already crossed from the module that made it into the module that will store it,
and the point of failing here is that the traceback names the producer rather
than the reader. It is a programming defect, not a runtime condition — nothing
catches it, and no module handles it.

**Enumerations**
- `Side` — `BUY`, `SELL`
- `OrderStatus` — `SUBMITTING`, `SUBMITTED`, `FILLED`, `REJECTED`, `CANCELLED`, `UNKNOWN`
- `ExitTrigger` — `STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`, `EXTERNAL`
- `RejectionReason` — `HALTED`, `SESSION_CLOSED`, `INSTRUMENT_NOT_TRADING`, `DUPLICATE_TICKER`, `MAX_POSITIONS`, `COOLDOWN_ACTIVE`, `INSUFFICIENT_CASH`, `ZERO_LOTS`, `PORTFOLIO_EXPOSURE`, `BROKER_LOT_LIMIT`
- `HaltReason` — `DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`
- `StopOrderStatus` — `PLACING`, `ACTIVE`, `CANCELLED`, `EXECUTED`, `ORPHANED`, `FAILED`
- `StopProtection` — `EXCHANGE`, `LOCAL`. Which side owns a position's stop trigger

`POSITION_CAP` was removed in v1.30 and `PORTFOLIO_EXPOSURE` takes its place.
The old reason was unreachable: `config.load()` refused any configuration where
`POSITION_SIZE_PCT` exceeded `MAX_POSITION_PCT`, so the per-position cap was
never the binding minimum and no order could ever be rejected for it — while the
risk summary the bot shows on `/resume` listed it as an active control (#15). The
new reason can bind, because the ceiling it enforces is on the **portfolio**, and
the portfolio grows independently of any one order's size. The `signals` table
does not enumerate rejection reasons in a CHECK constraint, so no migration is
required; a value the schema would still accept but no code can produce is inert.

`BROKER_LOT_LIMIT` covers the broker refusing the size outright — its maximum
for the account is zero lots. It is distinct from `ZERO_LOTS`, which means our
own sizing arithmetic produced nothing affordable; the two have different causes
and only separate reasons make the rejection log diagnostic.

**Frozen dataclasses** — `Candle`, `Instrument`, `Signal`, `Position`,
`OrderRecord`, `StopOrderRecord`, `OperationRecord`, `PortfolioState`,
`SessionInfo`, `RiskDecision`, `HaltState`, `ReconciliationReport`,
`TradingCalendar`, `BacktestResult`.

**`SessionInfo` carries one predicate over its own two fields (v1.76).**

**`in_closing_window(now: datetime, minutes: int = 15) → bool`** — a method on
`SessionInfo`. True during the final `minutes` of that session, inclusive of the
window start and exclusive of `end`; `False` when the day is not a trading day or
either instant is absent. Raises `ValueError` on a naive `now`. No I/O, no clock,
no configuration — it reads `start`, `end` and its two arguments and nothing
else, which is why it is a comparison over the dataclass's own fields rather than
the "logic" the paragraph above excludes.

This is written down because `lifecycle.exits.evaluate` calls
`session.in_closing_window(now)` on a `SessionInfo` (§4 `lifecycle.exits`), and
until v1.76 the only function of that name the spec defined was
`market.session.in_closing_window(now, minutes)` — a module function, in a module
that does I/O, taking two arguments where the caller passes one (#100). The call
therefore resolved to nothing in the spec, and an implementer either invented a
method on a type §4 granted none, or made the pure exit path call an I/O module.
The built code has had the method since `models` was written
(`SessionInfo.in_closing_window`, recorded in `interfaces.md`); the spec is
catching up to it, and `market.session.in_closing_window` stays as the
module-level convenience that resolves "the current session" and delegates here.

**`SessionInfo` carries `trade_date: date` as its first field (v1.81).** The
fields are, in order, `trade_date`, `start`, `end`, `is_trading_day`.
`trade_date` is the Moscow calendar date the entry describes. It is present on
**every** `SessionInfo`, open or closed, and is never `None`: it is a
`date`, not `date | None`, and it carries **no default value**.

- Until v1.81 the type had no date at all, and a non-trading day was therefore
  `SessionInfo(start=None, end=None, is_trading_day=False)` — three values, none
  of which says *which day*. Every closed day was consequently
  indistinguishable from every other, which is #51: `market.session.calendar()`
  deduped on a sort key that was `datetime.min` for all of them and returned one
  closed day for a fortnight containing four. The same gap is why
  `db.trading_days` could record only trading days, and why `session_closed` had
  to derive its `trade_date` from the clock (v1.67) rather than from the entry it
  was describing.
- The date was never missing from the broker's answer; it was discarded on the
  way in. `TradingDay.date` is populated and correct on a closed day, measured
  against the live account (§2.1), and `broker.client.get_trading_schedule` read
  fields 2, 3 and 4 and ignored field 1.

**Why first, and not last.** Field order in a dataclass with no defaults is a
choice, and this one was made on three grounds rather than on diff size. Diff
size does not in fact separate the options: every construction site in the
repository already passes `start`, `end` and `is_trading_day` **by keyword**, so
each site gains exactly one `trade_date=` argument wherever the field sits, and
no site changes meaning. What separates them is that `trade_date` is the entry's
**identity** — it is the primary key of `trading_days` (§5), the uniqueness key
of `calendar()`, and the only field of a closed day that is neither `None` nor
`False` — and identity reads first. It also puts the dataclass in the column
order of the row it maps onto, which `db.trading_days` converts in both
directions. A closed day then reads `SessionInfo(trade_date=…, start=None,
end=None, is_trading_day=False)`: which day, then that nothing happened on it,
rather than the only meaningful value trailing two nulls.

**The absence of a default is load-bearing.** `trade_date: date` with no default
makes a construction site that forgets it fail at construction with `TypeError`,
naming the producer. A `date | None = None` would let every site that was not
updated keep compiling and put the project back where #51 started, with the
difference that the `None` would now be written to a primary key. No producer may
supply `None`, and no reader may accept it.

**The `is_trading_day` invariant is an obligation on producers, not a
`__post_init__` check (v1.81).** Where `is_trading_day` is true,
`trade_date` **must** equal `clock.moscow_date(start)`. That is not validated
here, and cannot be: `clock` imports `models` (`zarabot/clock.py` imports
`TradingCalendar`), so `models` importing `clock` is a cycle, and re-deriving the
Moscow conversion inside `models` would make it the second owner of an
arithmetic `clock` is the single owner of (§Global conventions) in a module whose
first line is that it holds validation and never logic. The obligation therefore
falls on every producer of a `SessionInfo`, and each is named in its own
contract: `broker.client.get_trading_schedule` (§4), `db.trading_days`
(§4, both directions), `sandbox.exchange`, and every test fixture. §3.2 pins it
at the producers for that reason — an invariant stated nowhere but here would be
an obligation with no owner.

What `models` **does** enforce, because it needs no clock to do it: `trade_date`
is a `date` and not a `datetime`. `datetime` is a subclass of `date`, so
`isinstance` accepts one silently, and a `datetime` here would key
`trading_days` on an ISO string with a time in it and make two observations of
one day two rows. Construction with a `datetime` — or with anything that is not a
`date` — raises `ValueError`, consistent with rule 22's posture that the
traceback should name the producer.

**Deliberately not added:** a `models` invariant forbidding a closed day from
carrying `start` or `end`. That shape is impossible from the only production
producer — `get_trading_schedule` returns `start=None, end=None` whenever
`is_trading_day` is false, and §3.2 already pins that — and the shape's real cost
was a test fixture, which is closed at the fixture in `market.session`'s §3.2
rather than by narrowing the type. Stated so that a later reader knows it was
weighed, not missed.

`OrderRecord` carries `broker_order_id` and `commission_alerted_at` (v1.39).
`key` is the bot's own idempotency key, and for a row describing an execution the
**exchange** performed — a stop the broker fired on the bot's behalf — the broker
has never seen that key, so nothing could ever re-query the row. Its commission
was therefore unrecoverable if it landed late, and the "commission still unknown"
alert repeated on every backfill run, forever, once per stop-loss exit ever taken
(#8). `broker_order_id` is the broker's own identifier for the order where the
bot knows it; `commission_alerted_at` records that the owner has been told once.
Both are `None` where they do not apply. `commission_alerted_at` is
notification bookkeeping rather than a trading fact, and it lives on the row
anyway because an alert that repeats forever is equivalent to no alert, and
"have I already said this" is a fact about the row that must survive a restart.

`OperationRecord` carries `operation_type` and `state` as well as the broker's
actual commission (v1.35). It used to carry neither, so the only way to tell a
sale from a purchase was the sign of `payment`, and the only way to find a fee
was to look for the substring `FEE` in a name the dataclass did not expose. Both
are now explicit and both come from the broker verbatim: `operation_type` is the
`OperationType` member's name (`OPERATION_TYPE_SELL`, `OPERATION_TYPE_BROKER_FEE`
and so on) and `state` is the `OperationState` member's name.
`broker.reconcile` has to identify one specific sale of one specific instrument
to book an external close at the price it actually happened at (#11), and the
sign of a payment is not a thing to build a money number on. `OperationRecord`
also carries `parent_operation_id`, which is how a fee is tied to the trade that
incurred it. `TradingCalendar` is
the queried schedule that `clock.trading_days_between` and `market.session` read.
`AppContext` (the assembled dependencies) and `LoadedModel` (an ML model plus its
feature manifest) are **not** domain types — they live with `app.startup` and
`strategies.ml_model` respectively, and no other module constructs them.

- Every monetary field is `Decimal`; every timestamp field is timezone-aware.
- Construction with a naive datetime raises `ValueError`.
- Construction with a negative lot count, negative price, or non-positive lot
  size raises `ValueError`.
- `RiskDecision` is either approved with a positive lot count, or rejected with a
  `RejectionReason`. It can never be both, and never neither.
- Must never contain a method that performs I/O.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

22. **A naive datetime crosses a module boundary** → `ValueError`. This is a
    programming defect, not a runtime condition; it fails loudly rather than
    being coerced to a guessed timezone.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A dataclass constructed with a naive datetime raises `ValueError`, for every
  type carrying a timestamp (proves the timezone invariant is enforced at the
  boundary rather than trusted).
- A negative lot count, a negative price, or a non-positive lot size raises
  `ValueError` (proves the domain rejects impossible values before they can
  reach an order).
- A monetary field given a `float` raises `TypeError` (proves the Decimal rule
  is enforced by the type, not by discipline — this is the test that stops a
  float leaking in from the SDK or a JSON payload).
- Every dataclass is frozen: assigning to a field raises (proves domain objects
  cannot be mutated in place behind a caller's back).
- `RiskDecision` cannot be constructed both approved and rejected, nor neither
  (proves the decision is total — every signal gets exactly one outcome).
- An approved `RiskDecision` with a lot count of zero raises (proves approval
  always means a placeable order).
- Every enum member round-trips through its string value unchanged (proves the
  values written to the database and read back are stable, since the schema
  stores them as TEXT with CHECK constraints naming them).
- A `Position` with `stop_protection = EXCHANGE` and no stop order key raises,
  as does `LOCAL` with one (proves the ownership pairing at the type level, not
  only in the repository).
- An `OperationRecord` round-trips `operation_type`, `state` and
  `parent_operation_id` as the broker's own values (proves a sale is identified
  by what the broker called it, not inferred from the sign of `payment`).
- A `SessionInfo` constructed without `trade_date` raises `TypeError` — the
  dataclass's own, because the field has no default — and one constructed with
  `trade_date=None` raises `ValueError` (v1.81; proves the field admits neither
  omission nor null, the two ways #51 could come back, the second of them
  writing a `None` into a primary key).
- A `SessionInfo` given a `datetime` for `trade_date` raises `ValueError`
  (v1.81; proves the check rejects a `datetime`, which `isinstance(x, date)`
  accepts — a `datetime` here keys `trading_days` on an ISO string with a time
  in it and makes two observations of one day two rows).
- A closed `SessionInfo` — `start=None`, `end=None`, `is_trading_day=False` —
  constructs successfully and reports its `trade_date` (v1.81; proves the type
  can say *which day* about a day with no session, which is the whole of #51).
- `trade_date` is the **first** field: two `SessionInfo`s differing only in
  `trade_date` are unequal, and the field order is `trade_date`, `start`, `end`,
  `is_trading_day` (v1.81; proves the ordering decision is pinned rather than
  incidental, so a later edit that moves it fails here rather than at a
  construction site).

## Expected output

- `zarabot/models.py` implementing the contract exactly
- `tests/test_models.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_models.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/models.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
