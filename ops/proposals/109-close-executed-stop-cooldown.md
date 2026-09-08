# Proposal: #109 S-19 — `close_executed_stop` must start the cooldown

**Kind:** spec. Rule 26: exchange-executed stop → close STOP_LOSS **and start
the cooldown**. `close_position` §4 names the cooldown; `close_executed_stop`
does not. Code shares `_finish_close` so it works; a rebuild from §4 alone
drops it.

**Should say:** `close_executed_stop` starts the same cooldown as `close_position`.
§3.2 already has a pin; keep it.

**Do not:** skip cooldown on exchange stops.

**Modules:** spec `execution.orders`. Code already correct.
