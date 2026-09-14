# Task 13/43: Implement `zarabot/lifecycle/exits.py`

## Product context

Pure. Decides which of the three exit triggers fires. Returns STOP_LOSS only for LOCAL-protected positions. 95% coverage.

## Build order position

Module **13** of 43 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/lifecycle/exits.py`

**`evaluate(position: Position, price: Decimal, now: datetime, session: SessionInfo, trading_days_open: int | None, config: Config) → ExitTrigger | None`**
- Pure. Returns the trigger that fires, or `None`.
- `STOP_LOSS` when `price ≤ position.stop_price` **and only when
  `position.stop_protection == 'LOCAL'`**. When the exchange holds the stop, this
  module must never return `STOP_LOSS`: the trigger has exactly one owner at a
  time, and both acting on the same position would sell it twice. Ownership is
  recorded on the position, not inferred.
- `TAKE_PROFIT` when `price ≥ position.target_price`.
- `MAX_AGE` when `trading_days_open ≥ MAX_HOLDING_DAYS` **and**
  `session.in_closing_window(now)` — the `SessionInfo` method (§4 `models`),
  with `minutes` left at its default of 15. This is a comparison over the
  `SessionInfo` argument's own fields, so the module stays pure: it does not call
  `market.session`, which does I/O.
- **`trading_days_open` is `None` when the age could not be measured, and then
  `MAX_AGE` never fires (v1.44).** The recorded calendar may not reach back to a
  position's entry after an outage longer than the schedule window, and the
  caller says so rather than passing a number it knows is short. A short number
  reads as a young position, which is the silent failure #45 was: nothing
  raises, the exit simply never comes. `None` keeps `STOP_LOSS` and
  `TAKE_PROFIT` working — they need only a price — and suppresses exactly the
  one trigger that depends on the count.
- The type is `int | None` rather than a sentinel like `-1` because an unmeasured
  age is a different kind of thing from a measured one, and the type is where
  that belongs.
- Precedence when more than one applies: `STOP_LOSS`, then `TAKE_PROFIT`, then
  `MAX_AGE`. Fixed, so the recorded reason never depends on evaluation order.
- Boundaries are inclusive at the stop and the target.
- **The threshold is `config.max_holding_days` and never a literal (v1.91).**
  This module holds no number of its own; the horizon changed from 3 to 18 in
  v1.91 and no line of this contract changed with it, which is the property that
  made the change a configuration edit rather than a re-specification. A test
  that pins the count to 3 rather than to the configured value is the way that
  property gets lost.
- Must never place an order, and must never consult a halt — an active halt does
  not suppress exits.

**How `MAX_HOLDING_DAYS` was derived, and how to derive it again (v1.91).**

Until v1.91 the horizon was three trading days and neither price exit was
reachable inside it. Every position the bot had ever closed — six of six — exited
on `MAX_AGE`, with realised moves spanning −1.0% to +3.3% inside a −5%/+10% band
(#211). The stop-loss path had never executed on real money, and the take-profit
class that `sandbox/train.py` labels on was empty by construction, which is the
mechanical cause of #14's near-zero positive rate. The brief's decision is to
lengthen the hold and leave the bands alone (brief §9); this section is the
arithmetic behind the number, recorded so that it can be re-checked rather than
re-argued.

**The number is a function of assumed volatility, not a constant of nature.**
Nothing below is a property of the market. Every figure is conditional on a
single assumed daily volatility, and the answer moves by a factor of two and a
half — seven trading days to eighteen — across the range the audit measured.

*The model.* A driftless log random walk, one path per position, eight intraday
sub-steps per trading day so that a bar has a real high and a real low. A
position resolves on the first bar whose low touches `entry × (1 − 5%)` or whose
high touches `entry × (1 + 10%)`; a bar touching both counts as the stop. That is
exactly the rule `sandbox/train.py::_label` applies, which is what makes the
label horizon and the trade horizon the same question (see `sandbox/` §4).
Figures below are 200,000 independent paths, standard error about ±0.11
percentage points — reproduce them approximately, not bit-for-bit.

*The model reproduces the measurement it is being run backwards from.* At a
three-day horizon it gives a target-before-stop rate of **0.02% at 1.5%/day** and
**2.1% at 2.5%/day**, against the audit's measured 0.00% and 2.96% (#211, #14).
Agreement at the horizon where the answer is already known is the only reason to
trust it at horizons where it is not.

*Two closed forms bound it.* In log terms the barriers are `a = ln(1.10) =
+0.0953` and `b = −ln(0.95) = 0.0513`. With unlimited time a driftless walk
reaches the target first with probability `b / (a + b) = 35.0%` — that is the
ceiling, and no holding period beats it. The mean time to resolve on either
barrier is `a · b / σ²` trading days: **21.7 at 1.5%/day, 12.2 at 2.0%, 7.8 at
2.5%**.

*The criterion.* The horizon is long enough when **the price band, not the clock,
is the usual answer** — the smallest whole number of trading days at which more
than half of positions resolve on the stop or the target. That is the median
resolution time, which is shorter than the mean above because the distribution is
right-skewed.

| Daily volatility | H where the band resolves >50% | `MAX_AGE` share at that H | Target-first at that H | Stop-first at that H |
|---|---|---|---|---|
| 1.5% | **18 trading days** | 48.5% | 12.1% | 39.3% |
| 2.0% | 11 trading days | 46.3% | 13.4% | 40.4% |
| 2.5% | 7 trading days | 47.8% | 12.8% | 39.4% |

For comparison, at the old three-day horizon: `MAX_AGE` 96.2% / 88.4% / 78.5% and
target-first 0.02% / 0.45% / 2.1% at the same three volatilities.

*One independent check, on real bars rather than synthetic ones.* When #53 made
`MAX_AGE` reachable in the backtester, the same 180-bar run at the shipped
`MAX_HOLDING_DAYS=3` produced 13 exits, all `MAX_AGE`; loosening the cap to 8
brought all three triggers back (`ops/STATE.md`, 2026-09-07). That is the
direction this table predicts and roughly the place it predicts it: at 8 the
model puts the resolved-on-price share at 22% / 41% / 58% across the three
volatilities — enough for the stop and the target to *appear*, not enough for
either to be the usual answer. It is corroboration of the shape, not a second
measurement of the number; one strategy over one 180-bar window is not a
distribution.

*The chosen value is 18, at an assumed 1.5%/day.* The low end of the audit's
range is taken rather than the midpoint because 1.5%/day is the volatility at
which the measured positive rate was **exactly zero** — the observation that
opened #211. A horizon derived at 2.0% gives eleven days, and eleven days leaves
the case that produced the incident still broken: at 1.5%/day it resolves 32% of
positions on a price and 68% on the clock.

*To re-derive it, H scales as `σ⁻²`.* `H(σ) ≈ 17.4 × (1.5% / σ)²` over
1–2.5%/day, good to about one trading day — the table above is the authority
where the two differ. Halving assumed volatility quadruples the horizon: at
1.0%/day the same criterion gives about 39 trading days, roughly two calendar
months. That sensitivity is why the brief
attaches a re-open condition to a measurement of realised watchlist volatility
(brief §9) rather than treating 18 as settled.

*What the change buys, in the two places it was supposed to.* At 1.5%/day the
stop-first rate rises from 3.8% to 39.3%, so the stop path stops being code with
no production evidence behind it; and the take-profit label's positive class
rises from 0.02% to 12.1%, which is a class a classifier can be trained on.
Neither is a claim about profit. A longer hold makes the exits reachable; it does
not make them favourable.

**What else keys off the horizon (v1.91).** Traced when the number changed, and
recorded so the next change traces the same list.

- **`config`** is the only definition. `lifecycle.exits.evaluate` is the only
  module in `zarabot/` that compares against it, and `telegram.commands` renders
  `cfg.max_holding_days` into `/limits`. No module carries a literal 3, so no
  module other than `config` changes.
- **`clock.trading_days_between`, `market.session.calendar()` and
  `app.loops._age_in_trading_days`** produce the count the comparison is made
  against. The count is unchanged in kind, but its *dependency* changes: the
  schedule window is 14 calendar days and the broker refuses a wider one (§2.1),
  while a hold of H trading days spans roughly `7H/5` calendar days. At H ≤ 10
  the single refresh that ran on a position's entry day already covers its whole
  life; at H = 18 (about 25 calendar days) it does not, and the age count depends
  on at least one further successful refresh during the hold. Daily rollover
  supplies one, so this is a change in what the guarantee rests on, not a break —
  and `market.session` §4 records the case where it does break.
- **`risk.gate`.** The re-entry cooldown is 120 minutes against an 18-day hold,
  so the duplicate-ticker rule now does nearly all of the anti-looping work: a
  ticker is unavailable for the length of the hold, and the cooldown binds only
  in the two hours after it frees up. No limit changes; the brief's §12 row is
  restated rather than amended.
- **`reporter.weekly`.** Closed trades per week fall roughly six-fold — about
  three a week across all strategies in steady state, against seventeen. "A week
  with no closed trades produces a valid report saying so" stops being an edge
  case and becomes an ordinary week. Nothing in the contract changes.
- **`sandbox.backtest` and `sandbox.exchange`.** A backtest fixture must now be
  long enough for `MAX_AGE` to be reachable at all; see §3.2.
- **`sandbox/train.py`.** The label horizon is the same horizon. See `sandbox/`
  §4.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Price exactly at the stop level triggers `STOP_LOSS`; one increment above does
  not (boundary).
- Price exactly at the target triggers `TAKE_PROFIT`; one increment below does
  not (boundary).
- A position whose age reaches the maximum during the final fifteen minutes
  triggers `MAX_AGE`; the same position earlier in that session does not
  (boundary, proves the timing rule).
- Every maximum-age case builds its threshold from `config.max_holding_days`
  rather than from the literal 18, and a case with the config value set to a
  different number ages by that number instead (proves the module reads the
  configured horizon. Without it, a suite written against today's default passes
  unchanged after the horizon moves and proves nothing about the move — and the
  horizon has already moved once, from 3 to 18 in v1.91).
- A position at both stop and maximum age returns `STOP_LOSS` (proves the
  documented precedence, so the recorded reason is deterministic).
- Age is counted in trading days: a position opened Friday is not aged by the
  weekend (proves calendar-aware ageing).
- An adopted position ages from its adoption timestamp (proves the reconciliation
  interaction).
- Evaluation is pure and repeatable for identical inputs.

## Expected output

- `zarabot/lifecycle/exits.py` implementing the contract exactly
- `tests/test_lifecycle_exits.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_lifecycle_exits.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/lifecycle/exits.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
