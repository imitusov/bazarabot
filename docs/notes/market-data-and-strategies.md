# Notes: market data polling, the trading cycle, and how strategies fire

Session handoff notes, written 2026-09-08. Derived entirely from
`business-brief.md`, `technical-spec.md`, `interfaces.md` and the current
implementation — nothing here is a new decision, and where this file and those
documents disagree, **they win**. Descriptive only; no contract lives here.

---

## 1. Where the poller sits

Per the diagram at `business-brief.md:83` and §13, the poller is the first thing
after the session guard and the input to every strategy:

```
Session guard → Market data poller → Strategy engine → Risk gate → Order executor → Position monitor
```

**Cadence:** every minute, main MOEX session only. `POLL_INTERVAL_SECONDS`
(default 60, `business-brief.md:770`) is deliberately set below the *measured*
broker rate limit, not a documented or guessed one — `technical-spec.md:176`
describes the verification script that measures the real limit across the full
watchlist at the configured interval.

In `app.loops.trading_cycle` (`technical-spec.md:3328`) candle fetching is
**step 6**, after:

1. session-closed early return — no broker call at all;
2. price refresh for open positions + stop-fill polling;
3. exits (take-profit, max age, and stop-loss only for `LOCAL`-protected
   positions), submitted **before** any halt check and before entries;
4. daily P&L recompute + halt on the daily loss limit;
4b. shutdown-requested return — entries stop, exits do not (v1.45);
5. halted return — entries stop here.

So a halted, shutting-down, or out-of-session bot never fetches candles.

**Backoff.** On a rate-limit response the bot backs off exponentially; sustained
rate limiting is a fault and is alerted. Transient broker errors retry with
exponential backoff; after three consecutive failures the cycle is skipped, the
owner alerted **once** (not per retry), and the bot stays alive for the next
cycle rather than exiting. That supervision lives in the surrounding cycle
(`_next_delay`, `_note_data_failure`), not in the fetch function.

---

## 2. `market.data` contract

`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) -> dict[str, list[Candle]]`
(`technical-spec.md:2630`, `interfaces.md:775`)

### Input
- `tickers` — the fixed configured watchlist of liquid MOEX shares.
- `lookback` — the **longest** lookback among enabled strategies; one fetch
  serves all of them.
- `now` — from `clock`, timezone-aware; naive raises `ValueError`.

### Output
Per-ticker **daily** candle series, **oldest-first**, timezone-aware timestamps.
Never pads, never interpolates. A ticker is absent only when its fetch
*failed*. Built on `broker.client.get_candles(figi, interval, since, until)`,
which is oldest-first, returns `[]` for a range with no trading activity, and
raises `ValueError` on naive datetimes.

### Logic

1. **Fetch per ticker.**

2. **Failure containment.** Catches only `BrokerUnavailable`,
   `BrokerRateLimited`, `InstrumentNotFound` → omit that ticker, log WARNING,
   **the batch still returns**; one unavailable instrument must never blind the
   bot to the rest. **Every other exception propagates** — an `AttributeError`
   from a renamed SDK field or a `ValueError` from a malformed candle reaches
   `app.loops._supervise`, which alerts with a traceback and restarts under
   rule 21. Since v1.27 `broker.client` raises those as themselves; catching
   `Exception` here would put them straight back in the dark.

3. **"Degraded" — two cases on ONE shared counter.** A call is degraded for a
   ticker when the fetch failed, **or** when it succeeded with fewer than
   `lookback` candles (zero included). The second is not a lesser case of the
   first: an empty success looks like health to every counter while guaranteeing
   the ticker is skipped by `_evaluate_entries`, or evaluated to `None` by every
   strategy, silently, on every cycle. One shared counter is mandatory —
   separate counters would let a ticker alternate between failure and shortfall
   forever without crossing any threshold.

4. **A short series is still returned.** Reporting insufficiency must not become
   dropping the ticker: `lookback` is the longest *enabled* lookback, so a
   series too short for that may still satisfy a shorter strategy, and the
   caller decides. Only a failed fetch omits a ticker from the result.

5. **Latched alert (rule 9, shaped by rule 36).** On the **third consecutive**
   degraded call for a ticker, alert the owner **once**, naming the ticker and
   the reason — the failure, or candles returned against candles required.
   Nothing further for that ticker until a call is not degraded, which clears
   both its count and its alerted flag, so a later degradation alerts again.
   Tickers crossing the threshold in the same call share one alert.

6. **Structured event** `candles_failed` (WARNING, fields `ticker`, `error`)
   whenever a ticker is omitted or counted degraded; `error` is the exception
   type name or `short_history` (v1.61). Independent of the Telegram alert.

7. **Process-local state** — `_failures` and `_alerted`, like
   `market.session`'s cache: they measure *this* process's consecutive
   failures, and a restart is entitled to start over rather than inherit a
   count it never observed. `sandbox.backtest` clears them between runs.

---

## 3. "Every 60 seconds" — three qualifications

1. **Main session only.** `trading_cycle` step 1 returns immediately when the
   session is closed, with no broker call. Roughly 510 cycles in an 8.5-hour
   session, not 1440 a day.

2. **A cycle interval, not a single call.** Each tick makes several distinct
   broker calls: `get_last_price` per open position; `get_executed_stop_fills`
   once over the window from the start of the Moscow trading day to now; exit
   submissions if any fire; `get_portfolio`; then one `get_candles` per
   watchlist ticker. Per-minute request count therefore scales with watchlist
   size plus open positions — which is exactly why the rate limit is measured
   across the full watchlist rather than trusted from documentation.

3. **60s is a default, not a constant.** `POLL_INTERVAL_SECONDS` must stay below
   the measured limit; if the watchlist grows, the interval is what gives.

**The candles are daily.** Polling every minute does not give minute-resolution
signals — it re-fetches a daily series whose latest bar is still forming. The
minute cadence exists for the *exit* side: take-profit and max age are evaluated
every cycle (`business-brief.md:344`), while the stop-loss is watched
continuously by the exchange.

---

## 4. Worked example of one cycle

Watchlist `["SBER", "GAZP", "LKOH"]`, one open position in SBER, cycle at
2026-09-07 13:45 MSK. Types are from `zarabot/models.py`.

### Step 1 — session (`market.session`)
```python
SessionInfo(start=datetime(2026,9,7,6,50, tzinfo=UTC),
            end=datetime(2026,9,7,15,40, tzinfo=UTC),
            is_trading_day=True)
```
Closed → return, no broker calls at all.

### Step 2 — prices for open positions
```python
{"SBER": Decimal("312.45")}      # or PriceRejected for a stale/zero/jumpy quote
```
A `PriceRejected` omits that ticker and the cycle continues; it does **not**
count toward the outage alert, and it latches — one alert per cycle that
rejects anything, naming the count, then quiet until a cycle rejects nothing.

### Step 2b — stop fills
`get_executed_stop_fills(since=day_start_msk, until=now) -> dict[str, OrderRecord]`
keyed by the broker's `stop_order_id`:
```python
{}   # nothing fired — the usual case
```
A fired stop:
```python
{"si-88f1": OrderRecord(key="…", ticker="SBER", figi="BBG004730N88", side=Side.SELL,
                        intent="STOP", lots=1, status=OrderStatus.FILLED,
                        filled_lots=1, filled_price=Decimal("296.80"),
                        commission=Decimal("1.48"), broker_reason=None,
                        created_at=…, settled_at=…,
                        exit_trigger=ExitTrigger.STOP_LOSS,
                        broker_order_id="ex-4412")}
```
A position closes **only** when this dict contains the `stop_order_id`
persisted in its own `stop_orders` row — never inferred from the stop being
absent from the active list. An absence is a discrepancy, not an exit: the
position stays open and is alerted once.

### Step 4 — portfolio
```python
PortfolioState(
  cash=Decimal("41230.00"),
  positions=(Position(id=17, ticker="SBER", figi="BBG004730N88",
                      strategy="ma_crossover", lots=1, lot_size=10,
                      entry_price=Decimal("305.00"),
                      entry_at=datetime(2026,9,5,7,12, tzinfo=UTC),
                      stop_price=Decimal("289.75"),      # −5%
                      target_price=Decimal("335.50"),    # +10%
                      status="OPEN", adopted=False,
                      open_order_key="…", close_order_key=None,
                      exit_trigger=None, exit_price=None, exit_at=None,
                      realised_pnl=None,
                      stop_protection=StopProtection.EXCHANGE,
                      stop_order_key="…"),))
```

### Step 6 — candles
`candles_for_watchlist(["SBER","GAZP","LKOH"], lookback=50, now)`:
```python
{
  "SBER": [Candle(timestamp=datetime(2026,6,30, tzinfo=UTC), open=Decimal("288.10"),
                  high=Decimal("291.40"), low=Decimal("287.05"),
                  close=Decimal("290.20"), volume=18_442_100),
           ...,                                    # 50 daily bars, oldest-first
           Candle(timestamp=datetime(2026,9,7, tzinfo=UTC), open=Decimal("310.00"),
                  high=Decimal("313.20"), low=Decimal("309.15"),
                  close=Decimal("312.45"), volume=4_210_300)],   # today, still forming
  "GAZP": [ ... 31 candles ... ],   # short — returned anyway, degraded, counter +1
  # "LKOH" absent — BrokerUnavailable, omitted, candles_failed WARNING, counter +1
}
```
SBER healthy; GAZP returned 31 < 50 so it counts as degraded but is still handed
to strategies (one with `lookback=20` can use it); LKOH simply missing.

### Step 6b — strategy output
```python
Signal(ticker="GAZP", strategy="rsi_reversion", side=Side.BUY,
       generated_at=datetime(2026,9,7,10,45, tzinfo=UTC),
       reference_price=Decimal("142.30"))
```
`None` from the strategies that saw nothing, and from any whose own lookback
exceeds 31.

### Step 6c — gate output
```python
RiskDecision(approved=True, lots=2, reason=None)
# or: RiskDecision(approved=False, lots=None, reason=RejectionReason.COOLDOWN_ACTIVE)
```

### Logs from this cycle
```
candles_failed   ticker=LKOH  error=BrokerUnavailable
candles_failed   ticker=GAZP  error=short_history
signal_generated ticker=GAZP strategy=rsi_reversion reference_price=142.30
```
No Telegram alert yet — each ticker is on its **first** consecutive degradation.
If both shapes repeat on the next two cycles, cycle three sends **one** alert
naming both tickers with their reasons ("BrokerUnavailable", and "31 candles,
50 required"), then goes quiet for them until a call comes back clean.

---

## 5. How a strategy "fires"

A strategy does not know anything. It is a **pure function asked every cycle**,
answering `Signal` or `None`. No event, no subscription, no state between calls.
The firing condition is entirely a property of the series just handed to it.

`app.loops._evaluate_entries` (`zarabot/app/loops.py:395`) loops strategies
outer, tickers inner:

```python
signal = strategy.evaluate(ticker, series, moment)
if signal is None:
    continue
```

`evaluate` is re-run from scratch on the same ~50-bar daily series every 60
seconds. Nothing remembers that it fired last minute. Determinism is part of the
protocol contract: identical inputs produce identical outputs.

### Guards every implementation applies first
1. `len(candles) < self.lookback` → `None`.
2. `len(set(closes)) == 1` (flat series) → `None` — degenerate input returns
   `None` rather than raising.

### What each one tests

| Strategy | `lookback` | Fires when |
|---|---|---|
| `ma_crossover` | 31 (`_SLOW`+1) | **A crossing, not a position**: `fast_prev <= slow_prev and fast_now > slow_now`, SMA(10) vs SMA(30). Both SMAs are computed on the full series and again on `closes[:-1]`, so this bar's relationship is compared against the previous bar's. |
| `rsi_reversion` | 15 (`_PERIOD`+1) | RSI(14) `< 30` — a **level**, not a crossing. `None` when `avg_gain == avg_loss == 0`; RSI is 100 when `avg_loss == 0`. |
| `momentum` | 21 (`_LOOKBACK_BARS`+1) | `last.close > max(high of the prior 20 bars)` — close against prior **highs**, with the last bar excluded from its own comparison window. |
| `ml_model` | model's own | `predict_proba(...)[1] >= CONFIDENCE_THRESHOLD`, a module constant rather than an env var: the threshold is a property of the trained model and travels with the code. There is deliberately no `ML_CONFIDENCE_THRESHOLD`. |

Every one returns `Side.BUY` or `None`. **A strategy can never return `SELL`** —
strategies enter, `lifecycle.exits` exits.

### Two consequences of stateless re-evaluation

- **`ma_crossover` self-limits; the other two do not.** A crossover is true for
  exactly one bar, so it naturally fires once per day. But RSI below 30, or a
  close above the prior high, stays true all session — `rsi_reversion` will
  return a `Signal` on all ~510 cycles of that day. What prevents repeat entries
  is not the strategy: it is `risk.gate`'s duplicate check against
  `state.positions`, the cooldown, and `opened_this_pass` for the within-pass
  case (`DUPLICATE_TICKER`, v1.40 — the broker's portfolio lags a market order
  that has only just filled, so the local set is the immediately-consistent
  record, and it lives in `app.loops` because `risk.gate` stays pure).
- **The last bar is today's, still forming.** All four read `closes[-1]` as
  `reference_price`, and that bar's close is the current price, moving all day.
  The same strategy on the same day can flip from `None` to `Signal` and back as
  the day's close drifts. Expected, not a bug.

### After it fires
A `Signal` is not an order. It goes: `signal_generated` log → duplicate-ticker
check → `get_instrument` → cooldown check → `risk.gate.check` → `RiskDecision`.
Every signal is recorded with its decision, approved or rejected, so a strategy
that fires constantly and is constantly blocked by cooldown shows up in the
weekly report — which the brief calls out as worth knowing.
