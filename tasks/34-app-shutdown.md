# Task 34/39: Implement `zarabot/app/shutdown.py`

## Product context

Graceful shutdown. Never cancels or liquidates positions - restarts must have no financial consequence. 70% coverage.

## Build order position

Module **34** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/shutdown.py`

**`async shutdown(ctx, signal) → None`**
- Stops accepting new signals, waits for in-flight submissions to reach a known
  state or a bounded timeout, settles what it can, records state, closes the
  database, and exits.
- Must never cancel or liquidate positions.
- Orders unresolved at the timeout are left as `SUBMITTING` for the next startup
  to resolve — this is correct, not a leak.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- With the session closed, no market data call is made (proves the session guard
  gates the loop).
- A shutdown signal during an in-flight order submission waits for a known state
  before exiting (proves the graceful-shutdown contract).
- Shutdown neither cancels nor liquidates positions (proves restarts have no
  financial consequence).

## Expected output

- `zarabot/app/shutdown.py` implementing the contract exactly
- `tests/test_app_shutdown.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_app_shutdown.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/app/shutdown.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
