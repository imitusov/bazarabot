# Proposal: #106 S-16 — rule 9b is the latch, not once-per-cycle

**Kind:** spec. Rule 9b: alert once per cycle with the count. `app.loops`:
latch until a clean cycle. Code implements the latch. Per-cycle alerts are
the 510-message defect rule 36 exists to stop.

**Should say:** §8 matches §4 latch. “Once per cycle” is wrong.

**Do not:** change `app.loops` to Telegram every cycle.

**Modules:** spec §8 rule 9b. Code already latched.
