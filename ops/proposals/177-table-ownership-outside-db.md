# Proposal: #177 a module may own the one table it alone writes

**Kind:** rulebook + spec. No code change. `CLAUDE.md` states a rule the
codebase does not follow, and neither document decides which is right.

**Wrong line** (`CLAUDE.md`, Must ALWAYS):

> Access the database only through its owning repository module. **No SQL
> anywhere else.**

Two modules outside `zarabot/db/` execute SQL, and both are otherwise correct —
`db.connection.transaction()`, no `aiosqlite.connect`, no `BEGIN`/`commit`:

| Module | Statements | Table | §4 ownership |
|---|---|---|---|
| `state/halt.py` | 6 (`:53`, `:73`, `:87`, `:93`, `:120`, `:127`) | `halt_state` | `:3136` "Sole owner of **the halt flag**" — the flag, not the table |
| `broker/reconcile.py` | 1 (`:447`) | `reconciliations` | none; only `interfaces.md:956` |

**Should say — reading 2, that a module may own its own table.** This is what
the code already implements, and the argument for it is stronger than the
argument for two new pass-through repositories:

`halt_state` is a single row, and `state/halt.py:53` does `SELECT *` on it for
no consumer but its owner, so a repository's whole surface would be one
pass-through for one caller. `reconciliations` is an append-only audit row with
exactly one writer, `_persist`, called once per `reconcile()`. Neither table is
shared, so the rule's purpose — no two modules mutating the same state — already
holds.

Two arguments deliberately **not** made, because they do not survive contact
with the code. "A repository would be a second place for the severity rule to
drift to" is rhetorical: `_SEVERITY` is a pure in-module dict consulted at
`halt.py:78` before the transaction, and a rows repository would never carry it.
And "only eight lines" cuts both ways — a trivial repository is an argument for
building it as much as for the exception. Neither point is decisive, which is
why this is filed as an owner decision and not a defect with one fix.

So amend `CLAUDE.md` to:

> Access the database only through the module that owns the table. For every
> table in §5 that is more than one module's business, that owner is a
> repository under `zarabot/db/`. A module may hold SQL for a table in
> `zarabot/` that it is the sole writer of, named as its owner in §4, and its
> **writes** run through `db.connection.transaction()`.
>
> A table created or seeded by a file in `migrations/` does not thereby have a
> second writer.

The word **writes** is load-bearing and the first draft of this proposal
dropped it, which would have made the rule forbid the code it exists to
legalise. `CLAUDE.md:85` scopes the existing clause to "every **write** runs
inside `db.connection.transaction()`", and `state/halt.py` reads on the shared
connection outside any transaction at `:53`, `:73` and `:120` — as does every
repository under `db/` (`db/positions.py:49` `conn = shared()`). An all-SQL
reading reds all of them.

The migrations sentence is load-bearing too. `migrations/001_initial.sql:168`
does `INSERT INTO halt_state (id, halted) VALUES (1, 0)`, executed by
`db.migrations`, so `state.halt` is not literally the sole writer. The
precedent is already in §4 at `:1589-1591`, for `003_position_events.sql`:
"The migration file belongs to this module even though the table belongs to
that one." A migration is a schema author, not a co-owner — but the amendment
has to say so, because the whole exception is bounded on sole writership.

and add the missing §4 lines:

- `state.halt`: change "Sole owner of the halt flag" to name the table —
  "**Sole owner of the `halt_state` table**", so check 5 can see it.
- `broker.reconcile`: add "**Sole owner of the `reconciliations` table.** One
  append per `reconcile()`, written in `_persist` inside
  `db.connection.transaction()`. No other module writes it."

**§3.2 that would have caught it:** a structural case per module — the module
executes SQL against no table but its own. That is what makes the exception
bounded rather than a licence.

**And delete both entries from `KNOWN_UNOWNED_TABLES` in the same change.**
`check_docs.py`'s `stale_tables` computes

```python
{t for t in KNOWN_UNOWNED_TABLES if writers.get(t) and f"`{t}`" in section}
```

so a table that acquires a §4 owner while still waived is itself a hard failure.
The amendment and the allowlist edit are one commit or the tree goes red.

**How much of this is checkable.** Check 5 builds `writers` from
`sql_literals(src)` over `pathlib.Path("zarabot").rglob("*.py")` and fails on
`len(who) > 1`, so "sole writer" is *enforced* for Python under `zarabot/` and
merely *asserted* for anything else — a `.sql` file, a script, a test. That is
exactly how the `halt_state` seed above escapes the gate today. Extending the
scan to `migrations/*.sql` and `scripts/` with an explicit migrations exemption
would close it, and is worth its own issue rather than this amendment.

**Do not:** create a repository for one and leave the other. Do not remove
either allowlist entry before its §4 line exists. Do not read this as
permission for a second writer — the exception is *sole* writer, enforced by
check 5 within `zarabot/` and asserted beyond it.

**Rejected alternative — reading 1**, two new repositories. It is defensible
and it is what a literal reading of the rulebook requires, but it costs a
`db.halt_state` whose whole surface is one singleton row, and `state/halt.py:53`
does `SELECT *` which a repository would have to expose as a typed row for no
caller but itself. If the owner prefers reading 1, this proposal is wrong and
the work is two new `db.*` tasks plus re-runs of `24-state-halt` and
`27-broker-reconcile`.

**Modules to re-run:** none for reading 2 — the code already complies, once the
rule is scoped to writes.

The change is `CLAUDE.md`, two §4 ownership lines, two §3.2 cases,
`scripts/ci/check_docs.py`'s allowlist, **and `scripts/make_tasks.py:45`**,
which carries the sentence verbatim — `"Sole owner of the halt flag. A halt
suspends ENTRIES ONLY..."` — and generates `tasks/24-state-halt.md` from it.
Amend §4 alone and the task an agent actually reads still says "the halt flag".
The first draft of this proposal missed that file, while arguing against a
repository on the grounds that it would create "a second place to drift to".
The second place already exists, and it is the generator.

**Also verify before merging the amendment:** `interfaces.md:825` already says
"Sole owner of `halt_state`" and `make_tasks.py:24` already says "Repositories,
`state.halt` and `broker.reconcile` all run their SQL on it". Reading 2 is the
project's de facto position in three places, not one — which strengthens the
case and means three documents, not two, currently disagree with `CLAUDE.md`.

**Stop:** this is an owner decision between two coherent readings, not a defect
with one fix. Do not touch `state/halt.py` or `broker/reconcile.py` until it is
made.
