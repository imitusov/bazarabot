# Task 10/39: Implement `zarabot/db/signals.py`

## Product context

Records every signal with its risk decision, approved or rejected. Rejections are analysed in the weekly report.

## Build order position

Module **10** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `signals`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ticker` | TEXT NOT NULL | |
| `strategy` | TEXT NOT NULL | |
| `generated_at` | TEXT NOT NULL | UTC |
| `reference_price` | TEXT NOT NULL | Price the strategy saw |
| `decision` | TEXT NOT NULL | CHECK IN (`APPROVED`, `REJECTED`) |
| `rejection_reason` | TEXT NULL | Non-null exactly when `decision = 'REJECTED'` |
| `lots` | INTEGER NULL | Non-null exactly when approved |
| `order_key` | TEXT NULL | FK → `orders(key)` when an order followed |

## Module contract

### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`

**`async record(signal: Signal, decision: RiskDecision) → None`** — stores every signal, approved or rejected, with its reason.

**`async list_for_period(start: date, end: date) → list[...]`** — for the weekly report.

**`async write_daily(snapshot) → None`** — upserts on the Moscow date; a second write for the same date updates rather than duplicates.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A rejected signal is stored with its rejection reason and is retrievable by day
  (proves rejections are analysable, as the brief requires).
- A daily snapshot written twice for the same date updates rather than duplicates
  (proves the date is the key).

## Expected output

- `zarabot/db/signals.py` implementing the contract exactly
- `tests/test_db_signals.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_signals.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/signals.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
