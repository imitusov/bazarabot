# Task 34/42: Implement `zarabot/app/shutdown.py`

## Product context

Graceful shutdown. Never cancels or liquidates positions - restarts must have no financial consequence. 70% coverage.

## Build order position

Module **34** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/shutdown.py`

**`async shutdown(ctx, signal) → None`**
- **Stops entries before it drains, and now actually does (v1.45).** It calls
  `app.loops.stop_entries()` first, then settles. This contract and the
  function's own docstring both claimed it stopped accepting new signals, and
  nothing implemented that half: `shutdown` ran as a task *concurrently with*
  `run`, and the runner was cancelled only after the drain returned, so for the
  whole thirty-second window the trading loop kept cycling and could open a
  position the drain had already looked past (#21).
- The flag is process-local and deliberately does **not** persist: a restarted
  process must accept entries again. It is the one piece of loop state that
  would be wrong to keep in `db.job_runs`.
- Exits are unaffected. A cycle already past the entry check completes and its
  order is drained; the flag closes the window before the *next* entry.
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
- A `BrokerRateLimited` whose `retry_after` exceeds the escalating back-off
  delays the next cycle by the **hint** (proves the broker's own number is used
  at all: it was computed, asserted at the raise site, and read by nothing).
- A hint **shorter** than the escalation leaves the escalation unchanged (proves
  the hint raises the floor and never lowers it — a two-second hint must not
  undo a back-off five failed cycles deep).
- A hint beyond the maximum back-off is capped at it (proves no number from
  outside can hold the exit path asleep).
- A successful cycle clears the hint, so a later failure that carries none is
  delayed by the escalation alone (proves the same reset discipline the failure
  counter has; a remembered hint is stale state shaped like a measurement).
- Three consecutive rate-limited cycles alert **once**, and the alert names
  throttling rather than a market-data outage (proves the cause reaches the
  owner, instead of a bad field reading as weather — #6 and #23's complaint).
- A cycle issues **zero** `get_trading_schedule` calls, and the calendar used for
  `MAX_AGE` is the one `market.session` holds (proves the fourteen-day schedule
  is no longer re-fetched once a minute).
- A process restarted after the Sunday 12:00–12:59 MSK hour still sends that
  week's report, once (proves the skip is gone — the exact-hour condition lost
  the week with no report, no alert and no record, against acceptance criterion
  9).
- Three restarts in one Moscow day produce **one** heartbeat and one rollover,
  not three (proves schedule state survives a restart, which is the ordinary
  case rather than an edge one).
- A job already marked run for its period does not run again on the next tick
  (proves the guard is the record, not the module global that a restart cleared).
- After `stop_entries()`, a cycle evaluates no entries and still submits exits
  (proves shutdown closes the entry window without blocking the closes it exists
  to settle — the half of the contract that was written and never implemented).
- `shutdown` calls `stop_entries` **before** it drains (proves the ordering: a
  drain that runs first has already looked past the position the next cycle
  opens).
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
- An approved signal emits `signal_generated` before the gate; a rejected
  decision emits `signal_rejected` with `rejection_reason`; `risk.gate` emits
  neither (v1.61).
- A successful heartbeat job emits `heartbeat` with `uptime_seconds`,
  `open_positions`, `halted`.
- A crashing supervised task emits `task_crashed` with `task` and
  `restart_in_seconds`.

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
