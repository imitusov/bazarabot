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

`halt_state` is a single row read and written by one module; a repository would
add a hop and a second place for the severity rule to drift to. `reconciliations`
is an append-only audit row with exactly one writer, `_persist`, called once per
`reconcile()`. Neither is shared, and the rule's purpose — no two modules
mutating the same state — is already satisfied.

So amend `CLAUDE.md` to:

> Access the database only through the module that owns the table. For every
> table in §5 that is more than one module's business, that owner is a
> repository under `zarabot/db/`. A module may hold SQL for a table it is the
> sole writer of, named as its owner in §4, and only through
> `db.connection.transaction()`.

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

**Do not:** create a repository for one and leave the other. Do not remove
either allowlist entry before its §4 line exists. Do not read this as
permission for a second writer — the exception is *sole* writer, and check 5
already fails a table written by more than one module.

**Rejected alternative — reading 1**, two new repositories. It is defensible
and it is what a literal reading of the rulebook requires, but it costs a
`db.halt_state` whose whole surface is one singleton row, and `state/halt.py:53`
does `SELECT *` which a repository would have to expose as a typed row for no
caller but itself. If the owner prefers reading 1, this proposal is wrong and
the work is two new `db.*` tasks plus re-runs of `24-state-halt` and
`27-broker-reconcile`.

**Modules to re-run:** none for reading 2 — the code already complies. The
change is `CLAUDE.md`, two §4 lines, two §3.2 cases, and
`scripts/ci/check_docs.py`'s allowlist.

**Stop:** this is an owner decision between two coherent readings, not a defect
with one fix. Do not touch `state/halt.py` or `broker/reconcile.py` until it is
made.
