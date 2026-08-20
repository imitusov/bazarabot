# Delegating the build to Cursor

How this repository is wired for Cursor, and how to run one module.

## What loads automatically

| File | Read by | When |
|---|---|---|
| `AGENTS.md` (root) | Cursor **Agent mode**, Claude Code | Every session |
| `CLAUDE.md` | Claude Code | Symlink to `AGENTS.md` — one file, cannot drift |
| `zarabot/*/AGENTS.md` | Agent mode | When editing files in that directory; combined with the root file, and where stricter, the stricter rule wins |
| `.cursor/rules/money-safety.mdc` | Chat, Composer, Agent | Every session (`alwaysApply: true`) |
| `.cursor/rules/test-first.mdc` | Chat, Composer, Agent | When a `.py` file is in context (`globs`) |
| `.cursor/rules/migrations.mdc` | Chat, Composer, Agent | When a migration is in context |

Chat and Composer do **not** read `AGENTS.md` — only `.mdc` files. That is why
the money rules are duplicated into `money-safety.mdc` with `alwaysApply: true`:
they must reach the agent in every mode, not just Agent mode. The legacy
`.cursorrules` is not used; Agent mode ignores it silently.

The directory-level files exist because a rule competing with 130 other lines in
a root file loses. `confirm_margin_trade` sitting in `zarabot/execution/AGENTS.md`
is unmissable when editing the only module that could set it.

## Running one module

1. Open a new Agent chat in the **Module Build** custom mode (below).
2. `@`-mention the task file: `@tasks/01-models.md`.
3. That file is self-contained — contract, tables, error rules, test cases.
4. When it is green and `interfaces.md` is updated, commit and start the next.

**One module per chat.** Never two. Focus is what keeps an agent inside its
assignment, and a fresh chat means the previous module's dead ends are not in
context.

## Custom Modes

Beta feature: **Cursor Settings → Chat → Custom Modes**. Create these two and
paste the instruction text.

### Mode: "Module Build"

Tools: file edit, terminal, search. Model: your strongest available.

```
You implement ONE module per session, test-first, from a task file the user
@-mentions.

Sequence, without deviation:
1. Read the task file, AGENTS.md, the AGENTS.md in the directory you will edit,
   and interfaces.md.
2. Write the test file FIRST, from the task's test cases. No implementation.
3. Run the tests. Show me they FAIL. If they pass, the tests are wrong.
4. Write the implementation.
5. Run until green and coverage is met (95% for risk.gate, risk.sizing,
   lifecycle.exits, execution.orders).
6. Append the module's public signatures to interfaces.md.
7. Stop. Do not start the next module.

Never modify a module outside the task. Never guess a signature — read
interfaces.md. Never weaken a test to make it pass. Never add a dependency
outside requirements-*.txt.

This project trades real money. If the contract is ambiguous, conflicts with
interfaces.md, or a test cannot pass without violating it — STOP and ask.
Do not guess.
```

### Mode: "Contract Critic"

Tools: read and search only — **no file edits**. Use before accepting a module.

```
You review an implementation against its contract. You do not write code.

Check, in order:
1. Does every signature match the spec exactly, including `| None`?
2. Is every documented exception raised under exactly the stated condition?
3. Is every ordering constraint respected? (write-then-send; cancel-stop-then-
   sell; position row before stop order)
4. Does a pure module do any I/O, read any clock, or touch the database?
5. Do the tests actually test the contract, or do they test the implementation?
6. Is any error rule handled differently from how §8 states it?
7. Does anything log a token, use float for money, or use a naive datetime?

Report findings by severity: CRITICAL if it could move money incorrectly,
SIGNIFICANT if it diverges from the contract, MINOR otherwise. Quote the
contract line and the code line for each. If you find nothing, say so plainly.
```

## Which wiki skills to load

The wiki guides are **authoring** guides — how to write a brief, a spec, a build
order. Those documents already exist here, so most of them have no role in the
build loop, and loading them is actively harmful: an agent handed
`business-brief-guide` may decide to "improve" the brief in the middle of
implementing a module, which is precisely the scope creep `AGENTS.md` forbids.

| Wiki skill | Where it lives here | Why |
|---|---|---|
| `interfaces-template` | `.cursor/rules/interfaces.mdc` | Auto-attaches on `interfaces.md`. The one discipline exercised at the end of **every** module |
| `technical-spec-guide` | `.cursor/skills/technical-spec-guide/` | **Manual only** (`/technical-spec-guide`). For amending the spec when a module reveals a contract defect |
| `business-brief-guide` | `.cursor/skills/business-brief-guide/` | **Manual only**. For amending the brief when scope changes |
| `project-rules-template` | already output as `AGENTS.md` | The template authored the rulebook; the rulebook is what agents read |
| `module-task-template` | already output as `tasks/*.md` | Its agent instructions are embedded verbatim in all 38 task files |
| `dependency-order-guide` | already output as `dependency-order.md` | |
| `environment-setup-template` | already output as `environment-setup.md` | |
| `documents-relationship` | — | Orientation for a human; changes no per-module behaviour |
| `workflow` | — | Same |

Both installed skills carry `disable-model-invocation: true`, so the agent cannot
pull them in on its own initiative — you invoke them deliberately when a document
genuinely needs amending.

## Test-first: how it is actually enforced

Test-first is stated in three places, deliberately, because each reaches a
different surface:

1. `AGENTS.md` — the workflow, read by Agent mode every session
2. `.cursor/rules/test-first.mdc` — globbed on `*.py`, so it reaches Chat and
   Composer too, which never read `AGENTS.md`
3. Every task file's **Agent instructions** — steps 1 and 2, in the prompt itself

None of that mechanically prevents an agent writing the implementation first and
back-filling tests that pass against it. Two things make it stick:

**The test cases come from the spec, not from the agent.** Each task file pastes
that module's test contract verbatim from `technical-spec.md` §3.2. The agent is
transcribing assertions someone else wrote against the contract, not inventing
tests that describe whatever it happened to build. This is the strongest control
and it is already in place.

**Two commits per module makes red-then-green auditable.** Commit the failing
tests first, then the implementation:

```
test(models): failing tests from spec section 3.2
Implement models: domain types and validation
```

The first commit is proof the tests existed and failed before any implementation
did. One commit per module hides the order and you have only the agent's word for
it. This deviates from the `AGENTS.md` git convention of one commit per module —
deliberately, and `AGENTS.md` records the exception.

If you ever doubt a module: `git show` the test commit and check the tests fail
against an empty implementation.

## Build order

Follow `dependency-order.md`. Modules 1–20 need nothing from the broker and can
be built now. **Module 21 (`broker.client`) is gated** on the verification suite
passing, because its contract encodes measured values.

## What not to delegate loosely

`execution.orders` (task 26). It is where money moves, needs 95% coverage, and
its correctness lives in orderings that are easy to write plausibly and wrongly.
Run Contract Critic on it, then read it yourself, line by line.

## Regenerating tasks

Task files are derived, never hand-edited:

```bash
python3 scripts/make_tasks.py
```

Re-run after any spec change so the tasks cannot drift from the contracts.
