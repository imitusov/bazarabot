# Proposal: `exit_failed.attempt` has no counter (O-07 leftover)

**Kind:** spec gap. `execution.orders` implements the field as written.

§7.1 requires `exit_failed` with `attempt`. §8 rule 4 retries the exit every
cycle until the position closes. `close_position` holds no durable attempt
counter, so every emit site passes `attempt=1` and the test asserts `== 1`.

The one field that exists to distinguish a first failure from the fortieth can
never be anything else. Combined with rule 4 it is also an ERROR repeating every
cycle with an identical payload and no latch.

**Should say** one of:

1. Drop `attempt` from §7.1 for `exit_failed` (this module has no owner of a
   counter; inventing one in SQLite is a new table).
2. Or: specify where the counter lives (order row? process-local per
   `position_id`?) and that it resets on a successful close.

Prefer (1) until a storage decision exists. Do not invent a counter in
`execution.orders` first.

**Test contract:** after the amendment, either the field is absent or a named
store increments across two failed `close_position` calls.

**Modules to re-run:** `26-execution-orders`.
