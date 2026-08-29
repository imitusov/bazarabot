# State

Where the project is, for a session starting cold. Read this, then
`ops/WORK-ORDER.md` and `ops/RUNBOOK.md`.

Updated: 2026-08-29 · spec v1.50 · brief v1.11 · 15 open issues

## What this is

A personal trading bot on the Moscow Exchange via T-Invest. Real money, single
user, Telegram interface. Built document-first: `business-brief.md` (what and
why) → `technical-spec.md` (module contracts) → `tasks/*.md` (generated, one per
module) → code. **When the brief and the spec conflict, the brief wins.**

39 modules implemented. `make check` green. All gates pass.

## The fact that reframed everything, and what replaced it

**The bot could never have traded, and now it can.** `broker.client` sent a
trading-calendar request the broker rejects — `from=now, to=now+14d`, where the
horizon is measured from the start of the day — so the cache was always empty and
`is_open()` was always false. 29 hours of uptime, a heartbeat reporting health,
and not one trading-related broker call. That was #39, and it was invisible
because #23 turned the `INVALID_ARGUMENT` into `BrokerUnavailable` and #31's
empty-cache bug kept the alert quiet.

Fixed and **verified against the live account** on 2026-08-26: the bot's own code
returned 15 days, 11 trading, 10:00–18:54:59 MSK, weekends closed.

Confirming it turned up #43: the calendar was read from whichever of the 53
MOEX-prefixed exchanges came back first — an extended session running to 23:49
MSK that reports **Saturday and Sunday as trading days**. Fixing #39 alone would
have had the bot trading on a Saturday evening. The two masked each other
exactly.

**What this says about the remaining backlog.** Every finding before #39 came
from reading code. #39 and #43 came from fifteen minutes against a live account,
and neither was reachable by inspection — one needed the broker to reject a
request, the other needed to see what 147 exchanges actually contain. `make
verify` and a sandbox session are still the highest-information action available.

## make verify is green — 11 of 11, first time ever

Ran 2026-08-27. Five separate defects had been stopping it, the first of which
meant it could never start: `run_all.sh` cd's into its own directory and the
Makefile handed it a *relative* interpreter path. Measured values are now
recorded in `technical-spec.md` §2.1 instead of being assumptions.

The findings that change decisions: `PostOrder` is limited to **2/second**;
`get_last_prices` takes the whole watchlist in **one** call (#19 is cheaper than
it looks); a duplicate idempotency key is **refused**, not echoed back, so
recovery must be `get_order_state`; and the exchange stop is **good-till-cancel
and survives a restart**, which is brief acceptance criterion 16 and had never
been tested.

Two of the eleven checks contained the very defects they existed to catch — V5
sent the >14-day calendar request that is #39, and V11 read protobuf's epoch-zero
sentinel as a real expiry. A check written from the same assumption as the code
cannot falsify it.

## Still true

**The bot has never traded.** The database exists at schema version 2 with zero
positions, zero orders, zero signals, zero snapshots. The verification suite
(V1–V11, `make verify`) has never run.

So all but one of the 28 open issues was found by *reading* code, or by a
critic pass over code that had already been read. The exception is #39, found in
production, and it is the one that explains everything else, and
`broker.client` is written against an API surface read out of the vendored SDK
wheel and never exercised against a live account. Lot sizes, candle depth, rate
limits and sandbox fidelity are all still assumptions.

`scripts/deploy/export_health.py` exists to give the audit real behaviour and
currently returns nothing, because there is no behaviour.

## Two critical money defects closed — #7 and #35

Spec v1.25, brief v1.8. Three tasks, each run in its own session with only its
contract: `03-config` → `27-broker-reconcile` → `32-app-startup`.

**#7 was a policy gap, not a bug.** The brief listed manual trading as out of
scope and the code adopted every unrecognised holding, priced its stop and target
from *average cost*, and sold it on the next cycle. A share bought by hand and up
40% was adopted already past its take-profit. The brief now decides it: the
account is the bot's alone, enforced — an unrecognised holding is reported and
startup refuses, naming the tickers, unless `ALLOW_FOREIGN_HOLDINGS`
acknowledges them, in which case they are never traded.

**#35 was the double-sell condition detected and dropped.** `reconcile` now names
`keep` and `cancel`; `app.startup` applies it; every adjustment type must be
handled and an unrecognised one alerts.

**Running each task in a clean session paid for itself three times.** Each agent
read the contract cold and found something the author could not see:

- task 03 found a §3.2 block I had scrambled while inserting, and that `config`
  was registered in `make_tasks.py` with no test-contract key — so every agent
  that ever ran task 03 got the contract without its test cases
- task 27 found a v1.24 sentence telling it not to do the work v1.25 required
  thirty lines below, and that "a holding the bot recognises" was never defined
- task 27 also found **#42**: `adopt` violates its own `open_order_key` foreign
  key on a clean database and reports it as `PositionStateError: open position
  already exists`. Batch 3 made foreign keys real; a fixture pre-inserting the
  synthetic order row had hidden it since `001`.

## Batch 3 done — one connection, one transaction owner

Closed #20, #29, #38 (spec v1.23) and then #40, #41 (v1.24). Twelve task
re-runs, `main` green, `Package` publishing again.

v1.23 made the connection shared and left the writers on per-module locks. That
serialises nothing — the transaction lives on the connection — so two writers in
different modules collided on the first attempt, and any bare `commit()` made
another module's in-flight rows durable, which meant `rollback()` undid nothing
on the money path. v1.24 gave `db.connection` a single reentrant `transaction()`
and forbade every other module from touching transaction state (rule 31), with
`db.migrations` the one stated exemption.

**That defect was mine, in the amendment.** It was found by the critic pass on
the finished batch, not by any test, and confirmed with two runnable
reproductions before a line was changed. The lesson worth keeping: an amendment
that changes *who owns* a resource has to say who owns the resource's
**state**, not just the handle.

## Batch 1 of the bug sweep — done

| Task | Issue | State |
|---|---|---|
| `03-config` | #26 (log half) | done — `a2a49a1` |
| `21-broker-client` | #6 | done — `71a1a77` |
| `33-app-loops` | #6 | done — latch landed in `74c7451` |
| `32-app-startup` | #26 (alert half) | done — `5a81008` + `cedfb39` |

**#6 and #26 closed**, each against its own verification list. `make check`
green on all six gates, coverage 90.57%. The critic pass on task 32 was not
clean; its two findings are filed as #35 and #36, which is what the runbook
requires before a batch closes.

**#23 was in this batch's title and is not fixed.** Batch 1 gave it the type
distinction — `PriceRejected` is no longer conflated with `BrokerUnavailable` —
but both problems the issue actually specifies are untouched:
`client.py:122` still turns every non-SDK exception into `BrokerUnavailable`
with `from None`, and `market/data.py:37` is still `except Exception: continue`.
It belongs to batch 6 with #10 and #18, where the work order already had it —
error typing and channel reuse are one change to the same wrapper. Scope
recorded on the issue so the next agent does not inherit the assumption that it
landed here.

## Opened by the critic on task 32

- **#35 · `STOP_DUPLICATE` detected and dropped.** `broker.reconcile` reports
  more than one live stop on a position — the double-sell condition the whole
  ownership design exists to prevent — and `app.startup._apply_remedies` has no
  branch for it, so it falls through silently and the bot starts trading with
  two live stops. `severity:critical`. The cause is failure class 2 below: the
  duplicate remedy is written in `broker.reconcile`'s contract section, so
  `tasks/32-app-startup.md` never carried it. Amend, then re-run 27 and 32 in
  that order; fold it into batch 9, which already lands on `broker.reconcile`.
- **#36 · the `/report` wiring is in no contract.** `set_report_builder` appears
  in `interfaces.md` and in the code, and nowhere in `technical-spec.md`. Delete
  the line and `/report` says `report unavailable` forever with every test
  green. Same shape as `build_application`, same failure class.

## Next after that

`ops/WORK-ORDER.md` has the ordering. Two rules that matter more than the rest:

- **`execution.orders` (six issues: #4, #5, #10, #11, #22, #28) runs attended and
  gets read line by line.** It is where money moves, needs 95% coverage, and
  everything beneath it should be correct first.
- **Sandbox last.** #12 says the backtester measures a system that does not
  exist. True, and it changes no live behaviour — fixing it before the live
  modules means backtesting a system you are about to change.

My standing recommendation, not yet taken: run `make verify` and then a sandbox
session before grinding further through issues found by inspection. It is the
highest-information action available and has been outstanding all project.

## The loop

`ops/RUNBOOK.md` has both prompts. Five steps: amend the spec (Claude) → `make
check` → implement (Cursor, one module per chat, test-first, two commits) →
`make check` → validate with `/contract-critic` and close.

**Cursor never edits `technical-spec.md`.** An agent that writes its own contract
and then satisfies it has removed the only independent check. Every finding so
far came from a contract someone else wrote.

## #10 and #11 closed — the last two defects writing wrong numbers into P&L

Both were instances of the same habit, and both turned out smaller in the place
the audit pointed and larger somewhere adjacent.

**#10.** The multi-slice exit loop and the quantity-weighted aggregation it
needed were both already dead: v1.27 had stopped settling a partial as a fill,
so the loop could not reach a second iteration. What v1.27 left behind was that
nothing *decided* what to do with a partial. Entry now cancels the remainder
(`broker.client.cancel_order`, new) and writes the position from a fresh
`get_order_state`, never from the pre-cancel response. A zero-fill `SUBMITTED`
order is deliberately left alone — cancelling a merely pending market order
would turn every slow fill into a missed entry. A terminal exit that sold part
of a position reduces it to the unsold remainder and leaves it open; the sold
slice's P&L is knowingly unbooked, bounded by one position's stop loss, alerted,
and recorded as `LOTS_ADJUSTED`.

`db.positions.close` needed **no** signature change for #10. The parameter the
audit anticipated would have added a way to write a price no order achieved.

**#11.** External closes are now resolved from the operations feed — weighted
price, the sale's own timestamp, and the fee summed from the operations parented
to those sales. `get_last_price` is gone from `broker.reconcile` entirely. When
the sale cannot be found the position stays **open** and the report carries
`EXIT_UNRESOLVED`, per rule 33. Recovered entries with no matching signal are
`UNATTRIBUTED` rather than `ma_crossover`, and the signal lookup spans the
order's life rather than one Moscow date.

**Failure class 2 struck again, and I caught it in review rather than in code.**
v1.35 said `EXIT_UNRESOLVED` "does not stop the bot" and stopped there — but the
set of adjustment types startup recognises without remedying lives in
`app.startup`, and anything outside it fires the urgent *"cannot act on"* alert.
Every unresolved exit would have tripped the alarm reserved for a report the
executor genuinely cannot read. v1.37 fixes it and names the follow-on cost too.
That is the sixth finding from this class.

**What is now load-bearing and untested: `get_operations` (#44).** Every external
close's price, time and commission comes from it, and it has never been called
against a live account — written from the wheel, exactly like
`get_trading_schedule` was before #39 and #43. Four of its five assumptions fail
*quietly*: an empty `parent_operation_id` silently restores the zero-commission
defect #11 set out to fix. The account has never traded, so the feed is empty
and the check is not possible yet. It is a gate on trusting the first external
close.

## #42 and #8 closed — and both had a better fix than the one written down

**#42.** The audit offered two fixes: fabricate an `orders` row, or make
`open_order_key` nullable. Neither was needed. Since v1.25 `adopt` is reached for
exactly one condition — a holding whose ticker has an unresolved `ENTRY` order of
the bot's own — so an order row *always* exists and the synthetic
`ADOPTED-{figi}` was standing in for a key already in hand.
`_recognised_tickers` became `_recognising_orders` and carries it out. The
adopted position now points at something real, which also makes its entry
commission recoverable: `close` reads it through `db.orders.get`, and a synthetic
key resolved to nothing.

**#8.** v1.28 had already fixed the half the audit spent most of its words on —
`close_executed_stop` writes the broker's `executed_commission`, not `None`. What
remained is why the number was *unrecoverable*: the row's key is a UUID the
broker has never seen. `orders` gains `broker_order_id` (migration 005), which
`get_executed_stop_fills` already had in hand, and `broker.client` gains a
lookup by it. The audit's suggested fix — match the operations feed on FIGI and
time — was declined: the spec's own `get_operations` contract rules that out as a
per-order commission source, and an identifier the broker issued needs no
heuristic.

**Both audits were right about the defect and wrong about the remedy**, in the
same way: they proposed building something new where the correct value was
already being carried and thrown away. Worth checking for next time before
adding a column or a call.

Also from #8: the "commission still unknown" alert now fires once per order
rather than once per backfill run — daily, and again before every weekly report,
forever. An alert that repeats forever is equivalent to no alert, in a channel
whose whole premise is that silence means healthy.

## The noise cluster — and the defect it uncovered

#19, #24, #32 closed; #34 was already done and is now closed with the evidence
rather than reimplemented. Two follow-ups filed rather than smuggled in: #46
(instrument metadata caching — needs a new repository, and a real decision about
whether `trading_status` is cacheable at all) and #45, below.

**#45 is the find, and it is worse than anything in the cluster.** `MAX_AGE` can
never fire. `get_trading_schedule` is anchored to the start of the current UTC
day and runs *forward* — correct for `is_open`, and #39's own fix — but
`trading_days_between` counts only dates present in the calendar, and every day
between a position's entry and yesterday is before the window. Measured: **1
counted against 7 actual.** With the shipped `MAX_HOLDING_DAYS=3` the trigger is
unreachable.

It survived because both sides of the seam are individually correct and
individually tested. `lifecycle.exits` is pure and receives `trading_days_open`
as an argument, so its tests pass the number directly and prove the *rule*.
`clock.trading_days_between` is tested with a calendar built to span the range
being asked about. Neither test asks the question the wiring asks: *does the
calendar `app.loops` actually holds contain those days?* **A test that
constructs its own fixture cannot discover that production builds a different
one.** That is a sixth failure class, and it is the one that hid an exit trigger
that has never worked.

## I broke production tonight, and it took two minutes to find because I deployed

The #45 fix — a backward calendar window — was implemented test-first, passed
every gate, and **aborted startup on the first real request**:
`TradingSchedules INVALID_ARGUMENT 30003`. Rolled back to `b1a1e6d` inside two
minutes; the bot is healthy. Reverted on main, and #45 is open again.

**The broker serves no trading schedule for any date before today.** Measured
across seven ranges — 14 days back, 7, 1, every end date. All rejected; only a
range starting at today's midnight is served. In §2.1 now, beside #39 and #43.

Three things worth keeping from it:

1. **The tests mock the broker, so a function that cannot work passed the very
   §3.2 case written to close the seam.** Same shape as #39 and #43. The lesson
   was already written in this file and I walked into it anyway: the assumption
   the whole design rests on is the one to check against the account *first*,
   before writing the tests that will agree with it.
2. **Deploying is what found it.** Nothing in the local toolchain could have.
   That is the third time production answered a question inspection could not.
3. **The revert was the right move, not a patch.** The design was wrong, not its
   parameters — no range works — so there was nothing to tune.

What remains for #45: the forward window fetched on day N covers N..N+14, so the
union of fetches already being made covers any span a position can be open. The
data is being discarded, not missing. Keeping it needs either a small table and
repository, or a switch to calendar-day counting — the second changes what
`MAX_HOLDING_DAYS=3` means, so it is the owner's call.

## #45 fixed on the second attempt, by remembering instead of asking

The broker serves no schedule before today, so the past is now **recorded**:
every `refresh` writes its whole window to `db.trading_days`, and `calendar()`
unions that history with the live cache. Verified end to end through the real
wiring — 7 trading days where the same query returned 1.

The property that makes it work is that the window is **fourteen days wide, not
one**: a single run records the next fortnight, so a bot that ran any time in
the last fortnight has every day since on disk, including days it was off for.

**The design deliberately adds no new broker assumption.** That is the whole
difference from the attempt that broke production: the only fetch is the one
already verified against the account, and everything new is a local table that
tests can actually exercise. When a design needs a fact about the broker that
has not been measured, measure it *first* — before writing the tests that will
agree with the guess.

Where it can still fail — an outage longer than the window — it is loud:
`covers` reports it, `app.loops` passes `trading_days_open=None`, and
`lifecycle.exits` suppresses `MAX_AGE` for that position alone. A short count
reads as a young position; `None` cannot be mistaken for a measurement.

Also worth keeping: `market.session`'s tests now run against a **real**
`db.trading_days` on a temp file database, not a stub. Stubbing that seam is how
the original defect survived, and failure class 6 says so.

## Restart safety — #27 and #21

Both were about the same window and both were justified by tonight rather than
by theory: **six container restarts in one evening.** Restarting is the ordinary
operating mode of this system, not an edge case, and two things were wrong in it.

**#27.** Every periodic job matched an exact instant and remembered its last run
in a module global. A restart re-armed all of them; a restart through the Sunday
12:00-12:59 MSK hour lost that week's report with no report, no alert and no
record. Jobs now schedule on "due and not yet done" against `db.job_runs`.

Two latches deliberately stayed process-local, and the reasoning is the useful
part: `_first_cycle_at` means "was *this* process running when the session
opened", which is what licenses writing the day's opening snapshot — the
baseline the daily loss limit measures against. Persisting it would let a
restarted process claim an origin it did not have, which is #9 through the back
door. Not all state that looks like schedule state is.

**#21.** `shutdown`'s contract and docstring both said it stops accepting new
signals; nothing implemented that half. It ran concurrently with `run`, and the
runner was cancelled only after the drain returned. **Failure class 4 again — a
contract satisfied vacuously** — and the second instance this month. Worth
asking of any contract line that reads like a guarantee: what would fail if this
were simply absent? If the answer is "nothing", it is absent.

## #12 closed — the backtest is now the live path, not a resemblance of it

`sandbox/exchange.py` is a simulated broker; `sandbox/backtest.py` runs
`app.loops.trading_cycle` itself against it, once per bar. Nothing is
reimplemented because nothing needs to be.

**The defect worth remembering is how it hid.** The old module imported
`strategies`, `risk.sizing` and `lifecycle.exits` but not `risk.gate`. It obeyed
"never reimplement" by *omitting* the gate entirely — so cooldowns,
`max_open_positions`, duplicate-ticker rejection, halt and session state played
no part in any backtest. **An omission reads as compliance.** That is failure
class 4's cousin: a rule satisfied by absence.

**The seam guard is the transferable artefact.** `_seams()` is an explicit table
of every place a live module reached the broker, the clock or config, and
`test_no_real_broker_call_escapes` patches `broker.client._connect` to raise and
runs a whole backtest. A missed seam is a test failure instead of a network
call. #39, #43 and the reverted #45 attempt all died for want of exactly that.

**A design correction came from building the consumer, not from review.** The
exchange first held orders `SUBMITTED` until the cursor advanced, which would
have sent every entry through `open_position`'s crash-recovery path and tripped
the outage counter every third time. Writing the caller is what exposed it.
Worth doing deliberately: build the consumer early enough that it can still
change the producer.

**It runs.** 180 bars x 4 tickers: 13 trades, 10 stop-losses to 2 take-profits,
8.23% marked-to-market drawdown. On synthetic bars — the machinery is proved,
the strategy is not. The real answer needs `sandbox.data.load` against the
broker's ~456 daily candles, and that is the next thing.

## An audit of one night's own work — six findings, four of them mine

Ran a self-audit over the code written in this session, reproducing every
hypothesis before filing rather than reasoning about it. Six issues: #47-#52.

**The uncomfortable one is #48.** `_age_unmeasurable_alerted` is set and never
reset — a latch that fires once per process and never again. That is #32
*exactly*, written **hours after closing #32** for the same defect, in the same
codebase, with the fix fresh. Every other latch in that module resets. Knowing
a failure class does not stop you writing it; only a check does. There is no
automated one for this, and the table in #48 shows how easily it would have
been spotted by simply listing set-sites against reset-sites.

**#49 is the seam guard failing at its own job.** The table covers broker,
clock and config; the guard test checks only the broker. `state.halt` reaches
the real notifier, so a backtest on a laptop with a populated `.env` sends real
alerts to the owner's phone. An omission that reads as compliance — which is
the exact defect #12 was closed for, reproduced in #12's own fix.

**Two habits that paid.** Reproducing before filing killed one candidate
outright: module-level `asyncio.Lock` looked like the #20 defect, and the
uncontended fast path meant it did not reproduce — it only fails under
contention (#50), which is a materially different and much narrower claim than
the one I was about to write. And auditing *my own fresh work* was far more
productive than auditing old code: four of six findings are from tonight.

## The first real strategy finding: MAX_HOLDING_DAYS=3 dominates everything

#53 is closed — four cycles a bar, at the open, low, high and close, the last
inside the closing window. It made the daily loss limit reachable, and turned up
that **`MAX_AGE` was unreachable too**, for the same reason: `lifecycle.exits`
needs `in_closing_window` and the single cycle sat at the session start.

The numbers moved more than the mechanism suggests. Same 180 bars, same
strategy, shipped `MAX_HOLDING_DAYS=3`:

| | before | after |
|---|---|---|
| exits | 10 STOP_LOSS, 2 TAKE_PROFIT | **13 MAX_AGE** |
| realised | -7746 | +3580 |

**Every position now exits on age.** Checked it was the fix and not a new
defect by loosening the cap to 8, where all three triggers reappear — so the
stop machinery is intact and positions were simply ageing out before a 5% stop
was touched.

That is a **strategy-parameter finding, not a code one**: at the shipped
configuration the stop and target levels are nearly decorative, and every
backtest before today said the opposite because it could not fire the trigger
that actually governs. It is the first thing this project has learned about its
own strategy rather than about its own plumbing.

**Writing the consumer exposed the producer's flaw for the second time today.**
`advance` first took a price; the marks are per instrument and a backtest runs
the whole watchlist, so one ticker's low would have been every ticker's price.
It takes a phase now. Build the caller early enough that it can still change
the callee.

## Failure classes that keep recurring

Recorded because they will happen again, and three of them were mine.

1. **An amendment spans more modules than I name.** Batch 1 of the wiring fixes:
   I amended `market.session` and `app.loops`, then named only task 33. The agent
   hit a function that did not exist and stopped. `check_docs.py` now catches
   this and names the task to re-run.
2. **An obligation written in the wrong module's contract section.** I put "and
   `app.startup` alerts" inside `config`'s section; `make_tasks.py` extracts by
   module heading, so task 32 never carried it and half of #26 was silently
   unimplementable. No check catches this — obligations add no function.
3. **A test that asserts the implementation, not the contract.** One
   monkeypatched `money_to_decimal` and asserted the stub's return, so it pinned
   which helper was called while stubbing out the conversion it claimed to test.
4. **A contract satisfied vacuously.** "Never estimate commission" was true
   because commission was never fetched at all.
5. **Defensive fallbacks that convert a loud failure into a silent one.** #33:
   probing for a field that does not exist turns a rename into total rejection
   with a plausible-sounding message instead of an `AttributeError`.
6. **A guard that covers one class of escape reads as covering all of them.**
   #49: the backtest's seam table patches broker, clock and config, and the test
   that "proves it complete" checks only the broker. Whenever a check is
   described as proving completeness, ask *of what* — and enumerate the classes
   it does not touch.
7. **A guard that can only fire when a code path executes is worthless for the
   paths a simulation never reaches** — and those are the paths most likely to
   hide an unpatched seam. #49: the behavioural alert guard passed with the
   defect live, because the daily loss limit cannot trip in a daily-bar
   backtest (#53). The structural guard — parse the source, compare against the
   table — named all seven missing modules immediately.
8. **Knowing a failure class does not prevent writing it; only a check does.**
   #48 is #32 in a second module, written hours after closing #32. The check is
   mechanical: list every `_*_alerted = True` in a module against every
   `_*_alerted = False` and look for the asymmetry. Do it whenever a latch is
   added.
9. **A test that builds its own fixture cannot see that production builds a
   different one.** #45: `trading_days_between` is tested against a calendar
   spanning the query, and the calendar production hands it spans the opposite
   direction. Both sides pass, the seam is broken, and no gate looks at seams.
   The check is to construct the fixture *the way the caller constructs it*, or
   to assert on the caller's output rather than the callee's.

## Validation earns its place

Of the eight issues opened while doing this work — #30, #31, #32, #33, #34, #35,
#36 and the `client.py:187` type error — **every one came from a check or a
critic pass, none from tests**. All the code involved passed its tests and
matched its contract.

Failure class 2 has now produced six findings on its own (#26's alert half,
#35, #36, and three more since — most recently `EXIT_UNRESOLVED` in v1.35, which
named a behaviour in `broker.reconcile`'s section that only `app.startup` could
deliver). It is the only class in the list above that no automated check
catches, and it is the one that keeps recurring. The habit that catches it is
re-reading each amendment asking *which module's code changes because of this
sentence*, not *which module is this sentence about*.

## Not yet done

- A sandbox session on **real** candles — the tool exists now (#12), the run
  does not
- CI workflows exist but **have never executed on GitHub**
- The VPS deploy path (`scripts/deploy/`) is written and untested
- `#30`: `broker.client` 74.9% and `pnl` 69.6%, on ratchet floors
- `#44`: no verification check exercises `get_operations`, which now carries
  every external close's price, time and commission
