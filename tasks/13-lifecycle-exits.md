# Task 13/38: Implement `zarabot/lifecycle/exits.py`

## Product context

Pure. Decides which of the three exit triggers fires. Returns STOP_LOSS only for LOCAL-protected positions. 95% coverage.

## Build order position

Module **13** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/lifecycle/exits.py`

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int, config: Config) → ExitTrigger | None`**
- Pure. Returns the trigger that fires, or `None`.
- `STOP_LOSS` when `price ≤ position.stop_price` **and only when
  `position.stop_protection == 'LOCAL'`**. When the exchange holds the stop, this
  module must never return `STOP_LOSS`: the trigger has exactly one owner at a
  time, and both acting on the same position would sell it twice. Ownership is
  recorded on the position, not inferred.
- `TAKE_PROFIT` when `price ≥ position.target_price`.
- `MAX_AGE` when `trading_days_open ≥ MAX_HOLDING_DAYS` **and**
  `session.in_closing_window(now)`.
- Precedence when more than one applies: `STOP_LOSS`, then `TAKE_PROFIT`, then
  `MAX_AGE`. Fixed, so the recorded reason never depends on evaluation order.
- Boundaries are inclusive at the stop and the target.
- Must never place an order, and must never consult a halt — an active halt does
  not suppress exits.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Price exactly at the stop level triggers `STOP_LOSS`; one increment above does
  not (boundary).
- Price exactly at the target triggers `TAKE_PROFIT`; one increment below does
  not (boundary).
- A position whose age reaches the maximum during the final fifteen minutes
  triggers `MAX_AGE`; the same position earlier in that session does not
  (boundary, proves the timing rule).
- A position at both stop and maximum age returns `STOP_LOSS` (proves the
  documented precedence, so the recorded reason is deterministic).
- Age is counted in trading days: a position opened Friday is not aged by the
  weekend (proves calendar-aware ageing).
- An adopted position ages from its adoption timestamp (proves the reconciliation
  interaction).
- Evaluation is pure and repeatable for identical inputs.

## Expected output

- `zarabot/lifecycle/exits.py` implementing the contract exactly
- `tests/test_lifecycle_exits.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_lifecycle_exits.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/lifecycle/exits.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
