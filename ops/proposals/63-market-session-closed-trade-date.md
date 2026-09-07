# Proposal: #63 `session_closed.trade_date` cannot come from `SessionInfo.start`

**Kind:** spec amendment in `market.session` (v1.61 event fields). Hold [#85](https://github.com/imitusov/bazarabot/pull/85) until this lands.

**Wrong lines** (`technical-spec.md` §4 `refresh`, §7.1 table):

> `session_open` when the first day of the fetched window is a trading session, with `trade_date`, `opens_at`, `closes_at` from that `SessionInfo`.
> `session_closed` when that day is not a trading session, with the same three fields (`opens_at` / `closes_at` may be null).

§7.1: a record missing a required field is a defect. The table lists `trade_date` as required for both events.

**Why it is impossible as written.** `broker.client` maps a non-trading day to `SessionInfo(start=None, end=None, is_trading_day=False)`. There is no date on that object. Deriving `trade_date` from `start` (what #85 does) logs every production `session_closed` as:

```json
{"event": "session_closed", "trade_date": null, "opens_at": null, "closes_at": null}
```

The date the operator needs is missing on every instance of the event. A closed-day fixture that still carries `start`/`end` (the current #85 holiday test) is a shape `get_trading_schedule` never returns — failure class 9.

This is not F-51. F-51 is `calendar()` collapsing undated closed days. This event reads `fetched[0]` and never goes through `calendar()`. Giving `SessionInfo` a `trade_date` field (#51) would also fix this, but O-09 should not wait on a models change.

**Should say** (one derivation, no new field):

- `trade_date` is the Moscow calendar date of `SessionInfo.start` when `start` is set.
- When `start` is `None` — every non-trading day the broker actually returns — `trade_date` is `clock.moscow_date(clock.now())`. The schedule window is anchored at today's midnight (§2.1); the first day of a successful fetch with no timestamps is therefore today in Moscow.
- `opens_at` / `closes_at` stay null on that path. They may be null; `trade_date` may not.

**Do not:** infer a date from a later trading day in the window; fabricate session times for a closed day; call `datetime.now()` outside `clock`.

**§3.2 cases that would have caught it:**

- A successful refresh whose first day is `SessionInfo(start=None, end=None, is_trading_day=False)` and whose window still contains a trading day emits `session_closed` with `trade_date` equal to `clock.moscow_date(clock.now())` and `opens_at` / `closes_at` null. Freeze `clock.now` so the assertion is not wall-clock.
- A successful refresh whose first day is a trading session still emits `session_open` with `trade_date` from `moscow_date(start)` (unchanged).
- An unavailable fetch still emits neither event.

**Modules to re-run:** `22-market-session` only (#85 rebases). Do not change `broker.client` or `models` in that PR.

**Stop:** do not implement the `clock.now()` fallback in #85 until this sentence is in the spec. The implementer must not choose between "today" and "#51's field" unaided.
