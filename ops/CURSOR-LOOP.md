# Cursor batch loop

Drives Cursor through `ops/WORK-ORDER.md` unattended, stopping only where it
must. Paste the prompt below into one Agent chat in **Module Build** mode.

## What it does and does not do

**Does:** implement every batch whose contract already covers the fix, test-first,
gated, committed.

**Does not:** edit `technical-spec.md`. Where the contract must change, it writes
a proposal to `ops/proposals/` and moves on. An agent that writes its own
contract and then satisfies it has removed the only independent check in the
loop — every finding so far, #31 and #32 included, came from a contract someone
else wrote.

So one pass produces two things: merged fixes, and a queue of amendments for
review. Run the loop, review the proposals, amend, run the loop again.

## Scope

Batches 2 through 7, then stop. **Batch 8 is excluded**: six issues on
`execution.orders`, 95% coverage, the module that moves money. Run it attended,
by itself, and read it. Batches 9 to 13 come after 8 lands.

## The prompt

> Work through `ops/WORK-ORDER.md` batches 2 to 7, in order. One batch at a
> time. Do not start batch 8.
>
> Read `ops/RUNBOOK.md`, `AGENTS.md` and `interfaces.md` before you begin.
>
> For each batch:
>
> 1. Read every issue in the batch with `gh issue view <n>`.
> 2. Read the module contract in `technical-spec.md` and the task file.
> 3. **Decide, and say which:** does the contract already cover this fix, or must
>    the contract change?
>    - **Contract already covers it** → implement. Write the tests first from the
>      task's test cases, run them, confirm they fail, then implement until
>      green. Two commits: `test(<module>): failing tests from spec §3.2`, then
>      `Implement <module>: <summary>`. Run `make check` — all six gates must
>      pass before you commit.
>    - **Contract must change** → do NOT implement and do NOT edit
>      `technical-spec.md`. Write `ops/proposals/<issue>-<module>.md` containing:
>      the contract line that is wrong or missing, what it should say, the test
>      contract cases that would have caught the bug, any error rule needed, and
>      every module that would need re-running. Then move to the next batch.
> 4. Change nothing outside the batch's modules. Append public signatures to
>    `interfaces.md` for anything you implement.
>
> **Stop the whole loop immediately if:** `make check` fails and you cannot fix
> it within the batch's modules; a test cannot pass without weakening it; or a
> fix would require changing a module outside the batch. Say why and stop — do
> not work around it.
>
> Never weaken, skip or delete a test. Never widen a risk limit. Never pass
> `confirm_margin_trade=True`.
>
> When you finish or stop, write `ops/LOOP-REPORT.md`: per batch, what you
> implemented, what you proposed instead, what you skipped and why. List the
> issue numbers ready to close.

## After the loop

Say **"validate the loop run"**. I will:

- run `make check` and `/contract-critic` over the whole range
- review every proposal in `ops/proposals/`, amend the spec for the sound ones,
  reject the rest with reasons
- push, close the issues that passed, file new issues for what the critic finds
- update the work order

Expect the critic to find things. It has on both batches so far, in code that
passed every test and satisfied its contract exactly.

## Why end-validation is weaker

Reviewing six batches at once is a worse review than six reviews of one batch.
A defect in batch 2 has five batches built on top of it by the time anyone
looks. That is the cost of running unattended, and it is worth paying only
because batches 2 to 7 are below the money path — not for batch 8.
