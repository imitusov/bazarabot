# Task 24/40: Implement `zarabot/state/halt.py`

## Product context

Sole owner of the halt flag. A halt suspends ENTRIES ONLY - exits keep running, and the halt survives restarts.

## Build order position

Module **24** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `halt_state`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, CHECK (`id = 1`). Single row |
| `halted` | INTEGER NOT NULL | 0 or 1 |
| `reason` | TEXT NULL | CHECK IN (`DAILY_LOSS_LIMIT`, `MANUAL`, `RECONCILIATION_MISMATCH`) |
| `detail` | TEXT NULL | Human-readable context for the alert |
| `halted_at` | TEXT NULL | |
| `resumed_at` | TEXT NULL | |
| `resumed_by` | TEXT NULL | `owner` or `system` |

## Module contract

### `zarabot/state/halt.py`

**Sole owner of the halt flag.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31). This module is not a `db.*` repository, but it
was one of the eight sites opening its own connection and it owes the same
obligation.

**`async is_halted() → bool`** · **`async current() → HaltState | None`**

**`async halt(reason: HaltReason, detail: str, at: datetime, daily_loss_pct: Decimal | None = None) → None`**
- Persists the halt so it survives a restart. Idempotent when already halted
  **for the same or a more severe reason**.
- **Severity order: `DAILY_LOSS_LIMIT` > `RECONCILIATION_MISMATCH` > `MANUAL`.**
  A halt for a strictly more severe reason replaces a weaker one, rewrites the
  detail and re-alerts. Returning early regardless of reason meant a daily-loss
  breach arriving during a manual halt was silently discarded, so `/resume`
  cleared a halt whose real cause nobody had been told about (#9).
- Suspends **entries only**. Never affects `lifecycle.exits` or
  `execution.orders.close_position`.
- **Emits `halt_triggered` (CRITICAL) after a halt is persisted or upgraded,
  with `reason`, `detail`, and — when the caller supplied one — `daily_loss_pct`
  (v1.61, amended v1.69).** `risk.gate` stays pure and emits nothing.
- **`daily_loss_pct` is optional, and absent rather than zero when unknown
  (v1.69).** v1.61 made it a required `Decimal`, which no caller could satisfy:
  a `MANUAL` or `RECONCILIATION_MISMATCH` halt has no daily-loss figure, and one
  of the three call sites cannot obtain one at all (below). A required argument
  nobody can supply is not a contract, and `Decimal("0")` in its place would
  read as "no loss today" on the record of a halt — the worst available lie in
  this event. When the caller passes nothing, the field is **omitted from the
  record**, not set to null or zero.
- **Who passes it, by call site (v1.69).** Stated here so a later agent does not
  "fix" the one that abstains:
  - `app.loops` **passes it.** At the daily-loss check it already holds
    `loss = await daily_loss_pct(moment)` as a `Decimal` in scope, one line
    above the `halt` call. This is the `DAILY_LOSS_LIMIT` halt and the only site
    where the figure is both meaningful and free.
  - `telegram.commands` `/halt` **passes `await pnl.daily_loss_pct(now())`.**
    The module already imports `zarabot.pnl`, so this adds no dependency. A
    manual halt is worth annotating with the day's position.
  - `execution.orders._halt_on_db_failure` **passes nothing, deliberately.** It
    halts *because a database write just failed*, and `pnl.daily_loss_pct` reads
    that same database. Calling it there would query the thing that is broken,
    on the path that exists to handle its being broken. The field is absent from
    this halt's record and that absence is correct.
- Until v1.69 the spec's signature line carried a fourth argument while
  `interfaces.md` and `state/halt.py` both had three, and no `halt_triggered`
  was emitted anywhere. v1.61 amended the signature and never re-ran the
  callers — failure class 1, amendment scope under-counted.

**`async resume(actor: str, at: datetime) → bool`**
- Clears the halt, recording who cleared it. Returns `False` when not halted.
- **Emits `halt_cleared` (INFO) with `actor` when a halt was actually cleared
  (v1.61).** A no-op resume emits nothing.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

20. **Daily loss limit breached** → halt, persist the halt, alert with the loss
    and the trades that produced it. Exits continue to run.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A `DAILY_LOSS_LIMIT` halt emits `halt_triggered` carrying the `daily_loss_pct`
  its caller passed (v1.69; proves the field is the caller's real figure, not a
  placeholder — a test asserting only that the key exists would pass against a
  hardcoded zero).
- A halt whose caller passes no `daily_loss_pct` emits `halt_triggered` with the
  key **absent** — not null, not `Decimal("0")` (v1.69; proves an unknown loss
  is reported as unknown. Zero on a halt record reads as "no loss today", which
  is false precisely when it matters).
- `resume` emits `halt_cleared` with `actor` (v1.61).
- An **escalation** emits `halt_triggered` too — a `DAILY_LOSS_LIMIT` halt
  arriving during a `MANUAL` one emits a CRITICAL record carrying the new reason,
  the new detail and the caller's figure (v1.70; the contract says "persisted
  **or upgraded**" and only the persist half was stated. Move the emit under
  `if not replacing:` and every other case in this file stays green while the
  event goes silent on exactly the #9 path).
- A **no-op `resume`** emits nothing (v1.70; the `False` return had a case, its
  silence did not).
- Halting then reading state reports halted with its reason (happy path).
- Halt state survives a simulated restart, where a restart is
  `db.connection.disconnect()` followed by `connect` to the same file — a
  connection left open in-process is not a restart (proves persistence: a crash
  must never be a way to resume trading).
- The module calls `aiosqlite.connect` nowhere (proves it runs on the shared
  connection; `state/halt.py` was one of the eight sites opening its own).
- Resuming clears the halt and records who cleared it (proves auditability).
- Resuming when not halted is accepted and changes nothing (proves idempotency).
- A `DAILY_LOSS_LIMIT` halt arriving during a `MANUAL` halt **replaces** it,
  rewrites the detail and alerts; a `MANUAL` halt arriving during a
  `DAILY_LOSS_LIMIT` halt changes nothing (proves severity ordering — the more
  serious reason was previously discarded, #9).
- A halt does not prevent `lifecycle.exits` from returning triggers, nor
  `execution.orders` from placing an exit (proves the halt-blocks-entries-only
  contract, which is the single most consequential interaction in the system).

## Expected output

- `zarabot/state/halt.py` implementing the contract exactly
- `tests/test_state_halt.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_state_halt.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/state/halt.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
