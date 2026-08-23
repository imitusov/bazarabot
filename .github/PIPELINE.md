# Pipeline

Audit finding → spec amendment → implementation → contract validation → merge.
Deployment is never automatic.

## Stages

| Stage | Trigger | Runs | Gate |
|---|---|---|---|
| **CI** | every push and PR | ruff, mypy, pytest, per-module coverage, docs consistency, spec drift, image build | Deterministic. No model opinions |
| **Spec amendment** | `spec:ready` label on an issue | Claude amends `technical-spec.md`, regenerates `tasks/` | You review and merge the PR |
| **Implement** | manual dispatch with a task name | Cursor Composer implements one task, test-first | CI + validation must pass |
| **Validate** | every PR | Contract Critic against the contract, not the diff | You read the findings |
| **Deploy** | you, by hand | `docker compose up -d --build` on the VPS | Human, always |

## Why a label and not issue creation

Opening an issue is not a decision; applying `spec:ready` is. Auto-triggering on
creation means every duplicate, question and half-formed thought starts an agent
amending the specification of a system that trades real money. The label costs
one click and gives you a queue you control.

## Why spec first

Every serious defect this project has hit was caught because a contract existed
to violate. An issue-to-code pipeline never updates `technical-spec.md`, so after
ten issues the spec is fiction while `tasks/` still regenerates from it and
agents are still fed those tasks. The CI spec-drift check exists to make that
decay loud, but the ordering is what prevents it.

## Why spec amendments are serial

`concurrency: spec-amendment` with `cancel-in-progress: false`. Six reliability
issues and five money issues touch the same spec sections; two agents amending
them in parallel produce conflicting versions of the same contract.

## Why validation is not a diff review

Every defect found so far would pass one. `close_position` keyed on the exit
trigger instead of the stop owner, commission never fetched at all,
`telegram.notifier` built after the modules required to alert, duplicate stops
neither adopted nor reported — in each case the code matched what the spec said,
and the spec or the wiring was wrong. Validation reads the contract.

## Setup, once

1. `/install-github-app` from Claude Code in this repo — installs the app and
   stores `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
2. Add `CURSOR_API_KEY` as a repository secret.
3. Create labels: `spec:ready`, `spec-amendment`, `implementation`.
4. Protect `main`: require the CI check, disallow direct pushes.

**If an audit bot files the issues**, add the bot to `allowed_bots` in the
Claude action inputs. It rejects bot actors by default to stop agents triggering
each other in loops — the failure is silent, and looks like the workflow simply
not running.

## What is deliberately absent

**No auto-deploy.** The last step puts new code in front of real capital and
should cost one deliberate command, outside trading hours.

**No broker credentials in CI.** Nothing here touches the broker. V2, V6 and V11
place real orders; they run from your machine against sandbox, never from a
workflow, and the live token never becomes a repository secret.

**No LLM task splitting.** `scripts/make_tasks.py` already does it
deterministically from the spec.
