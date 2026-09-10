# Task 38/42: Implement `sandbox/train.py`

## Product context

Model training and export with a feature manifest. Walk-forward validation only.

## Build order position

Module **38** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `sandbox/` — laptop research (never imported by server code)

**`async load(ticker: str, start: datetime, end: datetime, interval: CandleInterval, cache_dir: Path = Path("sandbox/cache")) → list[Candle]`**
- Async, because it calls `broker.client` on a cache miss. In a notebook this is
  awaited directly.
- `start` and `end` are timezone-aware; a naive value raises `ValueError`.
- Returns candles oldest-first, empty list when the range holds none.
- Caches to `<cache_dir>/<ticker>_<interval>.parquet`, writing through after a
  fetch. `sandbox/cache/` is gitignored: it is derived data, and committing a
  year of candles would bloat the repository for no benefit.
- A cached range that does not cover the request is extended by fetching only
  the missing span, never by refetching the whole range.
- Must never be imported by `zarabot/`.

**`sandbox/exchange.py` — a simulated broker (v1.46)**

A double for every function of `broker.client` the trading cycle calls, backed
by historical bars. It is the piece that makes a backtest mean something,
because with it the simulation does not *resemble* the live path — it **is** the
live path, with only the broker and the clock replaced.

- **`SimulatedExchange(bars: dict[str, list[Candle]], instruments: dict[str, Instrument], cash: Decimal, slippage: Decimal, commission: Commission, reject_stops: bool = False)`** holds
  simulated cash, holdings, submitted orders and standing stop orders, and a
  cursor into the bars. `advance(moment, phase)` moves the cursor and reports
  that **phase** of each instrument's current bar as its last price — `OPEN`,
  `LOW`, `HIGH` or `CLOSE`, defaulting to `CLOSE`.

  A phase rather than a price (v1.50): the marks are per instrument, and a
  backtest runs the whole watchlist, so a single price passed by the caller
  would report one ticker's low as every ticker's. The exchange holds the bars
  and is the only thing that can resolve a phase per instrument. Standing stops are checked **once per bar**, on first entry to it, so
  four cycles do not become four chances to fire.
- It exposes `get_candles`, `get_last_price`, `get_instrument`, `get_portfolio`,
  `get_max_lots`, `get_order_state`, `post_market_order`, `post_stop_loss`,
  `cancel_stop_order`, `cancel_order`, `list_stop_orders`,
  `get_executed_stop_fills` and `get_trading_schedule` with the signatures and
  the failure types `interfaces.md` records for the real ones. Where the real
  module raises, this raises the same exception.
- **The seam table covers four kinds of escape, and the guard checks all
  four (v1.48).** Broker, clock, configuration **and alerts**. It patched
  `alert` in four modules while eleven import it, and `state.halt` — which
  `trading_cycle` reaches on the daily loss limit — was not among them, so a
  backtest run where credentials happen to be present sent real messages to the
  owner (#49). `telegram.notifier.alert` is patched at its source as well as in
  each importer, so a module that starts importing it later is covered by
  default.
- **A guard that covers one class of escape reads as covering all of them.**
  The guard test asserted only that no broker call escaped, while describing
  itself as proving the table complete. It now asserts, for a whole run, that
  no real alert is sent, no unpatched clock is read and no configuration is
  loaded from the environment. This is the same omission-reads-as-compliance
  shape as the defect #12 was closed for, reproduced inside #12's own fix.
- **`market.session` is driven, not stubbed.** The simulator answers
  `get_trading_schedule`, and the real `refresh` / `is_open` / `calendar` /
  `covers` run on top. A backtest that stubbed those would not exercise the
  code that decides whether the market is open, which is where #39 and #43
  lived.

**Fill model.** The four rules below are where a backtest is honest or is not:

1. **Decide at a bar's close, fill at the next bar's open.** A strategy sees
   bars up to and including the one just closed, and the order it produces is
   **priced at the next bar's open**. This removes look-ahead completely: the
   price the order gets was not knowable when the decision was made. It is
   *conservative relative to live*, which polls intra-day and can act within the
   bar — that gap is #13, and it is now a measurable difference rather than a
   hidden one.

   The fill is returned **synchronously**, on the cycle that submitted it, even
   though its price comes from the following bar (v1.47). Holding the order
   `SUBMITTED` until the cursor advanced was the first design and it was wrong:
   `execution.orders.open_position` treats an unfilled submission as an unknown
   outcome and raises `BrokerUnavailable`, so **every** entry would have gone
   through the crash-recovery path and every third one would have tripped the
   market-data outage counter. A backtest whose control flow differs from live
   on the ordinary path is the exact failure this rebuild exists to remove.

   What that costs is one bar of precision in `entry_at`, and therefore in the
   age `MAX_AGE` counts. It is named here rather than hidden because it is a
   real difference; it is the smaller of the two, and the alternative distorted
   the whole control flow to protect it.

   An order placed on the **last** bar of history has no next open, so it stays
   `SUBMITTED` and never fills. That is correct: history ran out, and inventing
   a price for it would be the look-ahead this rule exists to prevent.
2. **Stops are checked against the bar's low, take-profits against its high.**
   Checking the close, as the old code did, means a day that traded 8% down
   intraday and closed at −1% never triggers a 5% stop — while the exchange stop
   fires on the intraday print. Backtested stop-hit rates were systematically
   optimistic, which is the worst direction for them to be wrong in.
3. **A gapped open fills worse than the trigger.** A sell stop fills at
   `min(stop_price, bar.open)`; a take-profit at `max(target, bar.open)`. The
   exchange cannot fill at a price the market never traded at.
4. **When one bar touches both the stop and the target, the stop wins.** Daily
   bars cannot say which came first, and the pessimistic reading is the only one
   that cannot flatter the result.

**The simulator refuses what the broker would refuse (v1.48).** A market buy
whose turnover plus fee exceeds simulated cash raises `OrderRejected`, leaving
cash and holdings untouched — as `broker.client` does, and as `get_max_lots`
exists to make avoidable. It previously debited unconditionally, so cash went
negative and the next `get_portfolio()` raised `ValueError: cash must not be
negative` (#47). Beyond the crash: a simulator that funds any order cannot
demonstrate that the gate and sizing keep the bot solvent, which is one of the
things a backtest is for.

**Four cycles per bar, at the open, the low, the high and the close (v1.49).**
One cycle per bar made two of the system's controls structurally unreachable,
and neither absence was visible in a result:

- **The daily loss limit could never fire.** One bar is one cycle *and* one
  Moscow date, so step 4 wrote the day's opening snapshot and measured against
  it in the same instant; the intra-day loss was always zero (#53). On any run
  where the strategy would have breached the limit, live stops trading for the
  rest of the day and the backtest kept going — optimistic in exactly the
  scenario the limit exists for.
- **`MAX_AGE` could never fire.** `lifecycle.exits` requires
  `session.in_closing_window(now)`, the final fifteen minutes, and the single
  cycle sat at the session start. Measured: `max_holding_days=1` over twenty
  flat bars with stop and target 50% away produced **zero exits**.

The four instants are the session start, two marks a third and two thirds
through it, and one **inside the closing window**, which is what makes
`MAX_AGE` reachable. The day's opening snapshot is still written once, on the
first of the four, so the loss is measured against the open rather than against
the previous mark.

**The order is open, low, high, close** — the drawdown before the recovery.
That is the same pessimism as the stop-beats-target tie-break above, and for the
same reason: a daily bar cannot say which came first, and only the pessimistic
reading cannot flatter the result.

It also fixes a third thing that was never filed: `get_last_price` returned the
bar's **close**, so a `LOCAL` position's stop was checked against the close
only — the exact optimism the exchange-stop rule above removes, still present on
the path where the bot owns the stop itself.

**Commission is the broker's tariff, not a flat fee** — a percentage of turnover
with a minimum, applied per fill. The old flat figure was also applied twice to
one round trip.

**`async backtest.run(bars, config, strategies, commission, slippage) → BacktestResult`**
- **Drives `app.loops.trading_cycle` itself, four times per bar**, against a
  temporary database with the migrations applied and a `SimulatedExchange` in
  place of `broker.client`. Live and backtest cannot diverge, because they are the same
  code: the gate, the sizing, the exits, the cooldowns, the halt, the
  duplicate-ticker rule and the portfolio-exposure ceiling are all the live ones,
  reached the way live reaches them.
- This replaces a module that imported `strategies`, `risk.sizing` and
  `lifecycle.exits` but **not `risk.gate`** — obeying "never reimplement" while
  omitting the gate entirely, so that cooldowns, `max_open_positions`,
  duplicate-ticker rejection, halt and session state played no part in any
  result. An omission reads as compliance, which is why it survived (#12).
- Runs the **whole watchlist** with concurrent positions against one shared cash
  balance. One ticker and one position modelled away capital contention,
  correlation and portfolio drawdown — the three things a portfolio-level risk
  answer depends on.
- **Equity is marked to market on every bar.** `max_drawdown` came from the cash
  balance, appended only on trade events; cash *falls* when you buy, so the
  reported figure was approximately the position size and meant nothing.
- Returns trades, P&L, win rate, maximum drawdown, exit-trigger distribution and
  the buy-and-hold benchmark, as before.

**What this costs, stated plainly.** The simulator is a second implementation of
the *exchange*, and it can be wrong in ways that flatter or punish a strategy
without either being detectable from the result. It is not a substitute for the
verification suite against the live account: it answers "what would this strategy
have done", never "does the broker behave as we think". Those are different
questions and #44 is still the other one.

**`fit(candles_by_ticker: dict[str, list[Candle]], horizon_days: int, folds: int, seed: int) → FittedModel`**
- Trains a buy/no-buy classifier. The label is whether the take-profit level is
  reached before the stop level within `horizon_days`, so the model is trained on
  the question the live system actually asks it.
- `seed` is required and recorded in the export: an unreproducible model cannot
  be audited after a losing week.
- Uses **walk-forward** validation across `folds`; a single train/test split on a
  time series leaks the future into the past and is not acceptable.
- Returns the fitted model with its validation scores per fold. Reporting one
  averaged number hides a model that works in one regime and fails in another.

**`export(model: FittedModel, path: Path) → Path`**
- Writes a joblib bundle `{"model", "features", "seed", "trained_at"}` where
  `features` is `strategies.ml_model.FEATURE_NAMES` in order, which
  `strategies.ml_model.load` validates.

**Feature construction has one owner.** `strategies.ml_model.build_features`
builds the feature vector, and `sandbox.train` **imports it** rather than
rebuilding the same four features for training. This is the same rule as the
backtester importing the live strategies, for the same reason: features computed
one way at training and another way at inference produce a model that scores well
offline and behaves differently on real money, and nothing in the manifest check
would catch it — the names would still match.

---

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception.

## Expected output

- `sandbox/train.py` implementing the contract exactly
- `tests/test_sandbox_train.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_sandbox_train.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `sandbox/train.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
