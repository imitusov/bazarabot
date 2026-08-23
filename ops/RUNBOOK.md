# Runbook — working through the work order

One batch at a time. A batch is the set of issues landing on the same module,
fixed in one pass, because two agents amending the same contract separately
means the second is written against a contract the first already changed.

Five steps per batch. Steps 1 and 5 are Claude; step 3 is Cursor; steps 2 and 4
are you, and take seconds.

---

## Step 1 — Amend the spec (Claude)

> Amend the spec for batch **N**: issues **#a, #b, #c**, which land on
> `<module>`.
>
> Read each issue in full, then `AGENTS.md` and the module's contract. For each
> finding decide whether it is a spec defect, a code defect, or both, and say
> which.
>
> Where the contract is wrong or underspecified: fix it, add the test contract
> cases in §3.2 that **would have caught this**, add or amend a §8 error rule if
> a failure mode is uncovered, and bump the version. Amend the brief instead if
> the product decision changed — the brief wins.
>
> Then run `python scripts/make_tasks.py` and `make check`.
>
> Finish by telling me **every module task to re-run, in dependency order** —
> not just the one the issue names. An amendment that adds a function to module
> 22 and an obligation to module 33 needs both re-run, lowest first.
>
> Do not write implementation code.

## Step 2 — Check the amendment (you, 4 seconds)

    make check

`check_docs.py` fails when the spec specifies a function `interfaces.md` does
not record, and names the task to re-run. **A red gate here is the cheap
version of the failure**; the expensive version is an agent blocked mid-task
having to guess a signature.

Expect it to be red until step 3 lands. That is correct.

## Step 3 — Implement (Cursor, Module Build mode, one fresh chat per task)

> Implement `@tasks/<NN-module>.md`. This closes **#a, #b, #c**.
>
> The spec was amended for these (v**X.Y**) and the contract is agreed — **do
> not edit `technical-spec.md`**. If it cannot be satisfied, stop and say why
> rather than working around it.
>
> Read `AGENTS.md`, the `AGENTS.md` in the directory you are editing, and
> `interfaces.md` first.
>
> Test-first: write the tests from the task's test cases, run them, **show me
> they fail**, then implement until green. Never weaken a test to make it pass.
> Two commits — `test(<module>): failing tests from spec §3.2`, then
> `Implement <module>: <summary>`.
>
> Change nothing outside this module. Finish with `make check` green and the
> module's public signatures appended to `interfaces.md`.

One task per chat. A fresh chat means the previous task's dead ends are not in
context.

## Step 4 — Gate (you)

    make check

All six must pass: ruff, mypy, pytest, per-module coverage, docs consistency,
spec drift.

## Step 5 — Validate and close (Claude)

> Validate batch **N** with `/contract-critic`. If it passes, push and close
> **#a, #b, #c** referencing the commit, then update `ops/WORK-ORDER.md`.

The critic reviews against the contract, not the diff. If it reports that the
**contract** is wrong, stop: that is a new amendment, and implementing around it
writes the defect into the code instead. File it as an issue — that is how #31
was found, immediately after the code that satisfied the contract perfectly.

---

## Marking done

A batch is done when all four are true:

- [ ] `make check` green
- [ ] Contract Critic clean, or its findings filed as new issues
- [ ] Issues closed with the commit referenced
- [ ] `ops/WORK-ORDER.md` status updated

An issue closed without the first two is closed on the agent's word.

## When to stop and think

- The critic says the contract is wrong → new amendment, not a code fix
- The agent stops on a contract conflict → it is right; fix the contract
- A test cannot pass without weakening it → the contract and the test contract
  disagree, which is a spec defect
- Coverage falls on a money module → stop, that is a financial defect

## Order

`ops/WORK-ORDER.md`. Two rules that matter more than the rest:

**`execution.orders` (batch 8) after everything beneath it.** Six issues, 95%
coverage, and the module that moves money should be rewritten against
dependencies that are already correct.

**Sandbox last.** F-12 says the backtester measures a system that does not
exist. Fixing it before the live modules means backtesting a system you are
about to change.
