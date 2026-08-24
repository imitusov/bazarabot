# Pipeline

Issue → spec amendment → implementation → contract validation → merge.
The first three run from your machine; CI is the deterministic floor under them.
Deployment is never automatic.

## Stages

| Stage | Trigger | Runs | Gate |
|---|---|---|---|
| **CI** | every push and PR | ruff, mypy, pytest, per-module coverage, docs consistency, spec drift, image build | Deterministic. No model opinions |
| **Spec amendment** | you, in a Claude session | amend `technical-spec.md`, regenerate `tasks/` | `ops/RUNBOOK.md` step 1 |
| **Implement** | you, one Cursor chat per task | Cursor Composer implements one task, test-first | `make check` must pass |
| **Validate** | you, `/contract-critic` after a task | Contract Critic against the contract, not the diff | You read the findings |
| **Package** | green `main` | builds and publishes to GHCR, tagged by commit | Gated on the full check suite |
| **Deploy** | hourly timer on the VPS | pulls the new digest, verifies, rolls back on failure | Refuses outside 02:00-05:00 MSK or with orders in flight |

## Why three stages are local and not workflows

There are no repository secrets. `Weekly audit`, `Spec amendment` and
`Contract validation` were `anthropics/claude-code-action` workflows needing
`ANTHROPIC_API_KEY`, so every run failed at the action and **`Contract
validation` put a red X on every pull request** while producing no review. A
gate that cannot run is worse than no gate: it looks like coverage.

They were removed rather than disabled, because a workflow file that never runs
still reads as a control when someone audits this repository later. The loop
they described is unchanged and lives in `ops/RUNBOOK.md` — it is run by hand,
which is also what `Cursor never edits technical-spec.md` requires.

To restore them: `git show <commit>^:.github/workflows/audit.yml` and the two
siblings, add `ANTHROPIC_API_KEY` as a repository secret first.

## Why spec first

Every serious defect this project has hit was caught because a contract existed
to violate. An issue-to-code pipeline never updates `technical-spec.md`, so after
ten issues the spec is fiction while `tasks/` still regenerates from it and
agents are still fed those tasks. The CI spec-drift check exists to make that
decay loud, but the ordering is what prevents it.

## Why spec amendments are serial

Six reliability issues and five money issues touch the same spec sections; two
agents amending them in parallel produce conflicting versions of the same
contract. One amendment at a time, finished and committed before the next
starts.

## Why validation is not a diff review

Run it with `/contract-critic` after each task, before closing the issue.
Every defect found so far would pass a diff review. `close_position` keyed on the exit
trigger instead of the stop owner, commission never fetched at all,
`telegram.notifier` built after the modules required to alert, duplicate stops
neither adopted nor reported — in each case the code matched what the spec said,
and the spec or the wiring was wrong. Validation reads the contract.

## Setup, once

1. Protect `main`: require the CI check, disallow direct pushes.
2. Nothing else. CI and Package need no secrets — `GITHUB_TOKEN` is provided by
   Actions, and `scripts/ci/notify.py` exits 0 when the Telegram variables are
   unset, so a missing digest never reddens a run.

`Implement` still needs `CURSOR_API_KEY` and runs on a weekday schedule. Until
that secret exists it fails on every run; add the key or remove the workflow.

## The deploy is pull-based

The VPS polls for a new image; GitHub never reaches into the VPS. Two reasons:

- **No SSH key from GitHub into the machine holding the money.** A push-based
  deploy needs one, and it is a credential that grants shell on the trading host
  to anything that can read repository secrets.
- **Only the VPS knows whether it is safe to restart.** A runner cannot see that
  an order is in `SUBMITTING`. `scripts/deploy/update.sh` can, and refuses.

It deploys only inside a window when MOEX is closed, only when no order is in
flight, waits for `startup_ok` in the logs, and **rolls back to the previous
image** if that event does not arrive. A deploy that produces no `startup_ok`
has failed, whatever `docker compose ps` reports.

Rollback by hand:

    cd /opt/zarabot/app
    IMAGE_REF=<previous-digest> docker compose -f docker-compose.deploy.yml up -d --no-build

## Risk tiering

Amend the spec automatically **only** for `severity:limit` findings, and never
for anything labelled `area:money` or `area:risk`. Everything more serious is
filed and stops there, for a human to read the contract change before code is
written against it. This was enforced by the weekly-audit prompt; with that
workflow gone it is enforced by you.

This is the line I would not move. An agent amending its own contract for a
critical money defect, then implementing against the contract it just wrote, has
no independent check left in the loop.

**No broker credentials in CI.** Nothing here touches the broker. V2, V6 and V11
place real orders; they run from your machine against sandbox, never from a
workflow, and the live token never becomes a repository secret.

**No LLM task splitting.** `scripts/make_tasks.py` already does it
deterministically from the spec.
