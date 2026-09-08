# Proposal: #117 S-27 — eleven low-severity items (tick list)

Independent; do not one-amend them all.

1. V6 sandbox vs live preamble.
2. Rate-limit headroom 400x vs 200x arithmetic.
3. `session_closed` required fields vs null `opens_at`/`closes_at` (split
   the §7.1 row). Related O-04 / export_health.
4. V11 before V10.
5. §6 migration list not ascending.
6. Duplicated supervision sentence in `app.loops`.
7. Strategy tasks 15–17, 19 all point at `base.py`.
8. `check_coverage.py` vs AGENTS 80% overall / KNOWN_BELOW / STRICT path exists.
9. **AGENTS.md file tree** omits `job_runs`, `trading_days`, `ops.commissions`,
   `sandbox.exchange` — implementable without the spec.
10. `close_position` never-blocked-by-halt unpinned (test while halted).
11. `UNATTRIBUTED` reporting owed by commands/weekly, stated under orders.

**Do not:** change V6 to run live.

Item 9 can land in a tiny AGENTS.md PR; the rest are spec/brief.
