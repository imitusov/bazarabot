# Task 23/42: Implement `zarabot/market/data.py`

## Product context

Candles for the watchlist. One failing instrument must never blind the bot to the rest, and one that fails persistently must never do so in silence.

## Build order position

Module **23** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/market/data.py`

**`async candles_for_watchlist(tickers: list[str], lookback: int, now: datetime) → dict[str, list[Candle]]`**
- Returns per-ticker candle series, oldest-first.
- A ticker whose fetch fails with a **broker** failure is omitted from the
  result and logged at WARNING; the batch still returns. One unavailable
  instrument must never blind the bot to the rest.
- **A call is degraded for a ticker when the fetch fails, or when it succeeds
  with fewer than `lookback` candles** — no candles at all included. The second
  half is not a lesser case of the first: a fetch that returns nothing looks
  like success to every counter, and the ticker is then skipped by
  `app.loops._evaluate_entries`, or evaluated to `None` by every strategy whose
  lookback exceeds what came back, on every cycle, in silence. That is the same
  blindness #23 names, one layer downstream of it.
- **Consecutive degraded calls are counted per ticker, and a persistent one
  alerts.** On the third consecutive degraded call for a ticker the owner is
  alerted once, naming the ticker and the reason — the failure, or the candle
  count against the count required; nothing further is sent for that ticker
  until a call is not degraded. A good call clears both its count and its
  alerted flag, so a later degradation alerts again. Tickers crossing the
  threshold in the same call share one alert. **A failure and a shortfall share
  one counter**, or a ticker alternating between them would never cross a
  threshold at all. This is rule 9, and rule 36 is the shape it belongs to.
- **A short series is still returned.** Reporting insufficiency must not become
  dropping the ticker: `lookback` is the longest lookback among the enabled
  strategies, so a series too short for that one may still satisfy a shorter
  one, and the caller decides. Only a failed fetch omits a ticker from the
  result.
- **Only the broker's own failures are caught** — `BrokerUnavailable`,
  `BrokerRateLimited` and `InstrumentNotFound`. Every other exception
  propagates: an `AttributeError` from a renamed SDK field or a `ValueError`
  from a malformed candle reaches `app.loops._supervise`, which alerts with a
  traceback and restarts under rule 21. Since v1.27 `broker.client` raises those
  as themselves rather than as `BrokerUnavailable`; catching `Exception` here
  put them straight back in the dark, which is the second half of #23.
- Never pads or interpolates missing candles.
- **Emits `candles_failed` (WARNING) with `ticker` and `error` whenever a ticker
  is omitted or counted as degraded (v1.61).** `error` is the exception type
  name, or `short_history`. This is the structured event; the Telegram alert on
  the third consecutive degradation is unchanged.
- The counters are process-local, like `market.session`'s cache: they measure
  consecutive failures of *this* process, and a restart is entitled to start
  over rather than inherit a count it did not observe.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

9. **Candle fetch fails for one ticker, or returns too little history** → omit a
   failed ticker, WARNING, continue the batch. Both are **degraded calls** and
   both count on **one** per-ticker consecutive counter: on the **third**
   consecutive degraded call, alert **once**, naming the ticker and the reason —
   the failure, or the candles returned against the candles required — and send
   nothing further for it until a call is not degraded. A good call clears both
   the count and the alerted flag. Tickers crossing the threshold in the same
   call share one alert. Only `BrokerUnavailable`, `BrokerRateLimited` and
   `InstrumentNotFound` are handled as failures; every other exception
   propagates under rule 21.

   A ticker that fails forever is a delisting, a rename or a wrong class code —
   not weather — and before this rule it was dropped from every batch in
   silence, so the watchlist could shrink to nothing while the bot reported
   itself healthy (#23). **A fetch that succeeds and returns nothing is the same
   blindness wearing the opposite disguise**: it looks like success to every
   counter, while the ticker is skipped or evaluated to `None` on every cycle.
   The two must share a counter, or a ticker alternating between them crosses no
   threshold ever.
9b. **A quote is rejected as non-positive, stale, or an implausible move** →
    WARNING, omit that instrument for the cycle, alert once per cycle with the
    count. It is **not** a broker outage: it must not increment the consecutive
    failure counter of rule 1, and it must not be retried, because the next
    reading arrives on the next cycle anyway. Treating bad data as an outage is
    how a malformed field becomes an alert about the network.

36. **A degraded state that persists must alert; only a transient one may be
    logged.** Wherever this system absorbs a failure — a retry, a skipped
    instrument, a partial result — the absorption needs three things *together*:
    a threshold at which continuing to absorb stops being reasonable, exactly
    one alert when it is crossed, and a reset on recovery that re-arms that
    alert. Rules 1, 2 and 9 are the instances; the shape recurs wherever a loop
    tolerates a failure it cannot fix.

    Any two of the three are not enough, and each missing part has already cost
    this project an issue. With no threshold the log is the only record and
    nobody reads it, which is how a permanently broken ticker was dropped from
    every batch for as long as it took to notice (#23). With no reset the second
    incident is silent, which is #32 and #48 — the same defect written twice,
    hours apart, in two modules. With no single alert the channel fills and
    stops being read, which is the "commission still unknown" alert that fired
    on every backfill run forever (#8).

    Whenever a latch is added, list its set-sites against its reset-sites and
    look for the asymmetry. That check is mechanical, it takes a minute, and it
    is the only thing that has ever caught this class.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Candles for a watchlist ticker are returned newest-last, timezone-aware
  (happy path and ordering contract).
- Fewer candles available than the longest strategy lookback returns what exists
  and the caller can detect insufficiency (proves partial history is visible, not
  silently padded).
- A ticker returning **no** candles is counted as a degraded call and alerts on
  the third, exactly as a failed fetch does (proves an empty success is not
  mistaken for health — it is the one result that guarantees no strategy can
  evaluate the ticker).
- A ticker returning fewer candles than the requested `lookback` alerts on the
  third consecutive call, and the alert names both numbers (proves a short
  history is reported rather than tolerated forever in silence).
- The short series is still returned to the caller (proves reporting
  insufficiency did not become dropping the ticker: a strategy whose own
  lookback the series does satisfy must still see it).
- Two failed calls followed by a short one alert on the third (proves a failure
  and a shortfall share **one** counter — they are one degradation of one
  ticker, and counting them separately would let a ticker alternate between
  them forever without ever crossing a threshold).
- A ticker whose fetch raises a **broker** failure while others succeed does not
  fail the batch (proves one bad instrument cannot blind the bot to the rest).
- A ticker failing three consecutive calls alerts exactly once, and a fourth
  failure adds no second alert (proves the threshold and the latch together).
- A ticker that recovers and then fails three more times alerts a **second**
  time (proves recovery re-arms the latch; an alert that fires once per process
  and never again is what #32 and #48 both are).
- Two tickers crossing the threshold in the same call produce **one** alert
  naming both (proves the call, not the ticker, is the unit — a watchlist-wide
  outage must not send one message per instrument).
- A ticker whose fetch raises a non-broker exception propagates it rather than
  omitting the ticker (proves a programming error is not disguised as a missing
  instrument, which is the exposure `broker.client`'s narrowing exists to
  create and this module was swallowing).
- An omitted ticker emits `candles_failed` with that `ticker` and `error`
  (v1.61).

**`strategies.*`**

For every strategy, independently:
- A price series designed to produce an entry returns a `Signal` naming the
  strategy and the ticker (happy path).
- A price series with no setup returns `None` (proves the nullable contract).
- A series shorter than the strategy's lookback returns `None` and does not raise
  (proves insufficient history is a non-event, not a crash).
- A series containing a flat price run returns `None` rather than dividing by
  zero (proves the degenerate-input path).
- The same input evaluated twice returns equal results (proves purity and
  determinism).
- No strategy returns a `SELL` signal under any input (proves the entry-only
  contract that the whole exit design rests on).

Additionally, `strategies.ml_model`:
- `build_features` returns values in `FEATURE_NAMES` order, and raises
  `ValueError` on fewer candles than `lookback` (proves the shared contract that
  training depends on).
- With `ML_MODEL_PATH` unset, the strategy is absent from the registry (proves
  disabled-by-default).
- A missing or unreadable model file raises `ModelLoadError` at startup, not at
  first signal (proves failure is loud and early).
- A model whose feature contract does not match the expected names and order
  raises `ModelContractError` (proves a stale model cannot silently mispredict).
- A prediction below the confidence threshold returns `None` (boundary).

## Expected output

- `zarabot/market/data.py` implementing the contract exactly
- `tests/test_market_data.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_market_data.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/market/data.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
