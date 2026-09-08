# Proposal: #99 S-09 — gate §3.2 must list live reasons only

**Kind:** spec. `risk.gate` 95% module. Six lines in one §3.2 block: POSITION_CAP
is described as removed, then the “every branch, one test each” list still
names **position cap**. `PORTFOLIO_EXPOSURE` replaced it (v1.30).
`BROKER_LOT_LIMIT` is live and omitted.

**Should say:** the enumerated reasons are exactly the live
`RejectionReason` set: halted, session closed, `PORTFOLIO_EXPOSURE`, max
positions, cooldown, insufficient cash, zero lots, instrument not trading,
duplicate ticker, `BROKER_LOT_LIMIT` (and any other still on the enum).
No `POSITION_CAP`.

**Do not:** add a test that constructs `max_position_pct` — `config.load()`
rejects that state.

**Modules:** spec §3.2 `risk.gate` only. Tests already cover live reasons;
amend so a regenerated task cannot demand the dead one.
