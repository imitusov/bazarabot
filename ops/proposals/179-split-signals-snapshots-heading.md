# Proposal: #179 split the only shared §4 heading so check 2's `any()` has nothing to own

**Kind:** spec. Cursor never edits `technical-spec.md`. This is not a
`check_docs.py` patch and not a `db.signals` / `db.snapshots` re-run.

Three of the four items on #179 closed in #180 (check 1 heading-scoped; one
§4 parser; `test_zero_marker_document_passes`). What remains is check 2's
`any()`, which #180 documented rather than tightened:

> A shared heading is satisfied if any listed module records the name […]
> Wrong-module implementation is green. A NEW name recorded only under signals
> cannot be attributed to snapshots without an ownership convention in the
> spec; **splitting the heading is the spec-side fix.**

The gate cannot resolve ownership. The ambiguity is the heading.

**Wrong:** one §4 heading for two modules:

```
### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`
```

That is the **only** shared heading in §4. Check 2 then does:

```python
if not any(wanted in iface_sections.get(mod, "") for mod in current):
```

A name specified under that heading is "implemented" if **either**
`interfaces.md` section contains it. Plant (do not add this function to the
spec): put `**\`async purge_old(before: date) → int\`**` in that shared §4
body and record `purge_old` under **`zarabot.db.signals` only**. Check 2
prints **PASS**. A snapshots contract is satisfied by a signals recording.
`purge_old` is not a real API — it is the plant that proves the `any()`. Do
not invent it as a specified function.

#176 made a miss report both modules. That interacts badly with the `any()`:
an absent name yields two FAIL lines and two task files; an agent acting on
the first line implements it in the wrong module; then `any()` greens the
tree. The two halves point in opposite directions.

Check 3 has the matching special-case: `sections()` maps the same body onto
every path in the heading, and `find_signature_drift` compares that body
against each module. After the split there is nothing shared left to special-
case.

**Should say:** two §4 sections. Assign the functions that currently live in
the shared body to the module that already owns them. Source of truth is
`interfaces.md` plus the existing bullets under that heading — do not invent
signatures.

From `interfaces.md` **`zarabot.db.signals`** (`Sole owner of \`signals\` rows`):

- **`async record(signal: Signal, decision: RiskDecision) → None`** — already
  the first bullet under the shared heading ("stores every signal…").
- **`async list_for_period(start: date, end: date) → list[tuple[Signal, RiskDecision]]`**
  — the shared heading's `list_for_period` / weekly-report bullet, with the
  signals return type (not `list[...]`).

From `interfaces.md` **`zarabot.db.snapshots`** (`Sole owner of \`daily_snapshots\``):

- **`async write_daily(snapshot) → None`** — already the last bullet
  ("upserts on the Moscow date…"). Keep the existing spec wording; do not
  invent `DailySnapshot` fields in §4 that `interfaces.md` already records.
- **`async list_for_period(start: date, end: date) → list[DailySnapshot]`** —
  same name as signals, different return type. Today one shared
  `list_for_period` line is keyed onto both modules; after the split each
  heading must carry its own line or check 3 will compare the wrong return
  type against the other module's section.

The connection / `transaction()` preamble currently above those bullets
applies to both repositories (rule 31). Duplicate it under each heading so
neither task file loses it when `make_tasks.py` cuts by heading.

**After the split:** if §4 has no remaining multi-module heading, remove the
shared-heading `any()` from check 2 and the "map one body onto every path"
special-casing from check 3 / `sections()` docs. Until the spec is split,
leave the gate as #180 left it — documenting the `any()` is correct; teaching
the gate ownership is not.

**Do not:**

- Invent `purge_old` (or any other new function) in the spec.
- Implement the heading split in this proposal PR. Spec amendment only, later.
- Patch `check_docs.py` here to "fix" the `any()` while the heading is still
  shared — that is the thing the owner said the gate cannot do.

**§3.2 / plant that would have caught it:** the `purge_old` plant above. After
the split, the same plant under the snapshots heading with a signals-only
`interfaces.md` recording must FAIL as `zarabot.db.snapshots.purge_old`. Do
not add `purge_old` to production documents to write that test; a fixture
heading pair is enough once the gate no longer unions modules.

**Modules to re-run:** none until the spec is amended. After the amendment:
possibly **none** if it is docs-only (headings and existing bullets moved, no
new behaviour). Then `scripts/ci/check_docs.py` tests that still fixture the
combined heading (`tests/test_check_docs.py`) need updating when the `any()`
comes out — those tests, not `zarabot/db/signals.py` or `zarabot/db/snapshots.py`.
Do not re-run tasks 10/11 for a heading split that matches what
`interfaces.md` already records.

`make_tasks.py` currently labels task 10 as `` `db.signals` / `db.snapshots` ``
and both `tasks/10-db-signals.md` and `tasks/11-db-snapshots.md` paste the
shared heading. Regenerating `tasks/` is part of the amendment commit, not
this proposal.

**Stop:** do not implement the split or the gate cleanup in this PR. Do not
add functions. Owner decision is already made: split the heading.
