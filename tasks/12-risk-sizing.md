# Task 12/40: Implement `zarabot/risk/sizing.py`

## Product context

Pure. Converts a price and a budget into whole lots, always rounding down. 95% coverage.

## Build order position

Module **12** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/risk/sizing.py`

**`position_budget(allocated: Decimal, size_pct: Decimal) → Decimal`**
- Pure. Returns `size_pct% × allocated` — the intended cost of one position,
  before headroom and the cash reserve narrow it further.
- Exists so that the budget has **one** definition. `size_position` computes it
  to bound an order; `app.startup` compares it against a lot cost to decide
  whether any order is possible at all. Two copies of the formula in two modules
  is a drift hazard on the money path, and the second copy would be in an
  orchestration module with a 70% coverage floor.
- `size_position` **must** obtain its budget from this function rather than
  recomputing it. That is the whole point of extracting it, and a
  reimplementation satisfies the signature while losing the guarantee.
- Never negative. `allocated ≤ 0` is refused by `config.load()` and is not this
  function's concern.

**`size_position(price: Decimal, instrument: Instrument, allocated: Decimal, cash: Decimal, size_pct: Decimal, open_cost: Decimal, reserve_pct: Decimal) → int`**
- Pure. Returns the number of **whole lots** to buy.
- Rounds down, always. Never returns a negative number.
- Bounded by three quantities, and the smallest wins:
  - **budget** — `position_budget(allocated, size_pct)`, the intended size of
    one position. Obtained from that function, never recomputed here;
  - **headroom** — `allocated − open_cost`, so the portfolio's total cost never
    exceeds the allocated capital (#16). `open_cost` is the summed cost of
    positions already open, supplied by the caller because this function is pure;
  - **spendable** — `cash × (100 − reserve_pct)%`, a buying-power reserve.
- The returned value must satisfy, for every possible input:
  `lots × lot_size × price ≤ allocated − open_cost` and `≤ cash`.
- `reserve_pct` holds back a slice of cash so that fees, price movement between
  sizing and fill, and lot rounding cannot turn an approved order into one the
  broker refuses for insufficient funds. **It is a reserve, not an estimate of
  commission**: nothing here predicts what the fee will be, and nothing derived
  from it is ever recorded as a commission. Commission remains what the broker
  reports it charged, and only that.
- `cap_pct` was removed in v1.30. It could never be the binding minimum, because
  `config.load()` refused any configuration where `size_pct` exceeded it — so the
  per-position cap bounded nothing while appearing in the operator's risk summary
  as an active control (#15). The portfolio headroom replaces it with a ceiling
  that can actually bind.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- With `open_cost` leaving less headroom than one lot, returns 0 (proves the
  portfolio ceiling binds — the control that replaced a per-position cap which
  could not, #15/#16).
- The reserve is honoured: with cash exactly equal to one lot and a non-zero
  `reserve_pct`, returns 0 rather than an order the broker would refuse.
- `lots × lot_size × price ≤ allocated − open_cost` holds for every input
  (property, not example).
- A standard case returns whole lots at or below the configured percentage
  (happy path).
- A price so high that one lot exceeds the position cap returns zero lots
  (proves the expensive-instrument path, which would otherwise over-allocate).
- Available cash below the cost of one lot returns zero lots (proves cash is
  respected independently of the percentage).
- Rounding is always downward: a budget worth 2.9 lots returns 2 (proves the
  intended direction of error).
- The returned lot count multiplied by lot size and price never exceeds
  `allocated − open_cost` for any input (proves the ceiling is structural, as a
  property over the input space rather than an example).
- Integer division truncates rather than dividing then rounding down. At the
  default decimal context `8.999…9 / 3` evaluates to exactly `3`, so dividing
  first would return three lots costing 9 against 8.999…9 of headroom — one lot
  of real money above the ceiling the function exists to enforce.
- `position_budget` returns `size_pct%` of `allocated` exactly, in `Decimal`
  (proves the formula, and that it is not routed through `float`).
- `size_position`'s budget bound and `position_budget` agree for the same inputs
  (proves the single definition — the seam this extraction exists to close,
  asserted on the caller's output rather than on the callee's, per failure
  class 6).

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
