# Task 34/40: Implement `zarabot/app/shutdown.py`

## Product context

Graceful shutdown. Never cancels or liquidates positions - restarts must have no financial consequence. 70% coverage.

## Build order position

Module **34** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/shutdown.py`

**`async shutdown(ctx, signal) → None`**
- Stops accepting new signals, waits for in-flight submissions to reach a known
  state or a bounded timeout, settles what it can, records state, calls
  `db.connection.disconnect()` and `broker.client.close()`, and exits. Closing the
  database means that call and nothing else — no repository closes a connection
  it did not open — and closing the broker channel likewise belongs to the module
  that owns it.
- Must never cancel or liquidate positions.
- Orders unresolved at the timeout are left as `SUBMITTING` for the next startup
  to resolve — this is correct, not a leak.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- With the session closed, no market data call is made (proves the session guard
  gates the loop).
- A position whose stop is **absent from the active list and whose ticker is
  absent from the portfolio**, with no confirmed execution, stays `OPEN`, submits
  nothing, starts no cooldown, and alerts once (proves an exit is never inferred
  from two eventually-consistent absences — the false positive that fabricated a
  round trip, #5).
- A position is closed when `get_executed_stop_fills` contains the
  `stop_order_id` persisted in its `stop_orders` row, and the exit price recorded
  is the one that result carries (proves execution is confirmed and priced from
  the broker).
- Matching is on the persisted broker `stop_order_id`, not on the locally
  generated key: a stop whose broker-side key differs from our UUID is still
  matched (proves the key mismatch that made a live stop look dead cannot recur).
- Re-running the cycle after a position has been closed this way does not
  reconsider it (proves the re-queried window is idempotent).
- One position's price raising `PriceRejected` leaves the other positions
  evaluated normally, submits no exit for the rejected one, and does not
  increment the outage counter (proves one bad quote cannot abort a cycle or
  masquerade as a broker failure).
- Every price rejected in a cycle produces exactly one alert naming the count
  (proves a correlated failure is reported as one event, not as N).
- A second consecutive cycle with rejections produces **no** further alert, and a
  cycle with none re-arms it (proves the latch — the difference between a
  monitoring channel the owner reads and one they mute).
- Two strategies signalling the same ticker in one pass open **one** position,
  record the second as `DUPLICATE_TICKER`, raise no crash alert, and still
  evaluate the remaining tickers (proves a correct refusal is an ordinary
  outcome — it reached the supervisor, alerted "Background task trading
  crashed", and abandoned the rest of the pass).
- `open_position` raising `PositionStateError` or `DuplicateOrderError` is
  caught and the pass continues (proves both siblings of `OrderRejected` are
  handled, not just the one that had a branch).
- A cycle issues **zero** `get_trading_schedule` calls, and the calendar used for
  `MAX_AGE` is the one `market.session` holds (proves the fourteen-day schedule
  is no longer re-fetched once a minute).
- `run` starts the Telegram command listener, and a `/halt` sent afterwards
  halts trading (proves the kill switch exists at runtime — the acceptance
  criterion that a defined-but-uncalled listener left unmeetable while every
  unit test passed).
- An exhausted schedule cache alerts rather than quietly reporting closed
  (proves silent non-trading is detected).
- A shutdown signal during an in-flight order submission waits for a known state
  before exiting (proves the graceful-shutdown contract).
- Shutdown neither cancels nor liquidates positions (proves restarts have no
  financial consequence).
- `shutdown` calls `db.connection.disconnect()`, and `db.connection.shared()`
  raises `DatabaseNotOpenError` afterwards (proves "closes the database" is that
  one call rather than a repository-level close of a connection nobody owns).

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
