# Task 2/39: Implement `zarabot/clock.py`

## Product context

Sole owner of 'now' and of trading-day arithmetic. Everything else receives time rather than reading it, which is what makes the system testable and backtestable.

## Build order position

Module **2** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/clock.py`

Single owner of the current time and of trading-day arithmetic.

**`now() → datetime`**
- Returns the current instant, timezone-aware, in UTC.
- The only place in the codebase permitted to read the system clock.
- In tests and backtests this module is substituted; no other module may be.

**`to_moscow(moment: datetime) → datetime`**
- Converts a timezone-aware instant to `Europe/Moscow`.
- Raises `ValueError` on a naive input.
- Must never assume a fixed offset.

**`moscow_date(moment: datetime) → date`**
- The Moscow calendar date of an instant. Used as the key for daily counters and
  snapshots, so that a day means a Moscow trading day.

**`trading_days_between(start: datetime, end: datetime, calendar: TradingCalendar) → int`**
- Number of exchange trading days elapsed, excluding weekends and holidays.
- Returns 0 when both instants fall on the same trading day.
- Raises `ValueError` on a naive input or when `end` precedes `start`.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

22. **A naive datetime crosses a module boundary** → `ValueError`. This is a
    programming defect, not a runtime condition; it fails loudly rather than
    being coerced to a guessed timezone.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- `now()` returns a timezone-aware UTC datetime (proves the awareness invariant).
- `to_moscow()` on a UTC instant during a DST-shifted month returns the correct
  Moscow wall time (proves conversion is not a fixed offset).
- `trading_days_between()` across a weekend returns the count excluding Saturday
  and Sunday (proves calendar arithmetic ignores non-trading days).
- Any function given a naive datetime raises `ValueError` (proves the contract is
  enforced, not merely documented).

## Expected output

- `zarabot/clock.py` implementing the contract exactly
- `tests/test_clock.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_clock.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/clock.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
