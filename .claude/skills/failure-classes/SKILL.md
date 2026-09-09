---
name: failure-classes
description: "Reference catalogue of the failure classes that recur in document-first, agent-built projects, each with the mechanical check that catches it. Use when writing a critic checklist, when a finding looks familiar, when adding a gate to make check, or when an incident needs a 'how is this prevented' answer that names a mechanism rather than an intention. Triggers: 'failure class', 'why did the tests miss this', 'how do we prevent this next time', 'vacuous contract', 'orphaned obligation', 'seam', 'latch', 'fixture drift'. Knowing a class does not prevent writing it again; only a check does."
---

# Failure Classes

Each entry: the shape, how it hides from tests, the check. Add a class the
second time a finding has the same shape. Every class here recurred at
least twice in one project before it got a check.

## 1. Amendment scope under-counted
**Shape.** A spec change adds a function to module A and a call in module B;
only B's task is re-run. B's agent needs a function that does not exist.
**Hides.** Tests for A and B each pass; the seam is in neither.
**Check.** `check_docs.py --interfaces`: every function specified for an
already-built module is recorded in `interfaces.md` under that module's
section, else name the task to re-run. Modules not yet built are skipped, so
this is a real gate during the build loop, not only at the end. Run after
every amendment, before dispatch.

## 2. Obligation under the wrong heading
**Shape.** "…and module B alerts" written inside module A's section. The
task generator cuts by heading; B's agent never sees it.
**Hides.** An obligation adds no function, so the function-level check
passes. Delete the sentence and every test stays green.
**Check.** Spec rule: obligations live under the owing module. Then, after
every amendment, regenerate and grep — `python3 scripts/make_tasks.py` and
confirm each owing module's task file actually contains the obligation. That
is manual and it works: a v1.69 draft filed three call-site obligations under
`state.halt`, and the regenerated tasks showed `26-execution-orders`,
`29-telegram-commands` and `33-app-loops` with zero mentions of the field.
Moving each under its owning §4 heading fixed it.

`make_tasks.py` does **not** inject "obligations stated elsewhere" by scanning
other sections for a module's name. This file claimed it did, for months. It
cuts by heading and nothing else, so an obligation under the wrong heading
reaches nobody and no gate says so.

## 3. Test asserts the implementation
**Shape.** A test stubs the helper under test and asserts the stub's return,
pinning which helper was called while proving nothing about the result.
**Hides.** Coverage is full; the assertion is true by construction.
**Check.** PLANT cases: for each guarantee, the edit that must turn a named
test red. Mutation testing on critical modules.

## 4. Contract satisfied vacuously
**Shape.** "Never estimate commission" holds because commission is never
fetched. "Stops accepting signals on shutdown" has no implementation.
**Hides.** The prohibition is true; the absence is not a test failure.
**Check.** Critic question: what positive action makes this non-vacuous,
and what would fail if this line were absent? Spec rule: every never names
its positive action.

## 5. Defensive fallback turns loud into silent
**Shape.** Probing for a field that may not exist; `except Exception:
continue`; mapping every error to "provider unavailable" and retrying.
**Hides.** A rename or a programming error becomes a plausible-looking
degraded state with no traceback.
**Check.** The critic checks that each `except` is exactly as broad as its
rule, and error rules must name which exception classes are retryable.

Ruff `BLE001` would make that mechanical and is **not enabled** —
`pyproject.toml` selects `["E","F","W","I","UP","B","ASYNC","DTZ","S","RET",
"SIM"]`, no `BLE`. Turning it on would red the tree today:
`telegram/notifier.py`, `ops/backup.py` and `app/startup.py` all catch bare
`Exception`. So this class has no automated gate at all, and the reading is
the only thing standing between a rename and a plausible-looking degraded
state. Enabling `BLE001` belongs with the amendment that narrows error rules
13 and 18.

## 6. Guard covers one class and reads as covering all
**Shape.** The seam table lists broker, clock and config; the test that
"proves it complete" checks only the broker.
**Hides.** The proof is real for the class it checks.
**Check.** Whenever a check is described as proving completeness, enumerate
the classes it does not touch. Structural seam guard: parse imports,
compare against the table.

## 7. Behavioural guard on an unreachable path
**Shape.** "Trip the loss limit and assert nothing was sent" cannot trip
the limit in that test, so the guard passes with the seam open.
**Hides.** A guard that fires only when a path executes is worthless for
paths the test never reaches, which are the likeliest to hide a seam.
**Check.** Structural guards for completeness; behavioural guards for
behaviour. Never one for the other.

## 8. Known class rewritten hours after the fix
**Shape.** A latch set and never reset, written in a second module the same
evening the first was fixed.
**Hides.** Each module's tests pass; nobody lists set-sites against
reset-sites.
**Check.** `scripts/ci/check_latches.py`: for every module-level boolean a
function sets to True, require an assignment back to False inside a function
in the same module. Runs in `make docs` and in CI's "Latch resets" step. Spec
rule: every latch names its reset.

It covers booleans only. Collection latches are ignored, because whether one
self-expires is not decidable from assignments — `market/data.py`'s
`_alerted: set[str]` resets with `.discard(ticker)`, while `pnl.py`'s
`_alerted_reconstruction: set[date]` never discards and does not need to,
since a new Moscow date is a new key. Nor does it check that a reset is
*reachable*: a reset behind a condition that never holds passes. Green means
every boolean latch has a reset written down, not that every latch resets.

## 9. Fixture differs from production
**Shape.** `trading_days_between` is tested with a calendar spanning the
query; production hands it a calendar spanning the opposite direction.
**Hides.** Both sides pass; no gate looks at the seam between them.
**Check.** CALLER cases: build the input the way the caller builds it, or
assert on the caller's output. Composed-app test on the real wiring.

## 10. Built and never called
**Shape.** `build_application` defined, tested, never invoked. No kill
switch at runtime; two acceptance criteria unmeetable.
**Hides.** Unit tests call it directly.
**Check.** Spec: every function names a caller or is an entrypoint;
composition section lists every supervised loop. `check_docs.py --docs`
checks both, and that the named caller resolves to a wiring row, a specified
function or a module — a caller that does not exist is the same defect one
document later (`--no-callers` / `--no-wiring` turn the two halves off).
Composed-app test asserts each loop is running. Traceability check maps
every AC to a test.

## 11. Assumed provider behaviour
**Shape.** Wrapper written from the SDK's type stubs; the first real request
is rejected; the calendar arrives from the wrong exchange; the endpoint
serves no past dates.
**Hides.** Every mock agrees with the assumption. The verification suite
that would have caught it was written from the same assumption, or never
ran.
**Check.** Assumption ledger with measured values before the spec;
fixtures recorded from the provider; wrapper tested on fixtures; suite
runs from a clean shell in the environment checks; sandbox run in phase 4.
Rule: measure before writing the tests that will agree with the guess.

## 12. Omission reads as compliance
**Shape.** "Never reimplement the risk gate in the backtester" obeyed by not
calling the gate at all, so no limit applied to any backtest.
**Hides.** The rule is about duplication; absence is not duplication.
**Check.** Composed test enumerates the live modules it exercises against
the spec's module list. Critic question for every "never reimplement X":
where is X called?

## 13. Remedy proposed where the value was already in hand
**Shape.** Two audits proposed a new column and a new heuristic; in both
cases the correct value was already being carried and discarded.
**Hides.** Not a test failure, an expensive fix.
**Check.** Before adding a column, a call, or a heuristic: trace where the
value originates and whether it is already carried to the point of use.

## 14. Alert that repeats forever
**Shape.** "Commission still unknown" fires on every backfill run, daily,
forever.
**Hides.** Each fire is correct in isolation.
**Check.** Spec rule: every alert states its cadence and its latch. In a
channel where silence means healthy, an alert that never stops is
equivalent to no alert.

## 15. Restart treated as exceptional
**Shape.** Periodic jobs match an exact instant and remember their last run
in a module global; a restart through the hour loses the week's report.
**Hides.** Tests run in one process.
**Check.** Brief operating assumption: restarts are routine. Spec:
scheduled work is "due and not yet done" against durable state. Composed
test includes a restart.

## Using this file

- The critic reads it before each pass.
- `make check` runs every mechanical check listed here that exists.
- An incident report's "how is this prevented" names an entry here or adds
  one. An intention is not a prevention.
