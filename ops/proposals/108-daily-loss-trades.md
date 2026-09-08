# Proposal: #108 S-18 — “trades that produced” daily-loss halt must have an assembler

**Kind:** spec. Rule 20: alert with the loss **and the trades that produced
it**. `app.loops` alerts a percentage only. Nothing lists today's closes.
Vacuously true today.

**Should say** one of:

1. Drop the trades clause; percentage + `daily_loss_pct` extra is enough.
2. Name the assembler: `pnl` or `db.positions` list of closes for the
   Moscow date (ticker, lots, realised). Cap length. No tokens.

**Do not:** dump full `Position` repr (account-adjacent ids, huge).

**Modules:** spec rule 20 + either `pnl` or `app.loops` after a choice.
