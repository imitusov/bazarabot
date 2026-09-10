# Amendment proposals

Written by the Cursor batch loop when a fix needs the contract to change.
Cursor never edits `technical-spec.md`; it proposes, and the amendment is
reviewed and applied separately.

One file per issue: `<issue>-<module>.md`, containing the contract line that is
wrong or missing, what it should say, the test contract cases that would have
caught the bug, any error rule needed, and every module that would need
re-running.

Delete a proposal once its amendment is merged.

## Open GitHub issues grouped by theme (2026-09-07, after `c94e3c7`)

Per `operations-loop.md`: findings become spec amendments before code. Implementers
run **one module per session**, test-first, two commits, never edit the spec.

### Theme A — Observability (O-00–O-15)

| Issue | Status vs current `main` | Next action |
|---|---|---|
| #54 O-00 tracking | meta | leave open until children close |
| #55 O-01 assign events to modules | **amender** | remaining §7.1 events still live only in the catalog; `startup_ok` already in `app.startup` (v1.58 / `9f7b146`) |
| #56 O-02 `logging_setup` `moscow_time` | **implementer** | PR `cursor/obs-logging-setup-4db9` |
| #57 O-03 `startup_ok` | **done on main** | close referencing `9f7b146` |
| #58–#67 producers | mix | amend each owning module section, then one session per module |
| #68 `export_health.py` | after events exist | host tooling |
| #69 VPS verification | operator | cannot close from CI |

### Theme B — Calendar / session (`market.session`)

| Issue | Next |
|---|---|
| #52 F-52 `_remember` swallows `Exception` | **amender** then implementer — see `52-market-session-remember.md` |
| #51 F-51 closed days collapse | **amender**: `SessionInfo` needs a date — see `51-models-sessioninfo-trade-date.md` |

### Theme C — Execution / process identity

| Issue | Next |
|---|---|
| #50 F-50 locks bound to first event loop | implementer in `execution.orders` (attended) |
| #22 F-22 single-instance flock | **amender** then `app.startup` — see `22-app-startup-instance-lock.md` |
| #28 F-28 slippage/liquidity | **owner decision applied** — brief §12 alert-only, spec v1.74 `execution.orders` §4 + §3.2 + `FILL_SLIPPAGE_ALERT_PCT`. Proposal deleted. Liquidity half declined: no gate input, no new `RejectionReason`. Next: implementer in `execution.orders` |

### Theme D — Wiring / reporting

| Issue | Next |
|---|---|
| #36 F-36 `/report` wiring | **amender** — see `36-app-startup-report-wiring.md` |
| #17 F-17 snapshot equity curve | **amender** then `app.loops` |
| #46 F-46 instruments cache | **amender**; new repository |

### Theme E — Coverage / money / research / live

| Issue | Next |
|---|---|
| #30 F-30 coverage floors | likely **stale** (ratchet already ≥80%) |
| #16 F-16 portfolio risk | **owner decision applied** — sector/correlation cap **declined**, out of scope in brief §11; the axis is strategy, with no number set. Proposal deleted. No `risk.gate` change, no sector map, no new `RejectionReason`. Close referencing the brief amendment |
| #25 F-25 backup volume | **amender** / deploy |
| #44 V12 `get_operations` | **done** — `scripts/verify/verify_operations.py`, measured 2026-09-10 into §2.1. Fee attribution verified; the SELL check fails until the account sells once, and #44 stays open on that |
| #13 #14 sandbox/ML | research last |

### Theme F — Spec / interfaces drift

| Issue | Next |
|---|---|
| #166 S-34 `ConfigError.variable` | **amender** — see `166-configerror-variable-spec.md`; modules to re-run: none; #135 leftover is `app.startup` (do not implement here) |
| #177 S-36 table ownership outside `db/` | **owner decision** — see `177-table-ownership-outside-db.md`; two coherent readings, code already implements one; do not touch `state/halt.py` or `broker/reconcile.py` until it is made |
| #179 S-37 check 2 `any()` | **amender** — see `179-split-signals-snapshots-heading.md`; split the only shared §4 heading; do not patch the gate first; modules to re-run: none until the spec is amended |

### Theme G — §8 rule contradictions (S-01–S-05)

Applied to `technical-spec.md` in v1.73; the five proposal files were deleted
with the amendment, per the instruction above.

| Issue | What landed | Next action |
|---|---|---|
| #91 S-01 rule 6 vs rule 32 | rule 6 restricted to the recognised-order (crash-recovery) path, unrecognised holdings deferred to rule 32; `broker.reconcile` §4 now claims rule 6 | close; no code change |
| #92 S-02 resubmit fallback | §4 `get_order_state` fallback paragraph deleted, V6's third PASS criterion and §2's follow-on sentence corrected | close; no code change |
| #93 S-03 `pnl` halt severity | misfiled "Interaction with an existing halt" paragraph deleted from `pnl` §4; its `halted_at` obligation moved under `state.halt` §4, which owns severity | close; no code change |
| #94 S-04 rule 27 vs §4 | rule 27 turned into a pointer to rule 34's exit clause, quoting §4's terminal-partial paragraph as the real conflict | unbooked-slice question still open |
| #95 S-05 missing stop mid-session | rule 25 states detector/remedy/timing and both options; the choice is left **open** for the owner | **owner decision** before any `app.loops` work |

### Implementer dispatch order (after amendments)

1. `logging_setup` (#56)
2. `market.session` (#52)
3. Other O-* producers, one module per session
4. `export_health.py` (#68)
5. `execution.orders` (#50) last among live money
6. Sandbox last
