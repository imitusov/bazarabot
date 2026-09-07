# Task 5b/40: Implement `zarabot/db/connection.py`

## Product context

Sole owner of the process-wide SQLite connection. Opened by app.startup, closed by app.shutdown, never at import. Repositories, state.halt and broker.reconcile all run their SQL on it and none opens its own.

## Build order position

Module **5b** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/db/connection.py`

**Sole owner of the process-wide SQLite connection.** No other module calls
`aiosqlite.connect`. No other module closes the connection.

This module exists so every repository signature in `interfaces.md` can stay
exactly as recorded: callers keep calling `db.positions.open(...)` with no
connection argument. Threading a connection through every repository would change
every caller in the system. The lifecycle is already half-specified in
`app.startup` ("open the database") and `app.shutdown` ("closes the database");
this module is the named owner of the object those two sentences refer to.

**Must never connect at import.** `AGENTS.md` forbids module-level side effects
outside `config`. Connecting at import would violate that, and would make tests
inherit whichever file the previous importer happened to open.

**`async connect(path: str) → aiosqlite.Connection`**
- Opens the SQLite file at `path`, stores it as the process connection, and
  issues on that connection:
  - `PRAGMA journal_mode = WAL`
  - `PRAGMA foreign_keys = ON`
  - `PRAGMA busy_timeout = 30000`
- `foreign_keys` and `busy_timeout` are per-connection; `journal_mode` is
  persistent. All three are set here so that a forgotten per-connection pragma
  cannot silently disable integrity checking.
- Raises `DatabaseAlreadyOpenError` when a process connection is already open. A
  caller needing a different file must `disconnect` first.
- Returns the connection. Does not apply migrations — `app.startup` calls
  `db.migrations.apply(shared())` next.
- Called only by `app.startup` and by the test fixture below.

**`shared() → aiosqlite.Connection`**
- Returns the open process connection.
- Raises `DatabaseNotOpenError` when `connect` has not been called, or when
  `disconnect` already has.
- Must never open a connection as a side effect of being called. A silent
  reconnect would hide a missing `app.startup` step and would let a test inherit
  a file it did not create.

**`transaction() → async context manager yielding aiosqlite.Connection`**
- **The sole transaction owner.** Every write in the system runs inside it:
  `async with transaction() as conn:`. It holds one process-wide lock, issues
  `BEGIN IMMEDIATE`, commits on clean exit, and rolls back on exception.
- **Reentrant.** A nested acquisition on the same task joins the outer
  transaction instead of beginning a second one or deadlocking, and only the
  outermost exit commits. `broker.reconcile` calls `db.positions.adopt` and
  `db.positions.close` while doing work of its own, so nesting is the normal
  case, not an edge one.
- **No module may `BEGIN`, `commit` or `rollback` the shared connection
  itself.** A per-module `asyncio.Lock` was sufficient when every call opened its
  own connection; on one shared connection it serialises nothing, because the
  transaction lives on the connection rather than in the module. Two writers in
  different modules previously collided with `cannot start a transaction within a
  transaction`, and — worse — a bare `commit()` in any module made another
  module's in-flight rows durable, so its `rollback()` undid nothing. Both were
  reproduced on the money path (#40).
- A read needs no transaction and must not take one.
- **On `aiosqlite.Error` during a write, emit `db_write_failed` (v1.61)** with
  `table` (the object name when the error names one, otherwise `unknown`) and
  `critical`, then re-raise. This is the single owner of that event;
  repositories that swallow a rule-12 failure still go through this path so the
  event is not optional.
- **`critical` is derived from the table, not hardcoded (v1.62).** It is `true`
  for the trading-critical tables of rule 11 — `positions`, `orders`,
  `stop_orders`, `halt_state`, `position_events` — and `false` for the
  non-critical tables of rule 12: `signals`, `daily_snapshots`, `instruments`.
  Until v1.62 the contract said `critical` true unconditionally, so a failed
  analytics write announced itself as trading-critical. The field is what an
  operator filters on to find writes that actually stopped trading; always-true
  made that filter return every hiccup, which is the same as having no field.
- **An unrecognised or `unknown` table is `critical` true.** The safe default in
  a module that records money is to overstate rather than understate, and a
  table this list does not name is a table nobody has classified.
- This module cannot ask its caller: `transaction()` takes no argument saying
  which path it serves, and adding one would touch every call site to encode
  what the table already tells us. Rules 11 and 12 are defined by table, so the
  table is the honest source.
- **`cooldowns` is deliberately absent from both lists (v1.62).** Rule 11 names
  orders, positions and halt state; rule 12 names signals, snapshots and the
  instruments cache. Neither names cooldowns, and `db.cooldowns` currently
  swallows its write failures as though rule 12 covered it. A lost cooldown row
  lets the bot re-enter a ticker it just exited, which is a trading consequence,
  not an analytics one. Until an amendment assigns it, it falls to the unknown
  default above and is reported `critical` true.

**`async disconnect() → None`**
- Closes the process connection and forgets it. Idempotent when already closed.
- Called only by `app.shutdown` and by the fixture teardown.
- After it returns, `shared()` raises `DatabaseNotOpenError`.

**This module owns `tests/conftest.py`.** The fixture that connects a temporary
file, applies migrations, yields, and disconnects on teardown is defined once
there and used by every database test in the project. `tests/conftest.py` does
not exist today — each test file builds its own temporary database — and leaving
each task in this batch to invent its own fixture is a separate chance in each
one to leak a process connection between test files.

Enabling `foreign_keys` is cheap now: the live database holds zero rows, so
turning enforcement on cannot surface an existing violation. This is the cheapest
moment in the project's life to do it.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

11. **Database write failure on a trading-critical path** (orders, positions,
    halt state) → hard error: halt trading, alert, stop opening anything. The bot
    must never trade what it cannot record.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- `connect` on a new file, then `shared()`, returns a live connection, and
  importing the module opens no file (proves there is no import-time side effect,
  which `AGENTS.md` forbids outside `config`).
- A second `connect` without `disconnect` raises `DatabaseAlreadyOpenError`
  (proves the process holds one connection rather than silently leaking the
  previous file).
- `shared()` before `connect`, and after `disconnect`, raises
  `DatabaseNotOpenError` and opens no fallback connection (proves a test cannot
  inherit a connection, and production cannot quietly reconnect to the wrong
  file).
- `disconnect` is idempotent: a second call does not raise.
- After `connect`, `PRAGMA journal_mode` is `wal`, `PRAGMA foreign_keys` is `1`
  and `PRAGMA busy_timeout` is `30000`.
- Two concurrent tasks, one writing a row and one reading another table, both
  complete (proves WAL: a writer does not block a reader, which is the contention
  the 30-second timeout was absorbing).
- Two sequential tests connect to different temporary paths, and the second
  observes none of the first's rows (proves isolation without inheriting a
  process connection).
- Two writers in **different modules**, invoked concurrently, both complete and
  neither raises `cannot start a transaction within a transaction` (proves the
  transaction is serialised process-wide rather than per module — the collision
  reproduced on the first attempt in #40).
- A nested `transaction()` on the same task joins the outer one: the inner block
  exiting does not commit, and an exception after it rolls back the outer work
  too (proves `broker.reconcile` can call `db.positions.adopt` without
  deadlocking or committing early).
- A write inside `transaction()` does **not** survive that transaction's
  rollback, even when another module performs a write of its own in between
  (proves a foreign commit can no longer make half-written rows durable — the
  defect that made `rollback()` meaningless on the money path).
- A read path takes no transaction: reads succeed while another task holds one.
- A write that fails with `aiosqlite.Error` inside `transaction()` emits
  `db_write_failed` with `table` (the SQLite object name when the error names
  one, otherwise `unknown`), then the exception still propagates (v1.61).
- A failed write to `positions` reports `critical` true; a failed write to
  `daily_snapshots` reports `critical` false; a failure naming no table reports
  `critical` true (v1.62; proves the field distinguishes rule 11 from rule 12
  rather than asserting the same thing every time, and that an unclassified
  table fails loud).
- A rule-12 repository whose write fails produces **exactly one**
  `db_write_failed` record (proves `db.connection` is the single owner and the
  repository adds none of its own).

## Expected output

- `zarabot/db/connection.py` implementing the contract exactly
- `tests/test_db_connection.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_connection.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/connection.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
