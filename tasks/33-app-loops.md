# Task 33/40: Implement `zarabot/app/loops.py`

## Product context

The trading cycle. Exits run before the halt check, which is what implements halt-blocks-entries-only. 70% coverage.

## Build order position

Module **33** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/loops.py`

**`async trading_cycle(ctx: AppContext) → None`** — one iteration, in this fixed order:
1. If the session is closed, return without any broker call.
2. Refresh prices for open positions, and poll standing stop orders for
   execution. A stop filled by the exchange closes its position here.

   **Execution is confirmed, never inferred.** The cycle calls
   `broker.client.get_executed_stop_fills` once, over the window from the start
   of the current Moscow trading day to now, and closes a position **only** when
   that result contains the `stop_order_id` recorded in its own `stop_orders`
   row. Matching is on that persisted broker identifier, never on the UUID we
   generated: `list_stop_orders` builds its key from `order_request_id` when the
   broker supplies one and from `stop_order_id` otherwise, so our UUID may match
   nothing even for a stop that is perfectly alive.

   The previous rule concluded that a stop had fired from **two absences** — the
   stop missing from the active list, and the ticker missing from the portfolio —
   each an eventually-consistent read, and correlated rather than independent
   when the broker hiccups. A false positive closed a live position at an
   invented price, started a cooldown on an instrument the bot still held, and
   left the shares to be re-adopted as a fresh position at a new cost basis: one
   phantom round trip in the P&L from two reads that merely lagged (#5).

   **An absence is a discrepancy, not an exit.** A position whose stop is no
   longer live and for which no execution is confirmed stays open, and is
   alerted once so reconciliation and the owner can see it. The window is
   re-queried every cycle, which is harmless: a position already closed is not
   reconsidered.
   **A `PriceRejected` for one position omits that ticker and continues with the
   rest** — it must not abort the cycle, and must not count toward the
   consecutive-failure outage alert, which exists for a broker that cannot be
   reached. Positions with no price are skipped by the exit evaluation that
   follows, which already tolerates a missing entry. Rejections **latch**, like
   every other alert in this module: one alert when a cycle first rejects
   anything, naming the count, and none further until a cycle rejects nothing
   and re-arms it. An alert every cycle would be roughly 510 messages in an
   8.5-hour session for one permanently stale instrument, and the brief is
   explicit that a bot which cries wolf gets muted, and a muted bot is
   unmonitored. The count still matters — every price rejected at once is a
   different event from one instrument going quiet — so it is named in the alert
   that does fire.
3. Evaluate the remaining exits — take-profit, maximum age, and stop-loss only
   for `LOCAL`-protected positions — and submit them. **Before** any halt check,
   and before entries.
4. Recompute daily P&L; halt if the daily loss limit is breached.

   **Write the day's opening snapshot on the first in-session cycle of a day,
   and only when this process was already running when the session opened.** A
   bot restarted at 14:00 finds the snapshot written that morning and measures
   against it. A bot whose *first* cycle is at 14:00 writes **nothing** and lets
   `pnl` reconstruct the baseline.

   That second case is the whole point, and it is easy to get backwards. Writing
   at 14:00 would store bot equity as of 14:00 — a figure that already contains
   the morning's losses — so the baseline would hide exactly the drawdown the
   limit exists to catch. That is #9 restated, not fixed. `pnl`'s reconstruction
   is deliberately tight and alerts; a late snapshot is loose and silent, and on
   a limit that bounds real money the tight, loud option wins.

   **When the loss cannot be measured, do not trade on.** `pnl.bot_equity` marks
   open positions to market, so a `PriceRejected` or `BrokerUnavailable` on any
   one of them makes the day's loss unknowable rather than merely imprecise. The
   cycle then **skips entries for that cycle and alerts, latched**, without
   halting: exits have already run at step 3 and must not be blocked, and a
   halt would persist past a condition that is usually momentary. This is
   deliberately stricter than step 2, where one rejected quote omits its ticker
   and the cycle continues — there, a missing price costs one position's exit
   evaluation; here it costs the measurement that bounds the whole day.
4b. If shutdown has been requested, return; entries stop here and exits do not
   (v1.45). The same shape as the halt check below, for the same reason: a
   process on its way down must not open what nobody will be watching, and must
   not be stopped from closing what is already open.
5. If halted, return; entries stop here.
6. Fetch candles, evaluate strategies, and pass each signal through the gate.
7. Record every signal with its decision; execute the approved ones.

   **A ticker already opened earlier in this same pass is skipped before the
   gate, and recorded as `DUPLICATE_TICKER` (v1.40).** Strategies are looped
   outer and tickers inner, so two strategies can signal one ticker in a single
   pass. The gate's duplicate check reads `state.positions` from
   `get_portfolio()`, and the broker's portfolio lags a market order that has
   only just filled, so the second signal could pass a gate that was working
   correctly. `execution.orders.open_position` then refused it — also correctly —
   with `PositionStateError`, which nothing in this module caught (#24).

   The set of tickers opened in the pass lives here, not in `risk.gate`, which
   stays pure. It is the local record, which is immediately consistent, deciding
   a question the broker's eventually-consistent one cannot answer in time.

   **A refused entry is an ordinary outcome, not a fault.** `OrderRejected`,
   `PositionStateError` and `db.orders.DuplicateOrderError` are all caught here,
   logged, and the pass continues to the next ticker. Only `OrderRejected` was,
   so a correct refusal propagated to `run`'s supervisor, which logged
   `task_crashed`, alerted **"Background task trading crashed"**, slept, restarted
   the loop — and skipped every remaining ticker in the pass. The guard was
   doing its job; the caller was routing its success through the crash path.

   The portfolio is still re-read from the broker after each successful open.
   That read is what keeps `MAX_POSITIONS`, `PORTFOLIO_EXPOSURE` and available
   cash correct within a pass, and dropping it to save a call would trade a
   spurious alert for a breached risk limit. Opens are rare; the per-cycle cost
   #19 is about is elsewhere.

7b. **A position whose entry the recorded calendar does not reach is evaluated
   with `trading_days_open = None`, and the owner is alerted, latched (v1.44).**
   `market.session.covers` answers the question. Stop-loss and take-profit still
   evaluate normally — only the age trigger is suppressed, and only for that
   position.

   **The latch re-arms when a cycle measures every open position (v1.48).** It
   was set and never reset, so the alert fired once per process and a second
   occurrence after recovery was silent — which is #32 exactly, reintroduced in
   this module hours after #32 was closed for it. The condition is not
   permanent: it clears as soon as the recorded calendar reaches back far
   enough, which after an outage is the next refresh.

   It takes the shape of `_prices_for`'s rejection latch, deliberately: one alert
   when a cycle first cannot measure an age, **naming the count**, and none until
   a cycle measures them all. Re-arming per position would let one covered
   position clear a latch while an uncovered one is still suppressed, so the
   whole cycle is the unit.

8. The calendar handed to `lifecycle.exits` comes from `market.session.calendar()`
   (v1.40), never from a fetch of this module's own. It fetched a fourteen-day
   schedule **every cycle** — once a minute, for data that changes at most daily
   and that `market.session` already held, refreshed daily by task 3 of `run`
   (#19). On a broker error that fetch returned an *empty* calendar, so
   `clock.trading_days_between` counted zero and `MAX_AGE` exits silently stopped
   firing; reading the cache cannot reach that state.

Steps 3 and 4 running before step 5 is what implements the brief's
halt-blocks-entries-only rule, and their order is binding.

**Scheduling is "due and not yet done", recorded in the database (v1.45).**
Every periodic job — rollover, backup, weekly report, schedule refresh,
heartbeat — asks `db.job_runs.has_run(job, period_key)` and records completion
with `mark_run`. Two defects go with that change (#27):

- **Exact-hour matching is gone.** The weekly report fired only if the loop
  observed an instant inside the 12:00–12:59 MSK hour on a Sunday. A process
  down, restarting, or backing off through that hour skipped the week entirely,
  with no report, no alert and no record — against acceptance criterion 9. It is
  now due from 12:00 MSK Sunday onward and runs the moment the process is up. A
  report delivered at 14:00 after a restart is strictly better than none.
- **Schedule state survives restarts.** It lived in module globals, so every
  restart re-armed every job. Three restarts in a day produced three heartbeats
  and could repeat a rollover; a restart through the report hour lost the week.
  Restarts are routine — six in one evening during the #45 work — so this is the
  ordinary case, not an edge one.

**Not every latch moves.** `_first_cycle_at` stays process-local and must: it
means "was *this process* running when the session opened", which is exactly
what decides whether the day's opening snapshot may be written (step 4).
Persisting it would let a restarted process claim an origin it did not have. The
alert latches stay process-local too — they suppress repetition within a run,
and a restart is a reasonable moment to speak up again.

**There is no separate "overdue" alert**, because due-and-not-yet-done removes
the condition it would guard: a job is no longer skipped, only run late. The
weekly report carries its own timestamp, so lateness is visible in the artefact
rather than in a second alert with a threshold nobody chose.

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

33. **A recorded price comes from the broker, or the record stays pending.**
    Realised P&L, exit prices and commissions are written from what the broker
    reports it did — an order state, an executed stop, an operation — and never
    from a quote, a stop price, an entry price, or any other number the bot has
    to hand. Where the broker's own record is not yet available, the position
    stays open and the read is retried on the next cycle; after a bounded number
    of cycles the owner is alerted. A position closed a minute late is
    recoverable and a position closed at an invented number is not, because
    nothing downstream can tell the invented one from a real one. This rule
    generalises #4, #5, #8 and #11, which are four instances of the same
    mistake.

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
