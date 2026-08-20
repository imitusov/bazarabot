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

Never read or write another repository's tables, even for a "quick join". Cross
-table reads belong in the caller, assembled from repository calls.

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
