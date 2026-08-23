# Task 33/39: Implement `zarabot/app/loops.py`

## Product context

The trading cycle. Exits run before the halt check, which is what implements halt-blocks-entries-only. 70% coverage.

## Build order position

Module **33** of 39 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/loops.py`

**`async trading_cycle(ctx: AppContext) → None`** — one iteration, in this fixed order:
1. If the session is closed, return without any broker call.
2. Refresh prices for open positions, and poll standing stop orders for
   execution. A stop filled by the exchange closes its position here.
3. Evaluate the remaining exits — take-profit, maximum age, and stop-loss only
   for `LOCAL`-protected positions — and submit them. **Before** any halt check,
   and before entries.
4. Recompute daily P&L; halt if the daily loss limit is breached.
5. If halted, return; entries stop here.
6. Fetch candles, evaluate strategies, and pass each signal through the gate.
7. Record every signal with its decision; execute the approved ones.

Steps 3 and 4 running before step 5 is what implements the brief's
halt-blocks-entries-only rule, and their order is binding.

**`async run(ctx) → None`** — **the sole owner of composition.** Every
long-running task in the system is started here and nowhere else, and this list
is exhaustive:

1. the trading cycle
2. the daily rollover
3. **the trading-schedule refresh** (see `market.session`)
4. the commission backfill — at rollover, and immediately before the weekly
   report so the report is never composed from figures a pending commission
   would move
5. the nightly backup
6. the weekly report
7. the heartbeat
8. **the Telegram command listener**, via
   `telegram.commands.build_application()`

A module whose entry point appears in no list is dead code that passes its own
tests. `telegram.commands.build_application` was defined, covered by tests, and
never called — so `/halt` did not exist at runtime and the kill switch was
unreachable, while every test was green. Anything added to this system that must
run continuously is added to this list in the same change, or it does not run.

A failure in one task must never terminate another; each is supervised and
restarted with backoff. A failure in one task must never terminate another.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.

29. **Clock accuracy is a host requirement, verified at deployment, not a
    runtime rule.** V9 confirms the host clock is NTP-synchronised before the bot
    is deployed, and `app.startup` logs the observed system time in UTC and MSK
    so a skewed clock is visible in the first log line after every restart.

    There is deliberately **no runtime skew check**. The broker exposes no server
    wall-clock: the only timestamp available is `LastPrice.time`, which is the
    time of the last *trade* and lags arbitrarily when a market is quiet. Halting
    trading because nobody traded for ninety seconds would be a worse failure
    than the drift it guards against, and the alternative — shipping a
    hand-written NTP client into a system that moves money — is more risk than a
    correctly configured time daemon warrants.

---

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- With the session closed, no market data call is made (proves the session guard
  gates the loop).
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

## Expected output

- `zarabot/app/loops.py` implementing the contract exactly
- `tests/test_app_loops.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_app_loops.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/app/loops.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
