# Task 12/40: Implement `zarabot/risk/sizing.py`

## Product context

Pure. Converts a price and a budget into whole lots, always rounding down. 95% coverage.

## Build order position

Module **12** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/risk/sizing.py`

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, cap_pct: Decimal) → int`**
- Pure. Returns the number of **whole lots** to buy.
- Rounds down, always. Returns 0 when one lot exceeds the cap or exceeds cash.
- The returned value must satisfy, for every possible input:
  `lots × lot_size × price ≤ cap_pct% × allocated` and `≤ cash`.
- Never returns a negative number.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A standard case returns whole lots at or below the configured percentage
  (happy path).
- A price so high that one lot exceeds the position cap returns zero lots
  (proves the expensive-instrument path, which would otherwise over-allocate).
- Available cash below the cost of one lot returns zero lots (proves cash is
  respected independently of the percentage).
- Rounding is always downward: a budget worth 2.9 lots returns 2 (proves the
  intended direction of error).
- The returned lot count multiplied by lot size and price never exceeds
  `MAX_POSITION_PCT` of allocated capital for any input (proves the ceiling is
  structural).

## Expected output

- `zarabot/risk/sizing.py` implementing the contract exactly
- `tests/test_risk_sizing.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_risk_sizing.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/risk/sizing.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
