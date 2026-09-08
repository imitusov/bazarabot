# Proposal: #93 S-03 — pnl §4 must not describe the discarded halt

**Kind:** spec. `state.halt.halt` upgrades: `DAILY_LOSS_LIMIT` >
`RECONCILIATION_MISMATCH` > `MANUAL`. `pnl` §4 still states the old
"returns early, daily-loss during MANUAL discarded" as present tense, then
aspirationally the fix.

**Should say:** `pnl` names `halt(..., daily_loss_pct=...)` and defers
severity to `state.halt`. Do not re-specify discard.

**Do not:** change `pnl` to skip `halt` when already MANUAL.

**Modules:** spec `pnl` section; code already upgraded (v1.69/v1.70).
