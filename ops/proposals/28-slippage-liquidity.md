# Proposal: #28 slippage / liquidity — product, not an execution tweak

**Kind:** spec + brief. Money path. Do not auto-amend. Do not implement a
policy (keep vs unwind) in `execution.orders` first. Keep versus unwind is a
brief decision; unwinding is a second real trade. Do not invent "keep unless
X%" or a 5% drift rule.

Every entry is `ORDER_TYPE_MARKET`. The gate sizes from `signal.reference_price`
(last close; under #13 that may be an in-progress bar). The fill is accepted
if it filled. Stop/target are derived from the **fill**
(`execution/orders.py` from `order.filled_price`), so the **percentage** risk
is preserved; the **rouble** size is not what the gate approved. Brief §21
fixes 5%/10% of entry with no slippage clause.

Stops and bot exits should **stay market**. A limit stop that does not fill
in a falling market is worse than slippage.

**Options (human picks; recommend 1+2, not 3 yet):**

1. **Post-fill guard.** Compare `filled_price` to `reference_price`. Beyond
   a configured tolerance: alert. Whether to keep or immediately sell is a
   **stated policy**. Unwind is a second trade (cancel stop, sell, cooldown) —
   it needs the same write-then-send rules. Do not invent "keep unless X%".
2. **Pre-trade liquidity — split volume from spread.** They are not one
   remedy. New `RejectionReason` in `risk.gate` stays pure: any liquidity
   figure is an **input**, not a broker call inside the gate. Closest match
   to brief §20 excluding illiquid names (brief §8 already treats liquidity
   as watchlist selection).
   - **Volume is already in hand.** `Candle.volume: int` exists; the candles
     series is a local in `app/loops.py` (`candles_for_watchlist` / `series`)
     about forty lines above `gate.check`. A volume-based input needs no new
     broker method, no verification script, and `SimulatedExchange.get_candles`
     already serves it.
   - **Spread has no producer.** `broker.client` has no orderbook method
     (`order_book` / `orderbook` / `spread` are absent from `zarabot/`,
     `sandbox/`, `scripts/`, and both documents). Spread needs a new SDK
     surface, a `scripts/verify/` script (assumed provider behaviour), and a
     `SimulatedExchange` answer, because `sandbox/backtest.py` runs
     `app.loops.trading_cycle` against the simulator. A gate input the
     simulator cannot produce either breaks the backtest or forces a
     divergent stub.
3. **Limit orders.** Changes recovery (`SUBMITTING` / unfilled rest). Spec
   `execution.orders` first. Not this amendment unless the brief wants it.

**Must NEVER stays:** `confirm_margin_trade=True` is still never passed.
Never estimate commission. Never resubmit an entry.

**§3.2 after numbers exist:**

- Fill beyond tolerance → the chosen event/alert (and keep or unwind as
  specified).
- Ticker failing the liquidity inputs → rejected with the named reason.
- Existing fill/recovery tests stay green.

**Modules, after the brief:** `config` (tolerance / thresholds). `risk.gate`
for a liquidity input. `gate.check` is reachable from `app/loops.py`,
`scripts/diagnose/entry_funnel.py` (real gate, dry run), and
`sandbox/backtest.py` via `trading_cycle` — not a single call site. Volume
reuses candles already at those seams. Spread additionally prices
`broker.client`, `scripts/verify/`, and `sandbox.exchange`. `execution.orders`
only for the post-fill alert/unwind path.

**Stop:** do not unwind a fill in this module because a test fixture showed
5% drift. That is a new trade.
