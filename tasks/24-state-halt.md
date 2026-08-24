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
violation. This module is not a `db.*` repository, but it
was one of the eight sites opening its own connection and it owes the same
obligation.

**`async is_halted() → bool`** · **`async current() → HaltState | None`**

**`async halt(reason: HaltReason, detail: str, at: datetime) → None`**
- Persists the halt so it survives a restart. Idempotent when already halted.
- Suspends **entries only**. Never affects `lifecycle.exits` or
  `execution.orders.close_position`.

**`async resume(actor: str, at: datetime) → bool`**
- Clears the halt, recording who cleared it. Returns `False` when not halted.

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

---

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Halting then reading state reports halted with its reason (happy path).
- Halt state survives a simulated restart, where a restart is
  `db.connection.disconnect()` followed by `connect` to the same file — a
  connection left open in-process is not a restart (proves persistence: a crash
  must never be a way to resume trading).
- The module calls `aiosqlite.connect` nowhere (proves it runs on the shared
  connection; `state/halt.py` was one of the eight sites opening its own).
- Resuming clears the halt and records who cleared it (proves auditability).
- Resuming when not halted is accepted and changes nothing (proves idempotency).
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
