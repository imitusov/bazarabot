# Proposal: #95 S-05 — "replace missing stop immediately" has no mid-session owner

**Kind:** spec. Rule 25: missing EXCHANGE stop → replace immediately. Detector
is `broker.reconcile`, which runs at **startup** and **must never place or
cancel**. Remedy is startup step 7. `app.loops` has no reconcile task.
`lifecycle.exits` will not fire `STOP_LOSS` while protection is EXCHANGE.

**Should say** one of:

1. Rule 25 means "before the process serves traffic" (startup only). Say
   so. Mid-session hole is accepted until restart — or
2. A named loop task may call a **narrow** replace path (place stop only,
   still never sell) on a cadence. That is a new `app.loops` + reconcile
   split: reconcile currently forbids place/cancel.

**Do not:** have `broker.reconcile` start placing orders while its contract
says it never does. Do not have `lifecycle.exits` sell an EXCHANGE position
because the stop vanished (double sell if the exchange still holds it).

**§3.2 after a choice:** either a test that loops do not replace, or a
test that a missing stop mid-session is replaced without a sell.

**Modules:** spec first. Then `app.startup` / `app.loops` / `broker.reconcile`
only as named.
