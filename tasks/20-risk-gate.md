# Task 20/38: Implement `zarabot/risk/gate.py`

## Product context

Pure. Every entry check, with a fixed rejection priority so the recorded reason is deterministic. 95% coverage.

## Build order position

Module **20** of 38 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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
  `MAX_POSITIONS` → `COOLDOWN_ACTIVE` → `INSUFFICIENT_CASH` → `ZERO_LOTS` →
  `POSITION_CAP`.
- `MAX_POSITIONS` applies at or above the configured maximum.
- Rejects any signal whose side is `SELL`. Exits never pass through this module.
- Must never perform I/O, and must never mutate `state`.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

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
