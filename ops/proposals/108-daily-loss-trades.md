# Proposal: #108 S-18 — “trades that produced” daily-loss halt must have an assembler

**Kind:** spec. Rule 20: alert with the loss **and the trades that produced
it**. `app.loops` alerts a percentage only. Nothing lists today's closes.
Vacuously true today.

**Should say** one of:

1. Drop the trades clause; percentage + `daily_loss_pct` extra is enough.
2. Filter the existing `db.positions.list_closed()` (no date argument;
   every closed row already has `closed_at` / `exit_at`). Use the Moscow
   day filter already written as `reporter/weekly.py:43 _in_period`.
   Assembler at the call site (`app.loops` or `pnl`): ticker, lots,
   realised. Cap length. No tokens. **Do not** add a dated repository
   method.

**Do not:** dump full `Position` repr (account-adjacent ids, huge);
commission a new `list_closed_on(date)` (or similar).

**Modules:** spec rule 20 + `app.loops` (or `pnl`) after a choice. Not
`db.positions` unless the filter is copied, not a new query.
