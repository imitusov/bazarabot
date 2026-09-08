# Proposal: #110 S-20 — reconcile cooldown is a named side effect

**Kind:** spec. Reconcile “observes; no financial side effects” / never
place or cancel. Code `start_cooldown` on `CLOSED_EXTERNALLY`. That write
suppresses entries (rule 11) and has no stated remedy if it fails.

**Should say:** observational except: persist the reconciliation row **and**
start cooldown on external close (rule 26). Rule 11 remedy for that write
same as `close_position`. “No orders” stays.

**Do not:** remove the cooldown so diagnostics stay “pure” — then rule 26
fails on external close.

**Modules:** spec `broker.reconcile` + `interfaces.md` after amend. Then tests
for the write and a failed write.
