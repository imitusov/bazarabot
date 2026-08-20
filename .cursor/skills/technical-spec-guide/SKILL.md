---
name: technical-spec-guide
description: "Use this skill when writing, structuring, reviewing, or scoring a Technical Specification — the 'what each module must do' document for a software project. Triggers: 'write a spec', 'create the technical spec', 'define module contracts', 'review the spec', 'score the spec'. The spec defines module contracts (function signatures, return types, error conditions, ordering constraints), database schemas, error handling rules, test contracts, log events, and deployment config. It contains no code — prose contracts only. Audience: developers and AI coding agents implementing the code. Do NOT use for product scope or rationale (use business-brief-guide instead). MANUAL ONLY in this repo: invoke with /technical-spec-guide when a document must be amended. Never invoke it while implementing a module."
disable-model-invocation: true
---

> **Scope in this repository.** The document this guide describes already
> exists and is at 9.0+. Use this skill only to *amend* it — when building a
> module reveals a contract defect, or scope changes. Amending means: apply
> the fix, bump the version, and check whether the change ripples down to
> `dependency-order.md`, `environment-setup.md`, or a generated task file.
> Never rewrite a document as a side effect of implementing a module.


# Technical Spec Guide

## Purpose of this document

A Technical Specification defines **what each module must do — not how to do it**.
It is written for the developer or AI agent implementing the code. It must be
precise enough that two implementers reading it independently produce compatible
code.

The spec contains **no code implementations**. It contains contracts: function
signatures, return types, what each function does, what it raises, what it must
never do, and ordering constraints. The "how" is the implementer's job.

## Scope

This guide covers module contract structure, database schema format, error
handling rules, test contracts, log events, and deployment config. It does not
cover product rationale or scope — see `business-brief-guide.md`.

---

## Spec structure (section order)

### 1. Header
- Purpose statement
- Companion document reference: "Read the brief first. When this spec and the
  brief conflict, the brief takes precedence."

### 2. One-time manual setup
Steps that cannot be automated (creating database users, generating keys),
listed as prerequisites before deployment. Referenced from the Deployment
section.

### 3. Pre-development verification
Scripts that must pass (exit code 0) before any code is written. Each must
state explicit pass/fail criteria. Partial failure is failure. Each script
prints a `PASS` or `FAIL` summary line.

### 4. Unit tests
Test infrastructure (isolation strategy, mocking boundary, coverage threshold,
`pytest-asyncio` mode), then per-module test contracts.

### 5. Module contracts
One section per module. See structure below.

### 6. Database schema
Every table, every column, type, constraint, note. Content structures for
stored JSON/JSONB objects.

### 7. Migrations
What each migration file does, driver used, ordering.

### 8. Observability / dashboards
SQL for non-trivial queries (e.g. uptime calculation), null-handling notes.

### 9. Error handling rules
Numbered list, one rule per failure mode, applies across all modules.

### 10. Dependencies
Every package with version constraint and one-line reason.

### 11. Deployment
Start command, failure behaviour, build system. References manual setup.

---

## Module contract structure

Each module section follows this pattern:

```
### module_name.py

One-sentence purpose.

**function_name(param: type, ...) → return_type**
- What it does (not how)
- What it returns
- What it raises and under what conditions
- What it must never do (e.g. never log a plain token)
- Ordering or dependency constraints (called before X, after Y)
```

Rules for contracts:
- Return types must be explicit, including `| None` where applicable
- Functions that can fail must state what they raise
- Functions touching sensitive data must state what they must never log
- Functions with ordering dependencies must state them
- datetime parameters must state timezone-awareness requirements

---

## Database schema format

Per table, a column table:

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | Primary key |
| `user_id` | BIGINT | FK → users ON DELETE CASCADE |

Plus retention policy and any invariants (e.g. "`role` column and
`content.role` must always be identical").

### Content structures
For any JSON/JSONB column, define the exact field layout per variant:

```
- user: {"role": "user", "content": "string"}
- assistant (tool call): {"role": "assistant", "content": null, "tool_calls": [...]}
```

---

## Error handling rules format

Number every rule. Cover every external failure mode. State level, user-facing
behaviour, and retry policy:

```
1. External API failure → user-facing message, never silent drop
2. DB log failure → log to stdout only, never propagate
3. Rate limit (HTTP 429) → WARNING level, return message, no retry
4. Decryption failure → hard error, prompt re-registration
```

---

## Log events format

A table of every event type:

| Event | Level | Required data fields |
|---|---|---|
| `user_message` | INFO | `text`, `language` |
| `tool_call` (error) | ERROR | `tool`, `error` |
| `rate_limit` | WARNING | `window`, `retry_after_seconds` |

---

## Test contract format

Per module, list cases as plain-language assertions stating input, expected
output or side effect, and the invariant proven:

```
`function_name`:
- Input X returns Y (proves happy path)
- Input Z raises DescriptiveError (proves error contract)
- Called with A then B produces C (proves ordering contract)
- Boundary input at exactly N (proves boundary inclusivity)
```

Cover: happy paths, every early-return path, boundary conditions, sequence
flows (multi-step state transitions), and concurrency edge cases.

### Test infrastructure to specify
- **Isolation** — transaction rollback per test (DB tests), mock boundary
  (unit tests)
- **Database setup** — Docker command for local, CI service container
- **Coverage threshold** — e.g. 80% general, 70% for hard-to-test entry points
- **pytest config** — `asyncio_mode = "auto"` to avoid collection warnings
- **No real network calls** in unit tests — mock all external services

---

## What does NOT belong in a spec

- Code implementations (contracts only)
- Architectural rationale (brief's job)
- Product scope or user descriptions (brief's job)
- Inline code blocks except config files and schema definitions

---

## Quality checklist

- [ ] Every module has a section
- [ ] Every function has a contract (signature, returns, raises)
- [ ] Every error mode has a numbered rule
- [ ] Every test case has a plain-language description
- [ ] Database schema complete (all columns, types, constraints)
- [ ] Content structures defined for all stored objects
- [ ] Log events table complete
- [ ] No code implementations — contracts only
- [ ] Pre-dev verification scripts have explicit exit-code criteria
- [ ] Test isolation strategy defined
- [ ] Coverage threshold set
- [ ] `pytest-asyncio` mode specified
- [ ] All dependencies listed with versions and reasons
- [ ] Manual setup separated from automated deployment
- [ ] datetime parameters state timezone-awareness
- [ ] Trim/mutation logic has a single documented owner

---

## Scoring rubric

Score each category 1–10. Average is the document score. Target 9.0+.

| Category | What it measures |
|---|---|
| Module completeness | All modules have sections |
| Contract precision | Signatures, returns, raises all defined |
| Database correctness | Schema complete, invariants stated |
| Error handling | Every failure mode has a rule |
| i18n support | Language handling defined (if applicable) |
| State machine | All states and transitions covered |
| Concurrency | Locking, ordering, race conditions addressed |
| Testing | All paths, boundaries, sequences covered |
| Dependencies | Versioned with reasons |
| Deployment | Start command, failure behaviour, manual setup clear |

Severity: 🔴 Critical (runtime bug / blocks implementation), 🟡 Significant
(confusion / wrong assumption), 🟢 Minor (polish).

---

## Common mistakes to avoid

- Defining "how" instead of "what"
- Trim or mutation logic spread across multiple functions (pick one owner)
- Lock acquisition without a try/finally release guarantee
- Missing error rules for external calls
- Test cases covering only happy paths
- No DB test isolation (use transaction rollback)
- Ambiguous return types on nullable functions
- Missing timezone-awareness constraint on datetime params
- `sanitize`-style functions with unstated recursion scope
- Pre-dev scripts with no exit-code definition
- `pytest-asyncio` mode unspecified
