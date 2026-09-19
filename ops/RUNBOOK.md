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
> **State each obligation in the contract of the module that owes it.** A
> requirement on `app.startup` written inside `config`'s section is invisible to
> `tasks/32-app-startup.md`, because the generator extracts by module heading —
> the module that owes the work never sees the requirement. `check_docs.py` does
> not catch this: it detects functions that are specified and missing, and an
> orphaned obligation adds no function.
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

---

## One-off — repairing commission history (#246, spec v1.91)

Not part of the batch loop. Run once, on the deployed database, after the v1.91
code is live. Everything below uses code that already exists; nothing here is a
bespoke repair script, because a bespoke repair of a money figure is a second
implementation of the thing that was wrong.

**Why it is needed.** Until v1.91 `broker.client` wrote `Decimal(0)` whenever
the order state reported a zero `executed_commission`, which it does on every
fill. `db.orders.list_missing_commission` selects `commission IS NULL`, so the
daily backfill has never been shown one of those rows. Recording `None` from now
on fixes the next order and does nothing for the 22 already on disk.

1. **Back up first.** `make backup`, or whatever the deployment's backup step
   is. Step 3 rewrites recorded money data and there is no down-migration.
2. **Run V14** — `python scripts/verify/verify_commission_attribution.py`. It is
   read-only and it measures the one assumption the repair rests on: that an
   order's `stages[].trade_id` reaches the operations feed as a trade's
   `trades[].trade_id`. **If V14 does not join, stop.** The repair would then
   resolve nothing, alert once per unrepaired order, and leave the series
   exactly as gross as it is now. That is a finding to report, not a reason to
   loosen the join.
3. **Deploy v1.91.** `008_commission_zero_is_unknown.sql` runs at startup and
   rewrites every `FILLED` row's numerically-zero commission to `NULL`. The
   rows become visible to the backfill; nothing is recomputed yet.
4. **Run the backfill once over the whole account history**, in the deployed
   process's environment:

   ```python
   from datetime import UTC, datetime
   from zarabot.ops.commissions import backfill

   await backfill(datetime(2026, 3, 1, tzinfo=UTC), now())
   ```

   The window is wide only for this run. `app.loops._BACKFILL_LOOKBACK` stays at
   7 days: the measured trade-to-fee lag is one second, so nothing daily needs a
   wider window, and widening it would re-alert on old rows every day.
5. **Read the return value and the alerts.** It returns the number of orders
   updated — expect 22 — and alerts once per order it could not resolve. Every
   closed position touched by an updated order has had `realised_pnl` recomputed
   by the same call.
6. **Check the series.** Realised P&L across the closed positions should fall by
   the total commission the feed reports for the same period (14.67 as of
   2026-09-15). If it falls by some other amount, stop and report it: a partial
   repair is worse than none, because nothing marks where the discontinuity is.
