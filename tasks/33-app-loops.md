# Task 33/42: Implement `zarabot/app/loops.py`

## Product context

The trading cycle. Exits run before the halt check, which is what implements halt-blocks-entries-only. 70% coverage.

## Build order position

Module **33** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

   **That latch is rule 9b, and this module owns it (v1.75).** §8 said "alert
   once per cycle" until v1.75 while this paragraph said "latch", and both texts
   were internally plausible, so no test could be red for both (#106). The latch
   is the surviving reading and §8 now says so: set on the first cycle that
   rejects anything, reset by a cycle that rejects nothing, one alert carrying
   the count.
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

   **This step owns rule 20, and the alert names the trades (v1.75).** When the
   limit is breached the cycle halts, persists the halt, and alerts with the
   loss **and the trades that produced it**: every position closed on the current
   Moscow date, each named with its ticker, lots, realised P&L and exit trigger,
   newest exit first. The source is `db.positions.list_closed()` filtered on
   `clock.moscow_date(position.exit_at)` — the same list `reporter.weekly` reads
   and filters to a period. **No new column, no new repository function and no
   new broker call**: the value was already in hand and was being discarded
   (failure class 13). When the day closed no positions the alert says so
   explicitly rather than omitting the section — a limit breached with no closed
   trades is an unrealised drawdown, which is a different thing for the owner to
   look at, and an empty list must not read as a formatting failure. Reply
   truncation follows `telegram.commands`' rule: cut with an explicit note of how
   many were omitted.

   Until v1.75 the alert carried the loss and the limit only, so rule 20's "and
   the trades that produced it" was satisfied vacuously — true because nothing
   ever gathered them, and deletable with no test going red (#108). This is the
   most consequential alert the system sends, at the moment the brief wants
   deliberate friction to make the owner look at what went wrong, and it was the
   one alert with no evidence in it.

   **The daily-loss halt passes `daily_loss_pct` (v1.69).** The `loss` this
   module already computed for the limit check is handed to `state.halt.halt`
   as its fourth argument, so `halt_triggered` carries the real figure. It is in
   scope one line above the call; not passing it is what left the field
   unsatisfiable.

   **`signal_generated` / `signal_rejected` are emitted here (v1.61), not in
   `risk.gate`.** The gate stays pure. Each non-`None` strategy result logs
   `signal_generated` (`ticker`, `strategy`, `reference_price`) before the gate
   runs; a rejected decision logs `signal_rejected` (`ticker`, `strategy`,
   `rejection_reason`). Approved entries that submit are not a second
   `signal_generated`.

   **After a close that starts a cooldown, emit `cooldown_started` (`ticker`,
   `active_until`) (v1.61).** `execution.orders` owns the cooldown write;
   this module owns the event because the gate cannot log.

   **A ticker already opened earlier in this same pass is skipped before the
   gate, and recorded as `DUPLICATE_TICKER` (v1.40).** Strategies are looped
   outer and tickers inner, so two strategies can signal one ticker in a single
   pass. The gate's duplicate check reads `state.positions` from
   `get_portfolio()`, and the broker's portfolio lags a market order that has
   only just filled, so the second signal could pass a gate that was working
   correctly. `execution.orders.open_position` then refused it — also correctly —
   with `PositionStateError`, which nothing in this module caught (#24).

   **This step owns rule 8's mid-session half (v1.75).** The instrument is read
   before the gate, and an `InstrumentNotFound` logs a WARNING and moves to the
   next ticker: no entry is ever sized against a lot size the bot could not
   confirm, which is the protection rule 8 exists for. Only that exception is
   handled here — every other one propagates under rule 21, because a broker that
   is unreachable is the cycle's problem, not this ticker's. Rule 8's startup
   half is an open decision recorded in §8 and is not implemented in this module
   or in `app.startup`.

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

**This module owns rule 2 (v1.75).** `broker.client` raises
`BrokerRateLimited` and carries the broker's `retry_after` on it; this module is
where the back-off happens, where the consecutive-failure counter lives, and
where the alert fires — so the rule is claimed here. Throttling is never fatal
and is never reported as an outage: the alert that fires after three consecutive
rate-limited cycles names throttling, which is what tells the owner to change
nothing and wait rather than to go looking at the network.

**Back-off takes the broker's hint when the broker gives one (v1.53).** After a
failed cycle the delay escalates as rule 1 describes; when the failure was a
`BrokerRateLimited` carrying `retry_after`, the delay is **the longer of** that
hint and the escalation, capped by the same maximum. A hint shorter than the
escalation changes nothing — the escalation already reflects how many cycles
have failed — and a hint beyond the cap is bounded by it, because this loop
submits exits and no external number may hold it asleep.

The hint is overwritten on **every** failure, not only on the ones that carry
it, and cleared by a successful cycle alongside the failure counter. A hint
remembered from a rate limit two cycles ago would otherwise still be delaying a
plain outage, which is stale state wearing the shape of a measurement (rule 36).

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

**`async run(ctx: AppContext) → None`** — **the sole owner of composition.** Every
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

**Observability of the supervisor (v1.61):**
- Each heartbeat job emits `heartbeat` (INFO) with `uptime_seconds`,
  `open_positions`, `halted`.
- A supervised task that raises emits `task_crashed` (ERROR) with `task`,
  `error`, `restart_in_seconds` before the backoff sleep.
- When `clock.now()` is not UTC-aware, emit `clock_drift` (WARNING) with
  `drift_seconds` 0 and refuse to run the cycle — a naive "now" is a contract
  violation, not weather (v1.61). Do not call `datetime.now()` here to "check"
  the clock; that would itself violate the clock-ownership rule.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

2. **Broker rate limited** → WARNING, back off **for at least as long as the
   broker's own hint**, and alert once when it has persisted for three
   consecutive cycles, naming throttling rather than an outage. Never treat as
   fatal.

   `BrokerRateLimited.retry_after` carries the hint when the broker sends one.
   It is the only party that knows when it will accept calls again, so it raises
   the floor under the escalating back-off of rule 1 and never lowers it: the
   delay is the longer of the two. It is still bounded by the same ceiling,
   because this loop is also the **exit** path — no number supplied from outside
   may keep the bot from closing a position indefinitely.

   Until v1.53 the hint was computed, asserted at the raise site, and read by
   nothing: the bot backed off on its own schedule while the broker's answer sat
   unused on the exception. This rule previously said "alert once if sustained
   beyond five minutes", which named a threshold no code implemented and no test
   could fail — the alert has always come from rule 1's consecutive-cycle
   counter. It now says what happens.

8. **Instrument metadata unavailable mid-session** → skip that ticker for the
   cycle, WARNING. `app.loops` owns this: step 6 reads the instrument before the
   gate, and an `InstrumentNotFound` logs and moves to the next ticker, so no
   entry is ever sized against a lot size the bot could not read.

   **The startup half is not settled here (v1.75).** Until v1.75 this rule also
   said "unavailable at startup → `StartupError`", an obligation that landed on
   no module: the only §4 contract that reads watchlist instruments at startup is
   `app.startup` step 8a, whose text says the opposite — a ticker whose
   instrument or price cannot be read is "excluded from the judgement and named
   separately", and "this step never raises `StartupError`, and never prevents
   startup". `make_tasks.py` cuts by heading, so it delivered the negation to
   `app.startup`'s agent and the obligation to nobody (#105, failure class 2).

   **Open decision — not settled here.** Either (a) the mid-session skip is the
   whole rule, and an unconfirmable lot size at startup is a diagnostic finding
   that startup names and continues past; or (b) startup additionally refuses to
   run when it cannot confirm the lot size of any watchlist ticker, raising
   `StartupError` from step 8a. **Until it is decided, (a) is binding**, and it
   is what the built code does. Two things argue for (a) and are recorded so the
   decision is made on them rather than on the word "must": the protection rule 8
   asks for is already supplied per cycle by the skip above, so no entry can be
   sized on unread metadata either way; and `app.startup` step 8a's own
   reasoning — refusing to start abandons every *open* position, its exits, its
   stop management and its `MAX_AGE`, converting a benign no-op into an unmanaged
   holding with real money in it. What (a) genuinely costs is that a watchlist
   whose metadata is unreadable at start is a *quieter* condition than the same
   watchlist unaffordable, which rule 37 alerts on. No agent may add a
   `StartupError` to step 8a on its own reading of this rule; §3.2 gains a test
   once the owner chooses — under (a) that startup does not raise, under (b)
   that it does.

9b. **A quote is rejected as non-positive, stale, or an implausible move** →
    WARNING, omit that instrument for the cycle, and **alert once, latched, with
    the count** (v1.75): one alert on the first cycle that rejects anything, and
    none further until a cycle rejects nothing, which re-arms it. The count is
    named in the alert that does fire, because every price rejected at once is a
    different event from one instrument going quiet. `app.loops` owns this and
    implements it in the price refresh of step 2.

    Until v1.75 this rule read "alert once per cycle with the count", which the
    `app.loops` contract had already argued against in the next paragraph and
    which no code has ever done (#106). Read literally it is roughly 510 messages
    in an 8.5-hour session for one permanently stale instrument — the
    repeating-alert failure rule 36 exists to forbid, written into the rule an
    agent is told to implement from. Both texts were internally plausible and no
    test could be red for both. The latch is now stated here as well as in §4:
    the set-site is the first rejecting cycle, the reset-site is a cycle that
    rejects nothing, and the alert is one.

    It is **not** a broker outage: it must not increment the consecutive
    failure counter of rule 1, and it must not be retried, because the next
    reading arrives on the next cycle anyway. Treating bad data as an outage is
    how a malformed field becomes an alert about the network.

20. **Daily loss limit breached** → halt, persist the halt, alert with the loss
    and the trades that produced it. Exits continue to run.

    **"The trades that produced it" are the positions closed today, and they are
    already in hand (v1.75).** `db.positions.list_closed()` exists, returns
    newest exit first, and is already read this way by `reporter.weekly`, which
    filters it to a period. No new column, no new repository function and no new
    broker call is needed — the value was being carried and discarded (failure
    class 13). `app.loops` owns the assembly and its contract states the shape:
    each closed position of the current Moscow date named with its ticker, lots,
    realised P&L and exit trigger.

    Until v1.75 nothing assembled them and the alert carried a percentage and a
    limit only, so the clause was satisfied vacuously — true because the trades
    were never gathered, and deletable with no test going red (#108, failure
    class 4). This is the single most consequential alert the system sends, at
    the moment the brief says deliberate friction should force the owner to look
    at what went wrong, and it was the one alert with no evidence in it.

21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.

33. **A recorded price comes from the broker, or the record stays pending.**
    Realised P&L, exit prices and commissions are written from what the broker
    reports it did — an order state, an executed stop, an operation — and never
    from a quote, a stop price, an entry price, or any other number the bot has
    to hand. Where the broker's own record is not yet available, the position
    stays open and the read is retried on the next cycle; **after three
    consecutive cycles in which the read is still unavailable, the owner is
    alerted once for that order, and the alert re-arms when the order settles
    (v1.75)**. A position closed a minute late is
    recoverable and a position closed at an invented number is not, because
    nothing downstream can tell the invented one from a real one. This rule
    generalises #4, #5, #8 and #11, which are four instances of the same
    mistake.

    **Where the bound applies, and where "cycle" is the wrong unit (v1.75).**
    "A bounded number of cycles" named no bound until v1.75, which is precisely
    what rule 2's own post-mortem calls the defect it was rewritten to remove —
    "a threshold no code implemented and no test could fail" — live one rule
    family over, on the path that decides whether a position stays open with real
    money in it (#112). Three, matching rules 1, 2 and 9, because they are the
    same shape and a second threshold in the same system is a second thing to
    remember. **The counted path is `execution.orders.resolve_unfinished`**,
    which runs once per cycle, already logs `order_unresolved` with an
    `age_seconds` computed from the order's own `created_at`, and already
    distinguishes "the broker cannot be reached" from "the broker says this order
    was never placed". That is the full set of three parts rule 36 requires:
    threshold, one alert, reset on settlement.

    **`broker.reconcile`'s `EXIT_UNRESOLVED` is not on this counter and is not
    a cycle.** Reconciliation runs at `app.startup` step 7 and appears in no
    `app.loops` task list, so its retry cadence is one per process start, not one
    per minute, and it alerts once per pass by construction. Suppressing it
    across *restarts* would require durable state — restarts are routine (failure
    class 15) — and whether an operator should stop being told about an
    unresolved exit because the process has bounced is a judgement about a real
    money-path alert, not a bug to be fixed in passing. **It is left alerting
    once per pass**, and the question of a durable, restart-surviving latch is
    recorded here as open and unowned. It is entangled with rule 25's open
    decision, which is what would put a reconciliation task on a cadence in the
    first place, and should be settled with it rather than before it.

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
