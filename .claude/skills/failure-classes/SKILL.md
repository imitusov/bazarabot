---
name: failure-classes
description: "Recurring specification and review failure modes. Use when amending a contract, writing a task, reviewing a spec change, or checking whether an obligation landed in the module that must implement it."
---

# Failure classes

Recorded because they recur. Knowing a class is not a prevention; only a
check that actually runs is. Class 2 in particular has no mechanical gate.

## 1. An amendment spans more modules than I name

Batch work that amends two contracts and names one task. The agent hits a
function that does not exist and stops.

**Check:** `scripts/ci/check_docs.py` fails when spec §4 specifies a function
`interfaces.md` does not record, and names the task to re-run.

## 2. An obligation written in the wrong module's contract section

"And `app.startup` alerts" inside `config`'s section. `make_tasks.py`
extracts by module heading, so the owing module's task never carries the
sentence and half the work is silently unimplementable. Obligations add no
function, so check_docs name/signature gates cannot see them.

**Check:** there is no `make_tasks.py` injection that guesses the owing
module by name-matching. That check was never built. Obligations live under the
owing module's §4 heading because that is the only text `make_tasks.py`
copies into that module's task. Verification is regenerate-and-grep: run
`python scripts/make_tasks.py` and search the owing module's task file for
the sentence. Finding it only in another task is the bug. An intention to
put the text in the right section is not a prevention.

## 3. A test that asserts the implementation, not the contract

A test that monkeypatches a helper and asserts the stub's return pins which
helper was called while stubbing out the conversion it claimed to test.

**Check:** compare the test file against the module's §3.2 test contract case
by case.

## 4. A contract satisfied vacuously

"Never estimate commission" was true because commission was never fetched.

**Check:** ask what would have to exist for the prohibition to be able to
fail. If the answer is "nothing", the contract is vacuously true.

## 5. Defensive fallbacks that convert a loud failure into a silent one

Probing for a field that does not exist turns a rename into total rejection
with a plausible-sounding message instead of an `AttributeError`.

**Check:** prefer the exception the runtime already raises over a guessed
attribute list.

## 6. A guard that covers one class of escape reads as covering all of them

A completeness test that checks only the broker while the table also patches
clock and config.

**Check:** whenever a check is described as proving completeness, ask *of
what* — and enumerate the classes it does not touch.

## 7. A guard that can only fire when a code path executes

Worthless for the paths a simulation never reaches. A behavioural alert guard
passed with the defect live because the daily loss limit cannot trip in a
daily-bar backtest.

**Check:** a structural guard — parse the source, compare against the table.

## 8. Knowing a failure class does not prevent writing it

The same latch defect in a second module, hours after closing the first.

**Check:** list every `_*_alerted = True` in a module against every
`_*_alerted = False` and look for the asymmetry. Do it whenever a latch is
added.

## 9. A test that builds its own fixture cannot see that production builds a different one

Both sides pass, the seam is broken, and no gate looks at seams.

**Check:** construct the fixture the way the caller constructs it, or
assert on the caller's output rather than the callee's.
