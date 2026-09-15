# Task 21/43: Implement `zarabot/broker/client.py`

## Product context

The ONLY module that talks to the broker. Wraps t_tech.invest.AsyncClient and returns domain types, and from spec v1.85 it is also the sole writer and sole reader of the instruments cache. Run the verification suite before building this.

## Build order position

Module **21** of 43 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `instruments`

Owner: `broker.client` — **sole writer and sole reader (v1.85)**. The v1.77 open
decision is settled: the cache is built (#46, #102). §4's `broker.client` contract
carries the freshness window, the field split, the miss policy and the rule-12
write; no other module writes this table and no other module reads it.

**No migration is required.** `001_initial.sql` creates the table with exactly the
seven columns below and nothing else, and this contract needs no eighth: the
window is measured on `refreshed_at`, which the table already has and which
`Instrument` has carried, unread, since the first version (failure class 13).

| Column | Type | Notes |
|---|---|---|
| `figi` | TEXT | Primary key |
| `ticker` | TEXT NOT NULL | Unique. The lookup key — `get_instrument` takes a ticker |
| `lot` | INTEGER NOT NULL | Served while the row is fresh |
| `min_price_increment` | TEXT NOT NULL | Served while the row is fresh. Decimal string |
| `currency` | TEXT NOT NULL | CHECK = `RUB`. Served while the row is fresh |
| `trading_status` | TEXT NOT NULL | **Recorded, never served** |
| `refreshed_at` | TEXT NOT NULL | When the four columns above it were read. Defines staleness |

**`trading_status` is recorded and never served.** Every write stores the status
the broker reported at that instant, because the column is `NOT NULL` and because
the row is then a truthful record of one read an operator can query. No code path
returns it to a caller: §4 requires a live status on every `get_instrument` call,
whatever the row's age, because `risk.gate` rejects `INSTRUMENT_NOT_TRADING` on
that field and a stored value would let the bot size an entry into a halt. Reading
this column into a trading decision is a defect, not an optimisation.

**A row is never deleted and never invalidated by hand.** Staleness is a
comparison against `refreshed_at`, so the correction for a wrong lot size is the
next read past the window, which rewrites the row. Nothing else writes it, so
nothing else has to be kept consistent with it.

## Module contract

### `zarabot/broker/client.py`

The **only** module that calls the broker. Wraps `t_tech.invest.AsyncClient` and
returns domain types, never SDK types. The SDK's own services — `OrdersService`,
`MarketDataService`, `InstrumentsService`, `OperationsService`, `SandboxService`
— are reachable only from inside this module.

**Sole owner of the `instruments` table (v1.85).** This module is the only writer
and the only reader of that table. The owner settled the v1.77 decision on
2026-09-12: the cache is built, not dropped (#46). Two claims v1.77 made are
withdrawn as wrong rather than superseded — "nothing may be built from this
paragraph", which no longer holds, and "this module holds instrument metadata in
process memory only", which was never true: `get_instrument` issues one
`share_by` per call and memoises nothing, so what v1.77 described as a harmless
memory cache was an uncached read on the hot path (#46, #102).

**Why the owner is this module and not a `db.instruments` repository.** The cache
is one decision, not two. "Serve the row while it is fresh, otherwise fetch it
and store it" cannot be split, because the fetch is a broker call and this is the
only module permitted to make one; a repository could hold the SQL, but the
freshness window, the field rule below and the miss policy would still have to
live here, and a contract whose halves sit under two `###` headings is a contract
`make_tasks.py` hands to two agents in halves (failure class 2). Keeping it here
also means **no caller changes**: `market.data`, `pnl.benchmark_return`,
`app.startup` step 8a, `app.loops` step 6 and `broker.reconcile` all keep the
`get_instrument` call they make today, so the amendment's blast radius is one
module rather than six (failure class 1). The precedent is settled: `state.halt`
owns `halt_state` and `broker.reconcile` owns `reconciliations` — a
single-purpose table owned by the module it is about, which `instruments` is.

**What that costs, stated rather than waved away.** This module has never touched
the database, and from this version it owes every rule a repository owes:

- It must never call `aiosqlite.connect` and must never close the connection it
  uses. Every statement runs on `db.connection.shared()`.
- It must never issue `BEGIN`, `commit` or `rollback`. Every write runs inside
  `db.connection.transaction(critical=False)` (rule 12, rule 31).
- It writes no table but `instruments`, and no other module writes that one. A
  caller wanting instrument metadata calls `get_instrument`; there is nothing
  else to call.
- Its tests now need the temporary-database fixture `db.connection` owns in
  `tests/conftest.py`, and its 86% per-file coverage floor applies to the new
  branches like any other — it is a ratchet, so the cache may not be the reason
  it drops.

**Two classes of field, and the split is the whole design (v1.85).**

- **Cacheable: `figi`, `ticker`, `lot`, `min_price_increment`, `currency`.** These
  change on a corporate action — a lot-size change, a step change, a ticker
  reassignment — which the exchange announces days ahead. They are not static and
  are not cached forever: §2.1 records lot sizes SBER 1, **GAZP 10**, LKOH 1,
  MGNT 1 and price steps SBER/GAZP 0.01, **LKOH/MGNT 0.50**, and a wrong lot is a
  wrong position size while a wrong step is a stop the exchange refuses. Both are
  wrong answers, not slow ones, which is why they carry a window.
- **Never cacheable: `trading_status`.** It changes intraday — a volatility halt
  moves an instrument into a break or a discrete auction inside one cycle — and
  `risk.gate` rejects `INSTRUMENT_NOT_TRADING` on it. A stored `NORMAL_TRADING`
  is the bot sizing and submitting an entry into a halt. **The positive action:
  every call to `get_instrument` obtains the trading status from the broker,
  whatever the age of the stored row**, so a halted instrument is rejected by
  `risk.gate` on the cycle it halts and not on the cycle a window happens to
  expire.

**Fresh means `clock.now() - refreshed_at <= 24 hours`. Anything else is stale,
and an absent row is stale.** That is the bound. It is a module constant here and
not a `config` value: it is not an operator-tunable risk parameter, and the
number is chosen against the thing that invalidates the row — a corporate action
carries days of notice, so one day is inside it, while a shorter window would buy
nothing and a longer one would let a lot-size change survive a night. A watchlist
ticker is read many times a session, so the window is crossed once a day per
ticker and never in the middle of a session.

**What a stale row can cost, traced rather than asserted.** The window is only
defensible if the worst a day-old row can do is named:

- **A changed `lot`** is picked up up to one day late, and cannot become
  over-exposure: `execution.orders.open_position` reads
  `broker.client.get_max_lots(figi)` live before every submission and clamps
  `lots` down to it, and the broker computes that ceiling from the true lot size
  and the account's real cash. A stale lot that is too small under-sizes a
  position for at most a day; a stale lot that is too large is clamped by a live
  read on the money path. That control already exists and is not being added here
  — it is the reason one day is an acceptable bound rather than a hopeful one.
- **A changed `min_price_increment`** is picked up up to one day late, so
  `post_stop_loss` can post an off-step price once and be refused. That is rule
  23's degrade: the position settles `LOCAL`, the owner is alerted, and
  `lifecycle.exits` evaluates the stop from quotes until the next read past the
  window corrects the row. Noisy, visible, and never an unprotected position.
- **A reassigned `ticker`, so a stale `figi`.** Where the row's figi no longer
  resolves, the status read raises `InstrumentNotFound` — the same exception
  `share_by` would raise for the ticker — and rule 8's mid-session skip handles it
  unchanged. Where it still resolves, because it now names the *former* security,
  nothing in this design detects it for up to one day: the row is keyed by ticker
  and the returned `Instrument.ticker` is the row's. **That is a new exposure and
  it is stated rather than buried.** It is bounded at the window; MOEX announces a
  reassignment ahead of it; and the lever if one is announced is to shorten the
  constant and deploy, which is why the number is a constant an operator can see
  rather than a value derived at runtime. Detecting it properly would mean reading
  the broker to find out whether the row is right, which is the call the cache
  exists to avoid.

The first two are corrected by the next read past the window, which is what the
window is for. None of the three is corrected by hand: no code path deletes or
edits a row, so there is no repair procedure to get wrong.

**What one call costs, in each branch. Exactly one broker request, never two.**

- **Stale or absent row** → `share_by`, as today. Its response carries every
  field including the status, so no second request is made. The row is written
  through before the `Instrument` is returned.
- **Fresh row** → no `share_by`. The five cacheable fields come from the row and
  the status comes from a live market-data read of the trading status for the
  row's `figi`, through a private helper of this module — a new public function
  would put a second instrument surface in front of five callers that asked for
  one. The row is **not** rewritten on this branch: `refreshed_at` describes when
  the dimensions were read, and a fresh status is not evidence that the
  dimensions were.

**`refreshed_at` on the returned `Instrument` is the instant the cacheable fields
were read, on both branches, and never the instant of the status read.** A caller
reading it is asking how old the lot size is, which is the only question the
field can answer. It needs no new column and no new call: `Instrument` has
carried `refreshed_at` since the first version and nothing has ever read it
(failure class 13 — the value this design wants was already at the point of use).

**What this changes, measured in requests.** On a 10-ticker watchlist at a
60-second poll, one 8.5-hour session issues about 5,100 `share_by` requests
today, one per ticker per cycle from `market.data` plus one per signalling
ticker from `app.loops`. Under this contract it issues **ten** — one per ticker
per day — and answers every other read from the row. **The number of broker
requests per cycle does not fall**; what falls is how often rarely-changing data
is re-read, and the remaining per-cycle request moves off `share_by`, which §2.1
records as deprecated as of SDK 1.0.0 and which is on the hot path only because
nothing else resolved a ticker. Removing the per-cycle request outright is the
open decision below.

**A cache miss is not an error (v1.85).** A ticker absent from `instruments`, or
present and stale, is the ordinary first read of that ticker: fetch with
`share_by`, store, return. There is no preload step and no background refresher
— a periodic job is one more supervised loop and one more schedule a restart can
step over (failure class 15), for a value only ever wanted at the moment it is
used. Refresh is therefore **on use**, and the cadence is one `share_by` per
ticker per 24 hours plus one for every ticker whose row is missing.

**A miss at startup and a miss during a trading cycle behave identically, and
that is deliberate.** Both go through this function, both cost one `share_by`,
and neither refuses. `app.startup` step 8a already reads every watchlist
instrument before the ready alert, so the first cycle of a fresh process runs on
rows step 8a wrote seconds earlier; that is a consequence of the existing
ordering, not a rule this contract adds, and step 8a is not amended. Whether
startup should *refuse* on metadata it cannot read is rule 8's startup half,
which remains the open decision recorded in §8 and is untouched here. This
version changes where a value comes from, never whether its absence stops the
bot.

**A broker failure is never answered from the table.** When `share_by` fails on a
miss, or the status read fails on a hit, the same typed exception is raised as
today — `InstrumentNotFound`, `BrokerUnavailable`, `BrokerRateLimited` — and rule
8's mid-session skip and rule 9's absorption in `market.data` handle it exactly
as they do now. **Never serve a stored row in place of a broker that could not be
reached**; the positive action is to raise, because a caller that skips a ticker
for one cycle is correct and a caller sizing an entry on a status nobody
confirmed is not. That also settles what this table is *not*: v1.77 recorded
outage survival as the only thing a durable cache would buy, and it is the one
use this contract forbids.

**A write failure never fails a read (rule 12).** The write runs inside
`db.connection.transaction(critical=False)`. On `aiosqlite.Error` it is logged at
ERROR and swallowed, and the `Instrument` from the live read is still returned —
the caller asked for metadata, not for a cache — and the next call for that
ticker simply misses again. **Every other exception propagates**, `TypeError` and
`AttributeError` foremost, exactly as rule 12 was narrowed in v1.75: an analytics
path is where a dropped programming error survives longest. No alert is raised on
any of this, and none is wanted: rule 12 is stdout-only, a cache that is not
filling degrades nothing an operator can act on, and an alert per cycle in a
channel whose premise is that silence means healthy is equivalent to no alert
(failure class 14).

**When no database is open, the cache is skipped and the live read is
unaffected.** `db.connection.shared()` raises `DatabaseNotOpenError` (rule 30)
before `app.startup` step 3 and in every process that never opens a database:
`sandbox/data.py` and `scripts/research/backtest.py` both call this function on a
laptop with no bot database, and `scripts/verify/` speaks to the SDK directly. On
the read and on the write this module catches `DatabaseNotOpenError` **and nothing
wider**, logs it at DEBUG, and behaves exactly as it does today — one `share_by`,
no row. This is not a degraded state and raises no alert: there is nothing wrong
with a cache that is not present, and narrowing the catch to that one class is
what keeps a genuine database fault loud (failure class 5).

**This catch cannot hide a missing `app.startup` step 3, which is the reason rule
30 fails loudly.** Step 5 refreshes the calendar into `db.trading_days` and step 6
reads `db.orders`, both before the first `get_instrument` any process makes — the
earliest is in step 7's reconciliation remedies — so an unopened database has
already raised out of a repository that does not catch it. The catch here changes
what a *deliberately* database-free process does, never what a misordered startup
reports.

**The status read is unmeasured, and V13 measures it before that branch is built
(v1.85).** This is the first thing in the project to depend on the market-data
trading-status endpoint. §2.1 exists because the two most expensive defects here
were assumptions inspection could not falsify, and tests written against a mocked
endpoint would agree with the guess (failure class 11). **No session may
implement the fresh-row branch before V13 is green.** The half that is buildable
without it is the write-through: `get_instrument` keeps its single `share_by` on
every call and records the row, which gives the table its writer and makes
`refreshed_at` real, while every read is a miss. If V13 shows the endpoint absent
on the pinned SDK, or without the headroom it asks for, the fresh-row branch is
not built and the question returns to the owner with a measurement attached
rather than an assumption.

**Open decision — the identity-only read (v1.85). Not settled here.** Two of the
five callers never touch `trading_status`, `lot`, `min_price_increment` or
`currency`: `market.data.candles_for_watchlist` and `pnl.benchmark_return` each
read `instrument.figi` and discard the rest. They are also the only callers on
the per-cycle path, so they pay a live status read per ticker per cycle for a
field they do not look at. Either (a) that stands — one market-data request per
ticker per cycle, inside the 200-per-60-seconds §2.1 records, and a single
instrument surface in front of every caller; or (b) this module gains a second
entry point returning the identity alone, so a warm cycle issues **no** instrument
request at all, `get_instrument` stays a live read forever and the field split
above becomes a type distinction rather than a rule an agent must remember. (b)
is the better answer on requests and the worse one on surface, and it is not
free: it adds a §4 signature, which `check_docs.py` correctly reds until
`tasks/21-broker-client.md` is re-run; and it needs a matching method on
`sandbox.exchange.SimulatedExchange` plus rows in `sandbox.backtest`'s seam table
for `zarabot.market.data` and `zarabot.pnl`, or the backtest reaches the real
network through the one call the table does not patch. **Until it is decided, (a)
is binding.** No agent adds the second entry point on its own reading of this
paragraph.

**Sandbox is selected by endpoint, never by a different method family.** The SDK
exposes a `SandboxService` with a parallel set of methods — `post_sandbox_order`,
`get_sandbox_order_state`, `cancel_sandbox_order` and so on. This module must
**not** use them for trading operations. `TRADING_MODE=sandbox` instead
constructs the client against `INVEST_GRPC_API_SANDBOX` and calls the ordinary
`orders.post_order` / `orders.get_order_state`, so sandbox and live exercise the
same code path and a sandbox result is evidence about live behaviour. Using the
`post_sandbox_*` family would mean testing code that never runs in production —
worse than not testing, because it produces false confidence. The
`SandboxService` methods are permitted only for account housekeeping with no live
equivalent (opening and funding a sandbox account), and only from verification
scripts, never from `zarabot/`.

**Margin trading is prohibited at the call site.** The SDK's `post_order` accepts
`confirm_margin_trade: bool = False`. This module must never pass `True`, under
any condition, for any order. The brief's no-leverage guarantee — which is what
bounds the maximum loss to the allocated capital, and therefore what the entire
security model rests on — is enforced here, at the one place an order can be
created. A code change setting this flag is a critical defect regardless of what
else it does. **This module owns rule 28 (v1.75)**, and the enforcement is a
review gate rather than a runtime one: there is no condition under which `True`
is correct, so there is nothing to detect at runtime and no branch to test. What
makes the rule non-vacuous is the positive action — every submission in this
module passes `confirm_margin_trade=False` explicitly, and V10 asserts the
parameter is still on the SDK signature it is being passed to — a rename that
made the keyword silently inert is the one way this could fail without anyone
writing `True`.

**One channel per process, and one `Config`.** The module holds a single
`AsyncClient`, created lazily on first use — never at import, which `AGENTS.md`
forbids outside `config` — and reused for every later call. `app.shutdown` closes
it through `broker.client.close()`. Opening a fresh client per call meant a new
TLS handshake per request, and a single order cost three full `config.load()`
calls, each re-reading every environment variable, re-parsing every `Decimal` and
touching the filesystem to stat `ML_MODEL_PATH` — on the latency-critical path
(#18). Configuration is read through `config.get()`, the memoised accessor, not
`config.load()`.
- **Emits `broker_unavailable` (WARNING) on each `BrokerUnavailable` with
  `method` and `consecutive_failures`, and `rate_limited` (WARNING) on each
  `BrokerRateLimited` with `method`, `retry_after_seconds` (v1.61).** This
  module is the only one that sees those exceptions at the source; callers must
  not re-emit them.
- **`backoff_seconds` is not a field of this event (v1.65).** v1.61 required it
  here, but this module does not back off and cannot know the number: the
  escalating delay, its cap and the broker's `retry_after` hint all belong to
  `app.loops`, whose §3.2 cases pin them. An emitter that cannot know a value
  can only send a constant, and a constant `backoff_seconds: 0` tells an
  operator that no back-off is in effect while `app.loops` may be five cycles
  deep in one — worse than the field's absence.
- `consecutive_failures` **is** knowable here and stays: it is this module's own
  count of consecutive `BrokerUnavailable` raises for that method, reset when
  the method next succeeds. A count that is never reset is a latch, and this one
  must be cleared on the success path.
- If a back-off figure is wanted in the log, it belongs to an event owned by
  `app.loops`, which computes it. Assigning one is a separate amendment; this
  one only stops requiring a field at a site that cannot supply it.

**`async close() → None`**
- Closes the process client and forgets it. Idempotent. Called only by
  `app.shutdown`. A later call creates a new client, so closing is not a
  one-way door for a long-lived process that must reconnect.

**Errors are typed by what they are, not by where they were caught.** Only a
transport failure becomes `BrokerUnavailable`; a `RESOURCE_EXHAUSTED` becomes
`BrokerRateLimited`; `NOT_FOUND` becomes the caller's not-found type. **Every
other gRPC status — `INVALID_ARGUMENT` foremost — and every non-SDK exception
propagates as itself**, with its cause preserved via `raise ... from exc`.

Converting everything into `BrokerUnavailable` is what hid #39 for the whole life
of the deployment: a malformed request was retried forever as though it were
weather, the alert said the broker could not be reached, and the only place the
words `INVALID_ARGUMENT` appeared was the SDK's own log line. A programming error
must not wear an outage's costume — the caller's retry-with-backoff is correct
for an outage and useless for a bug, and the difference between them is exactly
what the type is for (#23). `from None` is forbidden: it discards the traceback
that names the real fault. Token redaction already prevents secret leakage, and
that is what makes preserving the cause safe.

**A quote's timestamp is read directly, never probed for (v1.43).**
`_quote_time` read `raw.time` and fell back to `raw.timestamp`, a field
`LastPrice` has never had (§2.1). The fallback was not merely dead: if `time`
were ever renamed, every quote would come back with no timestamp, be rejected
under rule 9b, and the owner would see "N prices rejected" every cycle — a
message that reads like a broker data problem while the real fault is an
integration break. **No `LOCAL` position's stop-loss would fire again**, because
that exit path needs a price. An `AttributeError` names the field and the line;
a defensive `getattr` chain names nothing. This is failure class 5 in
`ops/STATE.md`, and the rule generalises: where the broker's own field is the
contract, read it, and let its absence be loud.

**A partial fill is not a fill.** `EXECUTION_REPORT_STATUS_PARTIALLYFILL` maps to
`SUBMITTED` — the order is still live at the broker — with `filled_lots` carrying
what has filled so far. `FILLED` means `filled_lots == lots` and nothing further
is coming. Collapsing partial into filled erased the distinction at the boundary,
so no caller could act on it: the bot opened a position for the filled portion
while the remainder stayed live, and the account then held more shares than the
position row recorded (#10). What the *callers* do about a partial fill belongs
to `execution.orders`, and v1.34 settles it there — as neither of the two
remedies this paragraph once anticipated. There is no multi-slice exit loop to
cap and no quantity-weighted price to compute, because a partial no longer
settles as a fill and so never starts a second slice.

**`async get_instrument(ticker: str) → Instrument`**
- Raises `InstrumentNotFound` when the ticker does not resolve, `BrokerUnavailable`
  on transport failure, `BrokerRateLimited` when throttled.
- **Served from the `instruments` table for the five cacheable fields and never
  for `trading_status` (v1.85).** The signature is unchanged and no caller
  changes. A fresh row — `clock.now() - refreshed_at <= 24 hours` — supplies
  `figi`, `ticker`, `lot`, `min_price_increment` and `currency`, and the trading
  status is read live for that `figi`. A stale or absent row costs one `share_by`,
  whose response supplies every field, and is written through before the
  `Instrument` is returned. The full rules — the field split, the miss policy, the
  rule-12 write, the `DatabaseNotOpenError` case and V13 — are in this module's
  paragraphs above.

**`async get_candles(figi: str, interval: CandleInterval, since: datetime, until: datetime) → list[Candle]`**
- Returns candles ordered oldest-first with timezone-aware timestamps.
- Returns an empty list when the range contains no trading activity.
- Raises `ValueError` on naive datetimes.

**`PriceRejected`** — a quote arrived but is not usable. **Distinct from
`BrokerUnavailable`**, which means the broker could not be reached. Conflating
them makes a malformed field read as a network outage, so it counts toward the
consecutive-failure alert and is retried as though waiting would help.

**`async get_last_price(figi: str) → Decimal`**
- Validates the quote at the **single point prices enter the system**, and
  treats a bad price as missing data rather than as a signal. Raises
  `PriceRejected` when:
  - the price is **not strictly positive** — `Decimal(0)` currently flows
    straight through to `lifecycle.exits`, where `0 <= stop_price` is true for
    every position, so one degraded response liquidates the whole book at
    market;
  - the quote's timestamp is older than `price_max_age_seconds`;
  - the quote carries **no timestamp, or a naive one** — freshness that cannot
    be verified is not freshness. This is data from an outside system, so it is
    rejected as unusable rather than raising `ValueError` the way a naive
    datetime crossing an internal module boundary does;
  - the price differs from the last accepted price for that instrument by more
    than `price_max_move_pct`.
- Keeps the last accepted price per instrument, which is what makes the move
  check possible. This is the only state this module holds, and it is why the
  check cannot live in `lifecycle.exits`: that module is pure and has no memory
  of the previous tick.
- A rejected quote does not update the last accepted price. Accepting an
  implausible value as the new baseline would make the *next* implausible value
  look reasonable.

**`async get_portfolio() → PortfolioState`**
- **`cash` is RUB buying power**, not `total_amount_currencies`. That field is the
  converted value of *all* currency positions, including blocked and reserved
  funds and any non-RUB balance, so it overstates what an order can actually
  spend and an order sized against it can be refused for insufficient funds
  (#16). Where the response separates available from blocked, available wins.
- Returns cash and holdings as reported by the broker. This is the authoritative
  view referred to throughout the brief.

**`async get_trading_schedule(days: int) → list[SessionInfo]`**
- Session open and close instants per day, timezone-aware, marking non-trading
  days.
- **Requests the exchange by name: `exchange="MOEX"`.** Not a substring filter
  over the returned list. The response carries 147 exchanges, 53 of which contain
  `MOEX`, and taking the first match returned whichever the broker happened to
  order first — in practice `MOEX_MRNG_EVNG_E_WKND_D`, an extended session
  running to 23:49 MSK that reports **Saturday and Sunday as trading days**
  (#43). `MOEX` is the main equity board: 10:00–18:54:59 MSK, weekends closed.
  Naming it returns exactly one exchange, so there is nothing left to choose.
- **The range is anchored to the start of the current UTC day**, and `days` may
  not exceed 14. A mid-day `from_` plus 14 days is rejected by the broker with
  `INVALID_ARGUMENT` / `30002`, which is what left the cache empty and
  `is_open()` false for the whole of the bot's life (#39). Confirmed against the
  live account: `from=now, to=now+14d` fails, `from=now, to=now+13d` succeeds,
  and `from=midnight, to=midnight+14d` succeeds — the horizon is measured from
  the start of the day, not from the instant of the call.
- **A day with `is_trading_day` false yields no session**, whatever its
  timestamps say. A closed day carries `1970-01-01` in both, and some MOEX-
  prefixed exchanges report `is_trading_day` true with those epoch values; either
  read as a session is another way to believe the market is open.
- **Every returned `SessionInfo` carries `trade_date`, read from
  `TradingDay.date` (v1.81).** It is the Moscow calendar date of that field —
  `clock.moscow_date` of it where the SDK hands back an instant, the value itself
  where it hands back a `date`. Never `.date()` on the UTC value, which is wrong
  by one day whenever the broker expresses midnight Moscow rather than midnight
  UTC; never derived from `start_time`, which is the sentinel on a closed day;
  and never derived from the entry's position in the list, which would turn a
  short response into a silently mis-dated calendar. `date` is populated and
  correct on a **non-trading** day, measured against the live account (§2.1) —
  the sentinel lives in fields 3 and 4 only. Mapping `start_time` and `end_time`
  to `None` on a closed day is the correct normalisation and stays; discarding
  the date with them was #51.
- **The result is ordered ascending by `trade_date` (v1.81).** The broker
  returns one entry per calendar day, contiguous, oldest first (§2.1); this
  contract states it so that `market.session` may read `fetched[0]` as the
  window's first day without depending on a response shape nothing pins. Sort if
  the response ever arrives otherwise.
- **A day whose `date` is undateable is omitted from the result, logged at
  WARNING (v1.81; the condition written out in v1.84).** Undateable means any of:
  the field is absent; it is below the epoch guard; it is a naive `datetime`; or
  it is neither a `date` nor a `datetime`. The last two were implemented with the
  first two and are named here because the code omitted more than this clause
  said (#51). A naive instant is the interesting one: it cannot be converted to a
  Moscow date without choosing a timezone on its behalf, and choosing one is
  exactly the fabrication this amendment exists to remove — so it is undateable
  in the same sense as a missing field, not a lesser case to be salvaged.
  There is no honest fallback: an entry with no date cannot be keyed, cannot be
  recorded, and cannot be deduped, and the two available alternatives —
  fabricating a date from list position, or admitting a `None` back into
  `SessionInfo` — are each the defect this amendment removes. Omission is the
  same posture `get_executed_stop_fills` already takes toward a stop whose
  `exchange_order_id` does not resolve: leave it out rather than substitute a
  guess. It shortens the window, which `market.session.covers` reports, instead
  of mis-dating a day, which nothing would report. This has never been observed;
  it is contracted because the sentinel in the neighbouring fields has bitten
  twice.
- The SDK marks `trading_schedules` deprecated as of its 1.0.0. It is the only
  calendar surface available today; when a replacement appears this is the
  contract to amend, and V10's recorded SDK version is what makes the change
  visible.

**`async post_market_order(key: str, figi: str, side: Side, lots: int) → OrderRecord`**
- Submits a market order using `key` as the broker-side idempotency key.
- Raises `OrderRejected` carrying the broker's reason, `BrokerUnavailable`, or
  `BrokerRateLimited`.
- Must never be called before `db.orders.record_submitting` has persisted `key`.

**`async post_stop_loss(key: str, figi: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
- Places a standing stop-loss with the exchange using
  `post_stop_order(stop_order_type=STOP_ORDER_TYPE_STOP_LOSS,
  expiration_type=STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
  exchange_order_type=EXCHANGE_ORDER_TYPE_MARKET)`, carrying its own idempotency
  key and `confirm_margin_trade=False`.
- Raises `StopOrderRejected` carrying the broker's reason, or `BrokerUnavailable`.
- Good-till-cancel is required: a day-expiring stop would silently stop
  protecting the position overnight, which is precisely when it is needed.

**`async cancel_stop_order(stop_order_id: str) → None`**
- Idempotent. An already-cancelled or already-executed stop order is not an
  error, because the executor calls this while racing the exchange.

**`async cancel_order(key: str) → None`**
- Cancels a live ordinary order by its idempotency key, with
  `order_id_type=ORDER_ID_TYPE_REQUEST` — the same lookup `get_order_state`
  uses, because after a crash the exchange identifier is precisely what was
  lost.
- Idempotent in the same sense as `cancel_stop_order`: an order already filled,
  already cancelled, or unknown to the broker is **not** an error. The caller is
  racing the exchange by definition, and the authoritative answer comes from the
  `get_order_state` that follows it, never from this call's own outcome.
- Raises `BrokerUnavailable` or `BrokerRateLimited` on transport failure, and
  nothing else.
- Added in v1.34 so `execution.orders` can abandon the unfilled remainder of a
  partially filled **entry** (#10). It must never be used on an exit: a
  half-exited position is the one state the system must not rest in, and the
  remainder there is retried, never dropped.

**`async list_stop_orders() → list[StopOrderRecord]`**
- Every standing stop order on the account. Consumed by reconciliation.

**`async get_executed_stop_fills(since: datetime, until: datetime) → dict[str, OrderRecord]`**
- Returns the **actual execution** of every stop order that fired in the window,
  keyed by the broker's `stop_order_id`. Empty dict when none fired; never
  `None`.
- Implemented as two SDK calls, composed here because this is the only module
  permitted to talk to the broker: `get_stop_orders` with
  `StopOrderStatusOption.STOP_ORDER_STATUS_EXECUTED`, then each result's
  `exchange_order_id` resolved through `get_order_state` with
  `OrderIdType.ORDER_ID_TYPE_EXCHANGE`.
- The `OrderRecord` carries the broker's own numbers: `filled_price` from
  `executed_order_price`, `filled_lots` from `lots_executed`, and `commission`
  from `executed_commission`. None of the three is estimated, and none comes from
  a quote.
- **A stop whose `exchange_order_id` does not resolve is omitted, not guessed
  at.** The caller leaves the position open and retries. A position closed a
  minute late is recoverable; a position closed at an invented price is not.
- Raises `ValueError` on naive datetimes.

**`async get_order_state_by_broker_id(broker_order_id: str) → OrderRecord`**
- The same lookup as `get_order_state` but with
  `order_id_type=ORDER_ID_TYPE_EXCHANGE`, for a row whose `key` the broker has
  never seen (v1.39). Raises `OrderNotFound` when it does not resolve.
- Exists because a stop the exchange fired is recorded locally under an
  idempotency key the bot invented, so `get_order_state` by that key can only
  ever return `OrderNotFound` — which made the commission on every stop exit
  permanently unrecoverable (#8). It is a separate function rather than a
  parameter on `get_order_state` because the two answer different questions:
  one asks "what happened to the order I sent", the other "what happened to the
  order the exchange placed for me", and only the first is a recovery path.
- The returned record's `key` is the `broker_order_id` it was asked about, as
  `get_executed_stop_fills` already does. This module does not know the local
  row's key and must not guess at one.

**`async get_max_lots(figi: str) → int`**
- The maximum lots the broker will accept for a buy on this account. A pre-submit
  sanity check against `risk.sizing`, which models cash but not settlement or
  instrument-specific restrictions.

**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
- Executed operations including actual commission charged. Used for independent
  reconciliation of costs over a period, and — **since v1.91 and only by
  identifier** — as the per-order commission source of last resort.
- **What is still forbidden is the matching, not the feed (v1.91, #246).** Until
  v1.91 this line read "**not** as the per-order commission source:
  `OperationRecord` carries no order identifier, so attributing an operation to
  an order would mean matching on instrument, time and quantity, which is
  ambiguous exactly when two similar orders are close together." The second half
  of that sentence is still binding and always will be; the first half was a
  conclusion drawn from it that does not follow, and it is what left every
  realised P&L gross (§2.1). `OperationRecord` **does** carry an identifier the
  broker issued — `trades[].trade_id`, exposed as `trade_ids` — and an order's
  own `stages[].trade_id` is the same identifier. Attribution by that join is
  exact: an id matches or it does not. Matching on instrument, time and quantity
  remains forbidden here and everywhere.
- Each record carries `operation_type`, `state`, `parent_operation_id` and
  `trade_ids` verbatim (v1.35; `trade_ids` v1.91). `commission` is populated for fee operations, identified by
  `operation_type`, and is zero elsewhere. It was identified by testing whether
  the string `FEE` appeared in an attribute the record did not expose, which
  worked only because every fee type happens to contain it.
- **Only `OPERATION_STATE_EXECUTED` operations are returned.** A cancelled or
  still-progressing operation is not something that happened, and counting one
  as a cost or as a sale is the same error in two places.
- v1.35 gives this function a second consumer and a second purpose:
  `broker.reconcile` reads it to answer the one question no order of ours can,
  which is what a sale **the bot did not submit** was actually done at. That does
  not make it the per-order commission source; the paragraph above still holds.

**TLS requires the broker's own root certificate.** T-Bank's endpoint presents a
certificate chaining to the Russian Trusted Root CA, which gRPC's built-in trust
store does not contain. The SDK ships that root at
`t_tech/invest/certs/RussianTrustedRootCA.pem` but loads it **only** when the
environment variable `SSL_TBANK_VERIFY` is `true`; its default is `false`, so
every call otherwise dies in the TLS handshake with
`CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain`.

This is not a property of any particular network — it was reproduced from a
clean machine, and would fail identically on the VPS. `config` therefore exposes
`ssl_tbank_verify`, defaulting to **true**, and `app.startup` writes it into the
process environment immediately after `config.load()` and before any broker call.
`broker.client` must not read the variable itself: the SDK reads it from the
environment when a channel is created, so the only requirement is that it is set
before the first client is constructed.

**Commission does not come back on the order itself, and a zero there is
unknown, not measured (v1.91, #246).** Both `PostOrderResponse` and `OrderState`
carry `executed_commission`, keyed by our own idempotency key, and this spec
said until v1.91 that populating `OrderRecord.commission` from it was the whole
story. It is not: on the live account the field reads **zero on 22 of 22 real
fills** while 22 fee operations totalling 14.67 exist in the same feed for the
same trades (§2.1). `Decimal(0)` was therefore written where nothing was known,
`db.orders.list_missing_commission` selects `commission IS NULL`, and the
backfill built to recover a late commission has never been shown a single one of
these rows.

The contract question that produced this is *"is a zero at fill a measurement or
an absence?"*, and v1.91 answers it: **an absence.**

- **`_executed_commission` returns `None` when the broker reports a zero
  commission on an order it reports as filled.** A non-zero value is the
  broker's own number for that order and is recorded as before. This applies
  everywhere the helper is used — `post_market_order`, `get_order_state`,
  `get_order_state_by_broker_id` and `get_executed_stop_fills` — because the
  field is the same field in all four.
- The cost of this reading is that a **genuinely commission-free trade** is
  recorded as unknown rather than as zero. That is not left hanging: the
  operations feed settles it as a *measured zero*, terminally, under
  `ops.commissions` below. Nothing alerts forever on one.
- The value of this reading is that the row becomes visible to the backfill at
  all. Nothing is estimated: `None` is what "the broker has not told us" looks
  like, and the arbiter is still the broker.

**Commission is never estimated. The per-order source is the order state where
it carries a number and the operations feed where it does not, joined by an
identifier the broker issued** — never by matching on instrument, time and
quantity, which is ambiguous exactly when two similar orders are close together,
and which was declined once already for #8.

**`OrderRecord.trade_ids` and `OperationRecord.trade_ids` carry the join
(v1.91).** `OrderState.stages[].trade_id` is the set of executions the broker
attributes to one order; `Operation.trades[].trade_id` is the same identifier on
the operations feed. Both are read verbatim into a `tuple[str, ...]`, empty where
the broker supplies none. They are **broker-sourced and never persisted**: no
column holds them, `db.orders` returns `()` for every row it reads, and nothing
downstream may treat an empty tuple as anything but "no join material".

**The join is an assumption with a gate, not a measurement (v1.91).** §2.1
records it as unmeasured and names **V14** as its check. Its failure mode is an
unresolved commission and a once-per-order alert — loud and recoverable — never a
wrong number, which is why it is admissible where FIGI-and-time matching is not.

**`async get_order_state(key: str) → OrderRecord`**
- Retrieves an order **by the client idempotency key alone**, so a restarted
  process can determine what happened. Implemented as
  `get_order_state(account_id=…, order_id=<our key>, order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)`.
  The enum member exists in the SDK (`OrderIdType.ORDER_ID_TYPE_REQUEST = 2`) and
  the parameter is present on the async method signature, so this is verified
  against the library, not only against documentation. Passing the exchange
  identifier here is a defect: after a crash the exchange identifier is precisely
  what was lost.
- The key passed to `post_order` as `order_id` and the key passed here are the
  same value — the row's primary key in the `orders` table.
- Raises `OrderNotFound` when the broker has no record, which proves the order
  was never accepted.
- **There is exactly one recovery path, and it is this one (v1.73).** Until
  v1.73 this contract offered a "documented fallback": that `PostOrder` is
  idempotent on the `(orderId, accountId)` pair, so recovery "may re-call
  `post_market_order` with the original key, which is a safe read". §2.1 measured
  the opposite on a live account — a duplicate idempotency key is **refused**
  with `INVALID_ARGUMENT`/`30057` and does not return the existing order — and
  resubmitting an entry is forbidden outright by the brief and by `AGENTS.md`.
  Recovery is `get_order_state` by key, never a second submission, and no
  reasoning anywhere may rest on a second recovery path (#92).
- **Key retention caveat.** The broker states idempotency keys are retained for
  one year but explicitly declines to guarantee it, noting the mechanism may
  change. This design needs retention measured in minutes — from crash to
  restart — so the caveat is immaterial here, but it means keys must never be
  treated as a permanent audit identifier. The `orders` table is that record.

All functions in this module: must never log or include the token in any
exception; must convert every SDK exception into one of the typed exceptions
above; must never return a `float`.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

2. **Broker rate limited** → WARNING, back off **for at least as long as the
   broker's own hint**, and alert once when it has persisted for three
   consecutive cycles, naming throttling rather than an outage. Never treat as
   fatal.

   `BrokerRateLimited.retry_after` carries the hint when the broker sends one.
   It is the only party that knows when it will accept calls again, so it raises
   the floor under the escalating back-off of rule 1 and never lowers it: the
   delay is the longer of the two. It is still bounded by the same ceiling,
   because this loop is also the **exit** path — no number supplied from outside
   may keep the bot from closing a position indefinitely.

   Until v1.53 the hint was computed, asserted at the raise site, and read by
   nothing: the bot backed off on its own schedule while the broker's answer sat
   unused on the exception. This rule previously said "alert once if sustained
   beyond five minutes", which named a threshold no code implemented and no test
   could fail — the alert has always come from rule 1's consecutive-cycle
   counter. It now says what happens.

3. **Entry order rejected** → ERROR, record `broker_reason`, alert, open no
   position. **Never retried.**

4. **Exit order rejected or broker unreachable during an exit** → ERROR, alert
   **immediately**, retry on every following cycle until the position closes or
   the owner intervenes. The documented exception to rule 3.

5. **Order submission times out or the outcome is unknown** → leave the row
   `SUBMITTING`, resolve by querying with the idempotency key on the next cycle
   or at next startup. **Never resubmit.**

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

    **All three paths have a writer as of v1.85.** Until then "instruments cache"
    named nothing: the table had no writer anywhere in `zarabot/` (#102), so this
    rule listed a path that could not fail. `broker.client` owns it now, and the
    swallow there returns the `Instrument` the broker just supplied — the caller
    asked for metadata, not for a cache (#46).

    **Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error`
    (v1.75)** The swallow exists for a database that will not take the row, not
    for every way the call site can be wrong. Any other exception propagates and
    reaches rule 21's supervisor with its traceback. Unqualified, this rule reads
    as `except Exception: pass` on the analytics path, and an analytics path is
    exactly where a silently dropped `TypeError` survives longest — nothing
    downstream misses the row until a weekly report is composed from it.

19. **Secret exposure** → no token **and no account identifier** is ever written
    to a log, an exception message, or a Telegram message. If the redaction
    filter detects a secret in an outgoing Telegram message, the message is
    **dropped**, `secret_redacted` is emitted, and an alert reporting the
    incident without the secret is sent in its place.

28. **Any code path that would set `confirm_margin_trade=True`** → rejected in
    review, not at runtime. There is no runtime condition under which this is
    correct; it is listed here because the failure mode it would produce —
    losses exceeding allocated capital — is the one failure the brief promises
    cannot happen.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

31. **A module begins, commits or rolls back the shared connection itself** →
    programming defect, in the same family as rules 22 and 30. Every write runs
    inside `db.connection.transaction()`; nothing else touches transaction state.
    A `commit()` is connection-wide, so a module committing on its own behalf
    commits whatever another module has in flight, and that module's `rollback()`
    then undoes nothing. This is not a runtime condition to handle — it is a rule
    the code must not violate, and §3.2 pins it per module.

    **`db.migrations` is the single exemption**, and it is narrow: `apply`
    receives a connection rather than taking one, applies each file in its own
    transaction as §6 requires, and runs during `app.startup` step 3 — before any
    other task exists, so there is nothing in flight for it to commit. Every
    other module, without exception, uses `transaction()`.

33. **A recorded price comes from the broker, or the record stays pending.**
    Realised P&L, exit prices and commissions are written from what the broker
    reports it did — an order state, an executed stop, an operation — and never
    from a quote, a stop price, an entry price, or any other number the bot has
    to hand. Where the broker's own record is not yet available, the position
    stays open and the read is retried on the next cycle; **after three
    consecutive cycles in which the read is still unavailable, the owner is
    alerted once for that order, and the alert re-arms when the order settles
    (v1.75)**. A position closed a minute late is
    recoverable and a position closed at an invented number is not, because
    nothing downstream can tell the invented one from a real one. This rule
    generalises #4, #5, #8 and #11, which are four instances of the same
    mistake.

    **Where the bound applies, and where "cycle" is the wrong unit (v1.75).**
    "A bounded number of cycles" named no bound until v1.75, which is precisely
    what rule 2's own post-mortem calls the defect it was rewritten to remove —
    "a threshold no code implemented and no test could fail" — live one rule
    family over, on the path that decides whether a position stays open with real
    money in it (#112). Three, matching rules 1, 2 and 9, because they are the
    same shape and a second threshold in the same system is a second thing to
    remember. **The counted path is `execution.orders.resolve_unfinished`**,
    which runs once per cycle, already logs `order_unresolved` with an
    `age_seconds` computed from the order's own `created_at`, and already
    distinguishes "the broker cannot be reached" from "the broker says this order
    was never placed". That is the full set of three parts rule 36 requires:
    threshold, one alert, reset on settlement.

    **`broker.reconcile`'s `EXIT_UNRESOLVED` is not on this counter and is not
    a cycle.** Reconciliation runs at `app.startup` step 7 and appears in no
    `app.loops` task list, so its retry cadence is one per process start, not one
    per minute, and it alerts once per pass by construction. Suppressing it
    across *restarts* would require durable state — restarts are routine (failure
    class 15) — and whether an operator should stop being told about an
    unresolved exit because the process has bounced is a judgement about a real
    money-path alert, not a bug to be fixed in passing. **It is left alerting
    once per pass**, and the question of a durable, restart-surviving latch is
    recorded here as open and unowned. It is entangled with rule 25's open
    decision, which is what would put a reconciliation task on a cadence in the
    first place, and should be settled with it rather than before it.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Each method returns the documented domain type given a scripted broker
  response (happy path per method).
- **An order state reporting `execution_report_status = FILL` and
  `executed_commission` of units 0 / nano 0 yields `commission is None`, and the
  same state with a non-zero `executed_commission` yields that `Decimal`**
  (v1.91; proves a zero at fill is recorded as unknown and not as a measurement,
  which is the whole of #246. A fixture whose `executed_commission` is absent
  pins nothing: `None` already produced `None`, and that is exactly why this
  survived — failure class 3).
- `post_market_order` on a fill with a zero `executed_commission` writes
  `commission is None` too (v1.91; proves the rule is on the field and not on one
  call site — the same helper serves four).
- An order state whose `stages` carry `trade_id`s exposes them as `trade_ids` in
  order, and one with no stages exposes `()` (v1.91; proves the join material
  reaches `ops.commissions` rather than being read and dropped).
- `get_operations` exposes each operation's `trades[].trade_id` as `trade_ids`,
  and `()` where there are none (v1.91; the other half of the same join).
- A transport error raises `BrokerUnavailable` (proves transport failures are
  typed, not leaked as SDK exceptions).
- **An `INVALID_ARGUMENT` response does not raise `BrokerUnavailable`** — it
  propagates with its cause intact (proves a malformed request is a defect rather
  than weather, which is the confusion that hid #39 for the life of the
  deployment).
- An `AttributeError` raised inside the wrapper — a renamed SDK field — reaches
  the caller as an `AttributeError` with its traceback (proves the catch is
  narrowed to the SDK's own failures and `from None` is gone).
- `get_trading_schedule` requests the exchange named exactly `MOEX`, and a
  Saturday and a Sunday in the result are non-trading days (proves the calendar
  is the main board's and not an extended session that trades weekends, #43).
- `get_trading_schedule` anchors its range to the start of the current UTC day
  (proves the request the broker rejects with `INVALID_ARGUMENT` is not sent:
  mid-day plus 14 days fails, midnight plus 14 days does not, #39).
- A day returned with `is_trading_day` false yields no session even when its
  start and end are `1970-01-01` (proves the flag is honoured before the
  timestamps).
- That same closed day comes back with `start` and `end` `None` **and a
  `trade_date` equal to its `TradingDay.date`** (v1.81; proves the sentinel is
  normalised away in fields 3 and 4 without taking field 1 with it, which is
  #51. A fixture whose closed day has no `date`, or whose `date` matches the
  neighbouring trading day, pins nothing).
- A trading day's `trade_date` equals `clock.moscow_date(start)` (v1.81; proves
  the producer obligation `models` cannot check — it cannot import `clock`
  without a cycle — is checked at the producer that can).
- A `TradingDay.date` expressed as **midnight Moscow** — 21:00 UTC on the
  previous calendar day — yields the Moscow date, not the UTC one (v1.81; proves
  the conversion is `clock.moscow_date` and not `.date()`. A fixture at midnight
  UTC is satisfied by either and pins neither).
- Days are returned ascending by `trade_date` given a response in any order
  (v1.81; proves `market.session` may read `fetched[0]` as the window's first
  day).
- A day whose `date` is absent, or is the `1970-01-01` sentinel, is **omitted**
  from the result and logged at WARNING, and the remaining days are returned
  (v1.81; proves an undateable entry is dropped rather than given a fabricated
  date from its list position — and that one bad entry does not take the window
  down).
- A `PARTIALLYFILL` report maps to `SUBMITTED` with `filled_lots` below `lots`,
  never to `FILLED` (proves a partial fill stays visible as partial, #10).
- `get_executed_stop_fills` returns the broker's executed price, lots and
  commission for a stop that fired, keyed by `stop_order_id` (proves the exit is
  booked from the broker's own record rather than from a quote, #4).
- A stop whose `exchange_order_id` does not resolve is **omitted** from the
  result rather than returned with a substituted price (proves the caller is left
  to retry rather than handed a guess).
- Nothing fired in the window → empty dict, not `None`.
- Two successive calls reuse one `AsyncClient`, and `close()` then releases it
  (proves the channel is per process rather than per request, #18).
- `config.get()` is called once across a sequence of broker calls (proves the
  configuration is not re-read and re-validated per request).
- A rate-limit response raises `BrokerRateLimited` carrying the retry hint
  (proves the caller can back off correctly).
- A quote object with no `time` attribute raises `AttributeError`, **not**
  `PriceRejected` (proves a renamed SDK field surfaces as the integration break
  it is, rather than as every quote in every cycle looking like bad broker data
  — the state in which no `LOCAL` stop-loss can fire).
- Raising `BrokerUnavailable` emits `broker_unavailable` with `method` and
  `consecutive_failures` (v1.61). Raising `BrokerRateLimited` emits
  `rate_limited` with `retry_after_seconds`.
- A method that fails twice then succeeds reports `consecutive_failures` 1 then
  2, and a later failure reports 1 again (v1.65; proves the count is cleared on
  the success path rather than ratcheting for the life of the process).
- A zero-valued quote raises `PriceRejected`, not `BrokerUnavailable` and not
  `Decimal(0)` (proves the mass-liquidation path is closed at its source, and
  that bad data is distinguishable from an outage).
- A quote older than `price_max_age_seconds` raises `PriceRejected` (boundary:
  exactly at the threshold is accepted, one second beyond is not).
- A price more than `price_max_move_pct` from the last accepted price raises
  `PriceRejected`, and the last accepted price is **unchanged** afterwards
  (proves an implausible value cannot become the baseline that makes the next
  one look reasonable).
- An order rejection raises `OrderRejected` carrying the broker's reason string
  (proves the reason reaches the owner).
- No exception raised by this module contains the token in its message (proves
  the secret boundary).
- Prices returned are `Decimal` (proves no float leaks in from the SDK).
- **The instruments cache (v1.85).** A ticker with no row is fetched with
  `share_by`, and the row afterwards holds the same `figi`, `lot`,
  `min_price_increment`, `currency` and `refreshed_at` the returned `Instrument`
  holds (proves the write-through is the row that was returned, not a second
  reading of the response that can drift from it).
- A second call for the same ticker inside the window issues **no** `share_by`
  and **does** issue a live trading-status read (proves the five cacheable fields
  come from the row and the status never does).
- A fresh row storing `NORMAL_TRADING` whose live status read returns
  `BREAK_IN_TRADING` returns `BREAK_IN_TRADING` (proves `risk.gate` sees a halt on
  the cycle it happens rather than on the cycle a window expires — the case #46
  exists to close. A fixture whose stored and live statuses agree pins nothing).
- A row whose `refreshed_at` is older than the window is refetched with `share_by`
  and rewritten; a row exactly at the window is not (boundary: exactly 24 hours is
  fresh, one second beyond is stale).
- When the broker's `lot` has changed since the row was written, the stale-row
  branch returns the broker's value and the row afterwards holds it (proves the
  window is what lets a lot-size change be picked up without a restart — §2.1's
  GAZP lot of 10 is the case that costs money when it is wrong).
- The row is written inside `db.connection.transaction(critical=False)`, and this
  module issues no `BEGIN`, `commit` or `rollback` of its own (rule 31).
- An `aiosqlite.Error` raised by the write is logged and swallowed and the
  `Instrument` from the live read is still returned; a `TypeError` raised by the
  write **propagates** (rule 12 as narrowed in v1.75).
- With no database open, `get_instrument` returns the `Instrument` the broker
  reported, writes nothing, and no `DatabaseNotOpenError` reaches the caller
  (proves `sandbox/data.py` and `scripts/research/backtest.py` still run with no
  bot database).
- A live status read that fails on a fresh row raises `BrokerUnavailable` and the
  caller receives no `Instrument` (proves a stored status is never substituted for
  a broker that could not be reached).
- Nothing outside this module issues SQL against `instruments`, and this module
  issues SQL against no other table (proves single ownership in both directions).
- Every order submission is asserted to pass `confirm_margin_trade=False`
  (proves the no-leverage guarantee is enforced at the only place it can be
  broken — this test is the executable form of the brief's loss bound).
- `get_order_state` is asserted to pass `ORDER_ID_TYPE_REQUEST` (proves recovery
  looks orders up by the key it still has, not the identifier it lost).

## Expected output

- `zarabot/broker/client.py` implementing the contract exactly
- `tests/test_broker_client.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_broker_client.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/broker/client.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
