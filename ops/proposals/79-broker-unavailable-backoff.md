# Proposal: drop `backoff_seconds` from `broker_unavailable`

**Kind:** spec defect (failure class 4). v1.61 requires `broker.client` to emit
`broker_unavailable` with `backoff_seconds`. Delay lives in `app.loops` (the
escalating back-off, its cap, and `retry_after`). The client can only emit `0`.
An operator reading `backoff_seconds: 0` will think no back-off is in effect
while loops is five cycles deep.

**Should say (§4 `broker.client` and §7.1):** `broker_unavailable` carries
`method` and `consecutive_failures` only. Do not list `backoff_seconds`.

**`app.loops`:** when it actually sleeps after `BrokerUnavailable`, it may add
`backoff_seconds` to `task_crashed` or to a dedicated back-off log — that is a
separate sentence in the loops contract, not a second `broker_unavailable`.
Callers still must not re-emit `broker_unavailable`.

**Do not:** compute a fake delay in `broker.client` to fill the field.

**Test contract:** a `BrokerUnavailable` log has `method` and
`consecutive_failures` and no `backoff_seconds` key.

**Modules to re-run:** `21-broker-client` (#79 rebase). `33-app-loops` only if
the amendment also assigns delay to a loops event.

**Hold #79 until this amendment lands.**
