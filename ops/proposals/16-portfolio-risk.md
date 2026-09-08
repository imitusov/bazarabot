# Proposal: #16 remaining portfolio-level risk (human / brief)

**Kind:** product + spec. `area:risk`. Do not auto-amend. Several of the
original four gaps are already closed on `main`; inventing a sector map in
`risk.gate` without the brief is a new risk product.

**Already on `main` (do not re-implement):**

- `broker.client.get_portfolio` cash is RUB buying power (`held - blocked` on
  `RUB000UTSTOM`), not `total_amount_currencies` (#16 gap 4).
- `size_position` takes `reserve_pct` / `CASH_RESERVE_PCT` so fees and rounding
  have a configured slice. This module still **never estimates commission**
  (Must NEVER).
- `config.load()` still checks `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100`
  (nominal allocation).

**Still open, needs a brief sentence first:**

1. **Sector / correlation cap.** Gate today: duplicate ticker + max open count.
   Ten banks pass as ten bets. `risk.gate` stays pure: grouping must be an
   *input* (e.g. `sector_of: dict[str, str]` plus `max_per_sector`), not a
   lookup. New `RejectionReason` (name TBD). 95% coverage on every branch.
2. **Runtime total-exposure.** Sum of open cost + this order vs
   `ALLOCATED_CAPITAL` (or vs current equity — that is the next decision).
   Sizing already sees `open_cost`; the gate may need a named reason if
   sizing returning `0` is not visible enough.
3. **Whether `ALLOCATED_CAPITAL` tracks realised P&L.** Static env vs drifting
   equity. Record in `business-brief.md`; do not silently rebase sizing to
   `bot_equity()`.

**§3.2 (after the brief chooses numbers):**

- N open in one sector, cap N → N+1 rejected with the sector reason.
- Open cost at the ceiling → next signal rejected regardless of cash.
- Non-RUB balances still must not inflate `PortfolioState.cash` (already
  tested; keep).

**Modules, lowest first:** `business-brief` (human), then spec `risk.gate` /
`risk.sizing` / maybe `broker.client` only if cash is still wrong (it is not).

**Stop:** no ticker→sector map in config until the brief names the sectors and
the cap. A wrong map is a silent concentration hole.
