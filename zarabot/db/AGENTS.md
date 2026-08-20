# db/ — one owner per table

## Ownership

Each repository owns its tables exclusively and no other module writes them:

| Module | Owns |
|---|---|
| `db.positions` | `positions` |
| `db.orders` | `orders` |
| `db.stop_orders` | `stop_orders` |
| `db.cooldowns` | `cooldowns` |
| `db.signals` | `signals` |
| `db.snapshots` | `daily_snapshots` |
| `db.migrations` | `schema_version` |

Never read or write another repository's **tables** — no `SELECT`, no `JOIN`, not
even a "quick" one. The table is the private implementation; the module is the
public interface.

**Calling another repository's published function is fine and is the intended
pattern.** `db.positions.close` obtains the opening commission through
`db.orders.get(key)`, which is recorded in `interfaces.md` for exactly this
purpose. The distinction matters: a function call respects the owner's
invariants, validation and future schema changes, while a raw `SELECT` silently
depends on a layout the owner is free to change.

## Storage rules

- **Money is `TEXT`, never `REAL`.** SQLite's `REAL` is a binary float and cannot
  represent a price exactly. Convert to `Decimal` on the way out.
- **Timestamps are `TEXT`, ISO-8601, explicit UTC offset.** Naive in means
  `ValueError`, not a guess.
- **Nothing is ever deleted.** Closing a position is a state transition. History
  is permanent — it is the point of the project.
- Return `[]` for "nothing found", never `None`.

## SQL lives here and nowhere else

No SQL string appears outside this directory.
