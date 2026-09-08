# Proposal: #112 S-22 — rule 33 needs a numeric bound and a latch

**Kind:** spec. “Bounded number of cycles then alert” — no number, no owner.
`EXIT_UNRESOLVED` alerts every pass. Same hole rule 2’s post-mortem named.
Rule 36: threshold + one alert + reset.

**Should say:** N cycles (pick N), owner (`broker.reconcile` and/or
`execution.orders` unresolved path), one alert, reset when the broker
record appears. Related: proposal 69 `exit_failed.attempt`.

**Do not:** invent N=3 in code first.

**Modules:** spec rule 33 + named §4. Then one module.
