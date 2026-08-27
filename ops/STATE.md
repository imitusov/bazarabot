# State

Where the project is, for a session starting cold. Read this, then
`ops/WORK-ORDER.md` and `ops/RUNBOOK.md`.

Updated: 2026-08-26 · spec v1.27 · brief v1.8 · 25 open issues

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

## Validation earns its place

Of the eight issues opened while doing this work — #30, #31, #32, #33, #34, #35,
#36 and the `client.py:187` type error — **every one came from a check or a
critic pass, none from tests**. All the code involved passed its tests and
matched its contract.

Failure class 2 has now produced three findings on its own (#26's alert half,
#35, #36). It is the only class in the list above that no automated check
catches, and it is the one that keeps recurring.

## Not yet done

- A sandbox session — never run
- CI workflows exist but **have never executed on GitHub**
- The VPS deploy path (`scripts/deploy/`) is written and untested
- `#30`: `broker.client` 74.9% and `pnl` 69.6%, on ratchet floors
