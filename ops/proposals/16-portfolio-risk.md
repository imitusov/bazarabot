# Proposal: #16 remaining portfolio-level risk (human / brief)

**Kind:** product. `area:risk`. Do not auto-amend. Do not re-implement
`PORTFOLIO_EXPOSURE`.

**Already on `main` (do not re-implement):**

- `broker.client.get_portfolio` cash is RUB buying power (`held - blocked` on
  `RUB000UTSTOM`), not `total_amount_currencies` (#16 gap 4).
- `size_position` takes `reserve_pct` / `CASH_RESERVE_PCT` so fees and rounding
  have a configured slice. This module still **never estimates commission**
  (Must NEVER).
- `config.load()` still checks `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100`
  (nominal allocation).
- **Runtime total-exposure — shipped in v1.30.** `risk/gate.py` rejects with
  `PORTFOLIO_EXPOSURE` when `allocated_capital - open_cost < lot_cost`
  (algebraically `open_cost + lot_cost > allocated_capital`). `size_position`
  bounds approved lots by `headroom = allocated - open_cost`, so
  `open_cost + lots × lot_cost ≤ allocated` holds for the whole order, not
  just one lot. Four tests assert it, including
  `test_open_cost_sums_every_position_not_just_the_largest`.

**Closed by the brief (not a remaining gap):**

1. **Sector cap — whether to have one.** Brief §11: a sector cap is required
   **before** the watchlist holds two names in one sector; today it holds four
   names in four sectors, so the control would bind on nothing and is
   **deliberately deferred**. Re-open when a second name in one sector is
   added to the watchlist.
2. **Whether `ALLOCATED_CAPITAL` tracks realised P&L.** Answered. §21 puts the
   capital amount in deploy-time configuration, with every limit a percentage
   of it, and closes "Open questions … **None.**" §11 scopes drifting equity
   to the daily-loss limit alone (`pnl.bot_equity()`, consumed at
   `loops.py` daily-loss measurement) while exposure stays bounded within
   allocated capital. Do not silently rebase sizing to `bot_equity()`.

**Still open (cap value only):**

- **Sector cap number.** §11 fixed the timing, not the value. Ask the brief
  for a max-per-sector number before writing a map. `risk.gate` stays pure:
  grouping is an *input* (`sector_of: dict[str, str]` plus `max_per_sector`),
  not a lookup. New `RejectionReason` (name TBD). 95% coverage on every
  branch. If that re-opens: `business-brief` (human), then `config` (the map)
  and `risk.gate` (new input; `app.loops` is the only `gate.check` call site).

**§3.2 (when the brief names a cap value):**

- N open in one sector, cap N → N+1 rejected with the sector reason.
- Open cost at the ceiling → `PORTFOLIO_EXPOSURE` (already tested; keep).
- Non-RUB balances still must not inflate `PortfolioState.cash` (already
  tested; keep).

**Stop:** no ticker→sector map in config until the brief names the sectors and
the cap. A wrong map is a silent concentration hole. Do not send anyone to
implement `PORTFOLIO_EXPOSURE`.
