# Task 22/38: Implement `zarabot/market/session.py`

## Product context

Is the exchange open? Queried from the broker calendar, never hardcoded. Defaults to closed when unknown.

## Build order position

Module **22** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/market/session.py`

**`async refresh(days: int) → None`** — caches the schedule; called at startup and once per trading day.

**`is_open(now: datetime) → bool`**
- True when `now` falls within a main session, inclusive of the open instant and
  exclusive of the close instant.
- Returns `False` when the schedule is unavailable — the safe default is not to
  trade.

**`current_session(now: datetime) → SessionInfo | None`**

**`in_closing_window(now: datetime, minutes: int) → bool`** — true during the final `minutes` of the current session; used only by the maximum-age exit.

**`next_open(now: datetime) → datetime`** — used by the loop to sleep rather than poll.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

10. **Trading schedule unavailable** → treat the market as closed, WARNING, alert
    once. The safe default is not to trade.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A timestamp inside the main session reports open (happy path).
- Exactly at the session open instant reports open; exactly at the close instant
  reports closed (boundary, proves inclusivity at both ends).
- A Saturday, and a scheduled market holiday, report closed (proves the calendar
  is consulted, not the weekday).
- With the schedule unavailable, reports closed and raises no exception (proves
  the safe default is to not trade).

## Expected output

- `zarabot/market/session.py` implementing the contract exactly
- `tests/test_market_session.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_market_session.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/market/session.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
