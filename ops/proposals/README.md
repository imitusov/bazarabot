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
| #52 F-52 `_remember` swallows `Exception` | implementer (code vs rule 23) |
| #51 F-51 closed days collapse | **amender**: `SessionInfo` needs a date — see `51-models-sessioninfo-trade-date.md` |

### Theme C — Execution / process identity

| Issue | Next |
|---|---|
| #50 F-50 locks bound to first event loop | implementer in `execution.orders` (attended) |
| #22 F-22 single-instance flock | **amender** then `app.startup` — see `22-app-startup-instance-lock.md` |
| #28 F-28 slippage/liquidity | **amender**; do not auto-amend money/product |

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
| #16 F-16 portfolio risk | **amender**; `area:risk` waits for a human |
| #25 F-25 backup volume | **amender** / deploy |
| #44 V12 `get_operations` | live broker measurement first |
| #13 #14 sandbox/ML | research last |

### Implementer dispatch order (after amendments)

1. `logging_setup` (#56)
2. `market.session` (#52)
3. Other O-* producers, one module per session
4. `export_health.py` (#68)
5. `execution.orders` (#50) last among live money
6. Sandbox last
