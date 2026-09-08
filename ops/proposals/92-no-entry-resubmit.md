# Proposal: #92 S-02 — recovery must not re-call PostOrder

**Kind:** spec. §2.1 measured duplicate `orderId` as **Refused**
(`INVALID_ARGUMENT`/`30057`). §4 `get_order_state` still calls re-submit "a
safe read". AGENTS.md and the brief: never resubmit an entry; query the key.

**Should say:** delete the fallback paragraph. Recovery is `get_order_state`
by key only. Correct V6's third PASS criterion and the "11 of 11 green"
claim if V6 still requires a resubmit that the broker refuses.

**Do not:** add a resubmit in `execution.orders` because §4 called it safe.

**Modules:** spec + `scripts/verify` V6 text. Built recovery already queries.
