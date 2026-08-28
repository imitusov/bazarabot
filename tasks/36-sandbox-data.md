# Task 36/40: Implement `sandbox/data.py`

## Product context

Historical candle loading for research. Laptop only.

## Build order position

Module **36** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

- **`SimulatedExchange(bars, instruments, cash, slippage, commission)`** holds
  simulated cash, holdings, submitted orders and standing stop orders, and a
  cursor into the bars. `advance(moment)` moves the cursor and settles anything
  the newly-visible bar triggers.
- It exposes `get_candles`, `get_last_price`, `get_instrument`, `get_portfolio`,
  `get_max_lots`, `get_order_state`, `post_market_order`, `post_stop_loss`,
  `cancel_stop_order`, `cancel_order`, `list_stop_orders`,
  `get_executed_stop_fills` and `get_trading_schedule` with the signatures and
  the failure types `interfaces.md` records for the real ones. Where the real
  module raises, this raises the same exception.
- **`market.session` is driven, not stubbed.** The simulator answers
  `get_trading_schedule`, and the real `refresh` / `is_open` / `calendar` /
  `covers` run on top. A backtest that stubbed those would not exercise the
  code that decides whether the market is open, which is where #39 and #43
  lived.

**Fill model.** The four rules below are where a backtest is honest or is not:

1. **Decide at a bar's close, fill at the next bar's open.** A strategy sees
   bars up to and including the one just closed, and any order it produces fills
   on the next. This removes look-ahead completely. It is *conservative relative
   to live*, which polls intra-day and can act within the bar — that gap is #13,
   and it is now a measurable difference rather than a hidden one.
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

**Commission is the broker's tariff, not a flat fee** — a percentage of turnover
with a minimum, applied per fill. The old flat figure was also applied twice to
one round trip.

**`async backtest.run(bars, config, strategies, commission, slippage) → BacktestResult`**
- **Drives `app.loops.trading_cycle` itself**, once per bar, against a temporary
  database with the migrations applied and a `SimulatedExchange` in place of
  `broker.client`. Live and backtest cannot diverge, because they are the same
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

- `sandbox/data.py` implementing the contract exactly
- `tests/test_sandbox_data.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_sandbox_data.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `sandbox/data.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
