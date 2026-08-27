# Task 1/40: Implement `zarabot/models.py`

## Product context

Shared domain types. Every other module's signatures are written in these types, so this is the vocabulary the whole system speaks.

## Build order position

Module **1** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/models.py`

Domain types shared across every module. Contains validation only, never logic.

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

`OperationRecord` carries the broker's actual commission. `TradingCalendar` is
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
