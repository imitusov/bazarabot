# Task 20/42: Implement `zarabot/risk/gate.py`

## Product context

Pure. Every entry check, with a fixed rejection priority so the recorded reason is deterministic. 95% coverage.

## Build order position

Module **20** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/risk/gate.py`

**`check(signal: Signal, state: PortfolioState, instrument: Instrument, cooldown_active: bool, session_open: bool, halted: bool, now: datetime, config: Config) → RiskDecision`**
- Pure. Calls `risk.sizing` and returns approval with a lot count, or rejection
  with exactly one reason.
- Rejection reasons are evaluated in this fixed priority order, so that the
  recorded reason is deterministic when several apply:
  `HALTED` → `SESSION_CLOSED` → `INSTRUMENT_NOT_TRADING` → `DUPLICATE_TICKER` →
  `MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` →
  `PORTFOLIO_EXPOSURE` → `ZERO_LOTS`.
- `MAX_POSITIONS` applies at or above the configured maximum.
- **`PORTFOLIO_EXPOSURE`** rejects when the summed cost of open positions leaves
  less headroom than one lot: `allocated − open_cost < lot_cost`.
  **It cannot bind on a portfolio the gate sized by itself**, and that is not a
  defect. If every open position cost at most one budget and at most
  `max_open_positions − 1` are open, the surviving configuration bound
  `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100` guarantees headroom for another.
  It binds on holdings the gate did not size: a position adopted by
  `broker.reconcile` during crash recovery, or `ALLOCATED_CAPITAL` lowered
  between runs. Those are precisely the runtime cases #16 names, and the ones a
  configuration-time check cannot see. The gate
  computes `open_cost` from `state.positions`, which carry entry price, lots and
  lot size, so this stays pure and needs no new argument. Before v1.30 the only
  exposure controls were the duplicate-ticker check and a position count, so
  `MAX_OPEN_POSITIONS × POSITION_SIZE_PCT ≤ 100` bounded *nominal* allocation at
  configuration time and nothing bounded it at runtime (#16).
- A **sector or correlation cap is deliberately not implemented yet.** Ten
  positions in ten Russian banks pass every check above as ten independent bets
  and behave in a drawdown as one position at ten times the size — the largest
  unmodelled risk in the system. It is not implemented because it needs a
  ticker→sector grouping supplied as an input (this module must stay pure), and
  on the current four-instrument watchlist, four distinct sectors, it would bind
  on nothing. It becomes required before the watchlist holds two names in one
  sector, and this paragraph is the reminder.
- Rejects any signal whose side is `SELL`, with `ZERO_LOTS`, evaluated in that
  reason's slot rather than earlier — so the side of a signal cannot change which
  reason is recorded for a state where several apply. Exits never pass through
  this module.
- A rejection caused by the **cash reserve** rather than by raw cash surfaces as
  `ZERO_LOTS`, not `INSUFFICIENT_CASH`: `INSUFFICIENT_CASH` is defined on cash
  before the reserve is applied. The distinction is deliberate but makes a
  near-miss on funds read as a sizing result in the rejection statistics, which
  is worth knowing when reading them.
- Must never perform I/O, and must never mutate `state`.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Open positions whose summed cost leaves less than one lot of headroom reject
  with `PORTFOLIO_EXPOSURE` (proves the runtime exposure ceiling exists; before
  v1.30 only a configuration-time bound did).
- `PORTFOLIO_EXPOSURE` is evaluated after `INSUFFICIENT_CASH` and before
  `ZERO_LOTS` (proves the fixed priority, so the recorded reason is
  deterministic).
- No test constructs a `Config` the loader would refuse. The removed
  `POSITION_CAP` case did exactly that — it set `position_size_pct=50` against
  `max_position_pct=20`, a state `config.load()` rejects — so a green test
  asserted behaviour the assembled system could not produce (#15).
- A clean signal in an unremarkable portfolio is approved (happy path).
- Each rejection reason is produced by a state constructed to trigger exactly it:
  halted, session closed, position cap, maximum positions, cooldown active,
  insufficient cash, zero lots, instrument not trading, and an existing position
  in the same ticker (proves every branch, one test each).
- With several violations present at once, the rejection reason is the
  highest-priority one, deterministically (proves rejection reporting is stable
  and not order-dependent).
- Exactly at `MAX_OPEN_POSITIONS` a new entry is rejected; at one below it is
  approved (boundary, proves inclusivity).
- The gate never returns approval for a `SELL` (proves exits never route through
  the gate).
- The gate performs no I/O — verified by calling it with every collaborator
  absent (proves purity).

## Expected output

- `zarabot/risk/gate.py` implementing the contract exactly
- `tests/test_risk_gate.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_risk_gate.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/risk/gate.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
