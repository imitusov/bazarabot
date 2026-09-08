# Proposal: latch `clock_drift` and do not suspend exits (O-04 leftover)

**Kind:** spec gap. `app.loops` implements the contract as written.

§4 `app.loops` observability:

> When `clock.now()` is not UTC-aware, emit `clock_drift` (WARNING) with
> `drift_seconds` 0 and refuse to run the cycle.

A naive `clock.now()` is a standing defect, not weather. With a one-minute poll
this WARNING repeats forever. The cycle is also the **exit** path, so refusing
the whole cycle stops take-profit, max-age and LOCAL stop-loss on every open
position, forever, signalled only by a WARNING. §4 elsewhere says a process
on its way down "must not be stopped from closing what is already open". Those
two sentences cannot both stand.

**Should say:**

1. Latch: emit `clock_drift` once per process (or until `now()` is aware again),
   then stay silent until a cycle observes an aware `now()` and re-arms.
2. Alert once (Telegram), not only a WARNING — this condition suspends trading
   including exits.
3. Carve-out: a naive clock still refuses **entries** and snapshot/loss
   measurement; **exits and exchange-executed stop booking still run**, using
   the naive instant only if the spec later allows it, or by refusing new
   entries while still walking `_submit_exits` / `_close_executed`. That
   carve-out is a new error-rule decision; do not invent it in `app.loops` first.

**Test contract:** two naive cycles emit one `clock_drift` and one alert; a
later aware cycle then a naive cycle emits a second. After the carve-out:
naive `now()` still closes a LOCAL stop-loss.

**Modules to re-run:** `33-app-loops` after the amendment.

**Do not:** invent a latch, an alert, or an exit carve-out in `app.loops`
before these sentences exist.
