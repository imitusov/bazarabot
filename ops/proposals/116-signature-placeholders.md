# Proposal: #116 S-26 — §4 signatures must be matchable to `interfaces.md`

**Kind:** spec. Placeholders: `write_daily(snapshot)`, `list[...]`,
`evaluate(...)`, `run(ctx)`. Real mismatch: `SimulatedExchange` omits
`reject_stops=False`.

**Should say:** every bold signature has `→` and `name: type`. Fill from
`interfaces.md` (code is source of truth until amended).

**Do not:** drop `reject_stops` from the sandbox to match the shorter spec.

**Modules:** spec §4. `sandbox.exchange` only if the sixth param is removed
(it should not be).
