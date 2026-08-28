# Task 22/40: Implement `zarabot/market/session.py`

## Product context

Is the exchange open? Queried from the broker calendar, never hardcoded. Defaults to closed when unknown.

## Build order position

Module **22** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/market/session.py`

**`async refresh(days: int) → None`**
- Caches the schedule. Called at startup and once per trading day.
- **The unavailability latch is cleared on the success path**, next to the cache
  write (v1.40). It was set on the first failure and never cleared, so a schedule
  that went unavailable, recovered, and went unavailable again produced silence
  from this module for the rest of the process lifetime (#32). Rule 10 says
  "alert once" without saying once per incident or once per process; `app.loops`
  resets its equivalent latch on a successful refresh, and that asymmetry is what
  settles the reading — **once per incident**.
- **A response containing no trading sessions is treated as unavailable**, per
  error rule 10: the cache is left as it was, and the owner is alerted once. It
  must never replace a populated cache with an empty one, and it must never
  report success having stored nothing — a schedule fetch that "succeeds" with
  no sessions leaves the bot unable to trade with nothing raised.
- Does not raise on an unavailable schedule. Aborting startup over a transient
  broker blip is worse than starting and reporting the condition, which
  `cache_exhausted` then keeps visible on every cycle until it is fixed.

**`is_open(now: datetime) → bool`**
- True when `now` falls within a main session, inclusive of the open instant and
  exclusive of the close instant.
- Returns `False` when the schedule is unavailable — the safe default is not to
  trade.

**`cache_exhausted(now: datetime) → bool`**
- True when `now` is at or past the last cached session, meaning `is_open` is
  returning `False` because the bot has run out of calendar rather than because
  the market is shut.
- **True when the cache is empty.** An empty cache is the strongest form of
  having run out of calendar: there is no calendar at all. Reporting `False`
  there is the failure this function exists to detect, in its worst form — not a
  cache that expired, but one that never filled, with `is_open` false every
  cycle, nothing raised, and the heartbeat still reporting health.
- This relies on `app.startup` step 5 refreshing the schedule **before**
  `app.loops.run()` starts the trading cycle. Without that ordering an empty
  cache would be the normal state for the first moments of a run and this
  function would alert on every start. The ordering is contractual, not
  incidental; a change that moves the first refresh after `run()` must revisit
  this contract.
- These two states are indistinguishable from `is_open` alone, and conflating
  them is how a bot stops trading silently: every cycle returns "closed", no
  error is raised, and the heartbeat keeps reporting health. `app.loops` checks
  this and alerts.

**Refresh cadence.** The schedule is refreshed at startup **and at every daily
rollover**, always fetching a horizon longer than the gap between refreshes. A
cache filled once at startup expires while the process is still running, which
is the failure this cadence exists to prevent.

**`current_session(now: datetime) → SessionInfo | None`**

**`in_closing_window(now: datetime, minutes: int) → bool`** — true during the final `minutes` of the current session; used only by the maximum-age exit.

**`calendar() → TradingCalendar`**
- The cached schedule as a `TradingCalendar`, for callers that need to count
  trading days rather than ask whether a moment is inside a session. Empty
  calendar when the cache is empty; never `None`.
- **It spans forwards only, and that is why `MAX_AGE` still cannot fire (#45).**
  v1.41 proposed a second, backward fetch; it was implemented, deployed, and
  aborted startup — the broker rejects *any* `from_` before today's midnight
  with `INVALID_ARGUMENT` / 30003 (§2.1). It was reverted. The remaining
  approach is to persist what each forward fetch already tells us: the window
  fetched on day N covers days N through N+14, so the union of past fetches
  covers the span any position can be open. That needs somewhere durable to put
  it, which is a decision not yet taken.
- Added in v1.40 so `app.loops` stops fetching a fourteen-day schedule **once a
  minute** for data that changes at most daily and that this module already
  holds (#19). `_schedule_refresh_loop` refreshes this cache once per Moscow
  day; that is the only fetch there should ever be.
- It also removes a silent failure the caller had no way to see: `app.loops`
  returned an *empty* calendar on a broker error, which made
  `clock.trading_days_between` count zero and disabled `MAX_AGE` exits with no
  alert. Reading the cache cannot produce that state — an unavailable schedule
  leaves the cache as it was and alerts under rule 10, and an empty cache makes
  `is_open` false, so the cycle never reaches the exit step at all.
- **This does not fix #45.** The cached window is the same forward-looking one,
  anchored to the start of the current UTC day, so counting trading days
  *backwards* from a position's entry still finds nothing before today. That is a
  separate defect in what the calendar spans, not in how often it is fetched.

**`next_open(now: datetime) → datetime`** — used by the loop to sleep rather than poll.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

10. **Trading schedule unavailable, or returned with no trading sessions** →
    treat the market as closed, WARNING, alert once, and leave any existing
    cache intact. The safe default is not to trade. An empty result is a form of
    unavailable, not a valid schedule: treating it as success stores a cache
    that makes `is_open` false forever with no error anywhere.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A refresh that fails, then succeeds, then fails again alerts **twice** (proves
  the latch is per incident: it was set once and never cleared, so every outage
  after the first was silent from this module for the life of the process).
- `calendar()` returns the cached sessions as a `TradingCalendar`, and an empty
  one when the cache is empty rather than `None` (proves the caller has a total
  answer and never has to fetch its own).
- A timestamp inside the main session reports open (happy path).
- Exactly at the session open instant reports open; exactly at the close instant
  reports closed (boundary, proves inclusivity at both ends).
- A Saturday, and a scheduled market holiday, report closed (proves the calendar
  is consulted, not the weekday).
- With the schedule unavailable, reports closed and raises no exception (proves
  the safe default is to not trade).
- With `now` past the last cached session, `is_open` is False **and**
  `cache_exhausted` is True (proves an exhausted calendar is distinguishable
  from a closed market — the difference between a bot resting and a bot that
  has silently stopped trading).
- With an **empty** cache, `cache_exhausted` is True (proves the worst case is
  detected: a cache that never filled, not one that expired).
- `refresh` receiving a schedule with no trading sessions leaves a previously
  populated cache intact and alerts (proves an empty response cannot overwrite
  a good calendar).
- After a rollover refresh, `cache_exhausted` is False again (proves the cadence
  actually reloads).

## Expected output

- `zarabot/market/session.py` implementing the contract exactly
- `tests/test_market_session.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_market_session.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/market/session.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
