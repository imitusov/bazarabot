# Proposal: #44 V12 — measure `get_operations` before trusting external closes

**Kind:** live verification, then spec §2.1. Not a `zarabot/` module change
now. `broker.reconcile` books external closes from this feed (price, time,
commission). The feed has never been called on a live account. An empty
account cannot answer the questions.

**Assumptions that fail quietly (rule 33):**

1. `Operation.quantity` — lots vs instrument units.
2. `parent_operation_id` on fees — empty restores `exit_commission` of zero.
3. `OperationType` member names match the wheel.
4. `state` unset → `OPERATION_STATE_EXECUTED` drops everything (`EXIT_UNRESOLVED`).
   That one is loud.
5. `price` per instrument unit, in roubles.

**Should say:** add `scripts/verify/verify_operations.py` (V12) that dumps
type, state, quantity, price, payment, parent_operation_id, and whether fee
parents resolve to trades in the same window. Record measured answers in
§2.1 the way schedule defects were recorded. **Do not run it until there is
at least one real trade** (or a sandbox window with known fills). An empty
dump is not evidence.

**Do not:** change `broker.client.get_operations` parsing from a guessed
quantity unit. Do not place a live trade "to unblock V12".

**Modules after measurement:** `broker.client` / `broker.reconcile` only if
§2.1 contradicts the current mapping.

**Stop:** CI cannot close this. Same class as O-15.
