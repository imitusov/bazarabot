---
name: business-brief-guide
description: "Use this skill when writing, structuring, reviewing, or scoring a Business Brief — the 'what and why' document for a software project. Triggers: 'write a brief', 'create a project brief', 'document what we're building', 'review the brief', 'score the brief'. The brief defines goal, users, architecture overview, tech stack rationale, security model, error behaviour, hosting, environment variables, and acceptance criteria. It contains no code. Audience: stakeholders, developers, AI agents — anyone who must understand the product without reading source. Do NOT use for module-level implementation contracts (use technical-spec-guide instead). MANUAL ONLY in this repo: invoke with /business-brief-guide when a document must be amended. Never invoke it while implementing a module."
disable-model-invocation: true
---

> **Scope in this repository.** The document this guide describes already
> exists and is at 9.0+. Use this skill only to *amend* it — when building a
> module reveals a contract defect, or scope changes. Amending means: apply
> the fix, bump the version, and check whether the change ripples down to
> `dependency-order.md`, `environment-setup.md`, or a generated task file.
> Never rewrite a document as a side effect of implementing a module.


# Business Brief Guide

## Purpose of this document

A Business Brief defines **what to build and why**. It is the single source of
truth for product decisions. A non-technical stakeholder must read it and
understand the product. A developer must read it and understand every
architectural decision without asking follow-up questions.

The brief contains **no code, no function signatures, no SQL, no test cases**.
Those belong in the Technical Spec. If you find yourself writing
`use asyncio.Lock`, stop — that is a spec concern.

## Scope

This guide covers what belongs in a brief, the order of sections, a quality
checklist, and a scoring rubric. It does not cover implementation contracts —
see `technical-spec-guide.md` for those.

---

## Brief structure (section order)

### 1. Header
- **Companion document** — reference to the technical spec:
  "Implementation contracts are in `technical-spec.md`. When this brief and
  the spec conflict, the brief takes precedence."
- **Versioning** — "Versioned manually. New version when any architectural
  decision, security model, or scope boundary changes."

### 2. Goal
One paragraph: what the product does and why it exists. No jargon.

### 3. Problem being solved
Two to three sentences on the pain point the product addresses.

### 4. Users
- Who uses it
- Access model (open, invite-only, single-user)
- Platform and interface
- Supported languages
- Chat/group/private restrictions if applicable

### 5. Architecture overview
An ASCII flow diagram of the main request path, including edge-case branches
(error, rate limit, restart). The diagram is the anchor everyone refers back to.

### 6. Tech stack
A table: Component | Choice | Reason. Every choice needs a one-line reason.
A reader should never wonder "why this and not that."

### 7. Key concepts
Plain-language explanation of any non-obvious technology the project relies on
(protocols, encryption schemes, API formats). Assume no prior knowledge. This
section is what makes the brief readable by non-specialists.

### 8. Pre-development verification
What must be confirmed before any code is written (e.g. "verify the LLM API
accepts the expected format"). Prevents building on unverified assumptions.

### 9. Core capabilities / tools
What the product can do, listed at a functional level.

### 10. Security model
Encryption at rest, secrets management, data isolation between users, deletion
of sensitive messages, log sanitization. State consequences of key loss.

### 11. Session / conversation model
State, history, retention policy, what persists across restarts.

### 12. Behaviour under load / limits
Rate limits, what the user sees when limits are hit, retry policy.

### 13. Background tasks
What runs continuously, what each does, why it exists.

### 14. Logging and observability
Logging destinations, dashboard descriptions, alert conditions. Prose only.

### 15. Response handling
Truncation, pagination, formatting constraints.

### 16. Commands / API surface
Each entry point with a plain-language description.

### 17. Hosting and deployment
Platform, deploy flow, config fail-fast behaviour, graceful shutdown,
in-flight request handling.

### 18. Environment variables
Table: Variable | Required | Default | Description.

### 19. Acceptance criteria
Specific, testable statements of when v1 is done.

### 20. Out of scope
Table: Feature | Reason excluded. Every exclusion needs a reason or a
developer may build it anyway.

### 21. Open questions
Must be resolved before development starts. A finished brief has none.

---

## What does NOT belong in a brief

- Code snippets of any length
- Function signatures or type annotations
- SQL queries
- Test cases
- Implementation directives ("use a per-user asyncio lock")

---

## Quality checklist

- [ ] A non-developer can read it and understand the product
- [ ] Every architectural decision has a stated reason
- [ ] Every out-of-scope item has a stated reason
- [ ] No open questions remain
- [ ] No code appears anywhere
- [ ] Key concepts explained — no assumed knowledge
- [ ] Acceptance criteria are specific and testable
- [ ] Architecture diagram includes error and edge-case paths
- [ ] Security consequences (e.g. key loss) are stated
- [ ] Companion spec is referenced with conflict-resolution rule

---

## Scoring rubric

Score each category 1–10. Average is the document score. Target 9.0+.

| Category | What it measures |
|---|---|
| Clarity | Readable by intended audience without re-reading |
| Completeness | All sections present, no gaps exposed under questioning |
| Ambiguity | Edge cases defined, no room for divergent interpretation |
| Audience fit | Accessible to non-developers, useful to developers |
| Security model | Encryption, secrets, isolation, deletion, key loss covered |
| Onboarding flow | User's first-run path fully described |
| Error scenarios | What the user sees on each failure mode |
| Operational detail | Timezone, deployment, shutdown, limits precise |
| Observability | Dashboards and alerts described |
| Scope management | Out-of-scope reasoned, open questions resolved |

Issue severity classification:
- 🔴 **Critical** — blocks implementation or causes a wrong product to be built
- 🟡 **Significant** — causes confusion or incorrect assumptions
- 🟢 **Minor** — polish

---

## Iteration

Briefs improve through critic-and-fix cycles: write → critic (score + priority
fix list) → fix → repeat until 9.0+. See `documents-relationship.md` for the
full cycle and how the brief feeds the technical spec.
