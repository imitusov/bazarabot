# Proposal: latch `clock_drift` (O-04 leftover)

**Kind:** spec gap. `app.loops` implements the contract as written.

§4 `app.loops` observability:

> When `clock.now()` is not UTC-aware, emit `clock_drift` (WARNING) with
> `drift_seconds` 0 and refuse to run the cycle.

A naive `clock.now()` is a standing defect, not weather. With a one-minute poll
this WARNING repeats forever while the bot silently does not trade. The module
already latches the same shape for unmeasurable daily loss and unmeasurable age,
with the spec's blessing. §4 states neither a cadence nor a latch for `clock_drift`.

**Should say:** emit `clock_drift` once per process (or once until `now()` is
aware again), then stay silent until a cycle observes an aware `now()` and
re-arms. Do not Telegram-alert; WARNING log is enough.

**Test contract:** two naive cycles in one process emit one `clock_drift`; a
later aware cycle then a naive cycle emits a second.

**Modules to re-run:** `33-app-loops` after the amendment.

**Do not:** invent a latch in `app.loops` before this sentence exists.
