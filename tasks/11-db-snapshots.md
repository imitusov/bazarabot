# Task 11/42: Implement `zarabot/db/snapshots.py`

## Product context

Daily equity snapshots. The opening baseline is what the daily loss limit measures against.

## Build order position

Module **11** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `daily_snapshots`

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. **Moscow** calendar date |
| `opening_equity` | TEXT NOT NULL | Baseline for the daily loss limit. Written once, by the day's opening write, and never rewritten |
| `closing_equity` | TEXT NULL | Bot equity at the most recent in-session cycle; the last write of a trading day **is** that day's close. Null only for a date whose row no in-session cycle updated |
| `cash` | TEXT NOT NULL | The bot's **uninvested** money: `closing_equity` minus the market value of open positions at the same prices `unrealised_pnl` uses, so `cash` plus that market value is always exactly `closing_equity`. In words: allocated capital plus realised results, less what the open positions cost. **Never the broker's cash balance** — a deposit or a withdrawal is not a trading result, and a row whose equity is bot-scoped and whose cash is account-scoped is the unit mismatch that produced #9. Until v1.86 this column held a second copy of bot equity |
| `realised_pnl` | TEXT NOT NULL | For the day: `Σ pnl.realised` over positions whose `exit_at` is on this Moscow date |
| `unrealised_pnl` | TEXT NOT NULL | Open positions marked to market at the prices of the cycle that last wrote the row |
| `open_positions` | INTEGER NOT NULL | Open positions at that same cycle |
| `orders_placed` | INTEGER NOT NULL | Observational only — there is no daily cap. Every order recorded on this Moscow date, whatever its status: how much the bot tried to do, not how much filled |
| `benchmark_value` | TEXT NULL | Null when unavailable, never 0. **Nothing writes it today** — see the open decision under §4 `zarabot/db/snapshots.py` |

Every column but `opening_equity` and `benchmark_value` is refreshed on every
in-session cycle by `app.loops.trading_cycle` step 4, through
`db.snapshots.update_intraday` (v1.86, #17). Until v1.86 the whole row was
written once at the session open with `closing_equity` null and four figures
hardcoded to zero, and nothing ever revisited it: there was no equity curve,
so no drawdown, volatility or risk-adjusted return could be computed after the
fact, and `/status` answered `Today's P&L: 0.00` and `Orders placed today: 0`
with confidence. **No migration is required** — `001_initial.sql` already
creates all nine columns, and v1.86 changes who writes them and when, not what
they are.

## Module contract

### `zarabot/db/snapshots.py`

**Sole owner of `daily_snapshots` rows.**

Must not call `aiosqlite.connect` and must not close the connection it uses. All
SQL runs on `db.connection.shared()`; a private connection is a contract
violation. **Every write runs inside `db.connection.transaction()`**; this module
never issues `BEGIN`, `commit` or `rollback` itself, and holds no write lock of
its own (rule 31).

**`async write_daily(snapshot: DailySnapshot) → None`** — upserts on the Moscow date; a second write for the same date updates rather than duplicates.

**`async list_for_period(start: date, end: date) → list[DailySnapshot]`** — rows
whose `trade_date` falls in `[start, end]`, oldest first. For the weekly report.
Empty list when none.

**`async update_intraday(trade_date: date, closing_equity: Decimal, cash: Decimal, realised_pnl: Decimal, unrealised_pnl: Decimal, open_positions: int, orders_placed: int) → None`**
— updates exactly those six columns on the row for `trade_date`, and **does
nothing when that date has no row**. Called by `app.loops.trading_cycle` step 4
on every in-session cycle; the last call of a trading day is what makes
`closing_equity` the day's close (v1.86, #17). Write failures are rule 12 like
every other write here — `aiosqlite.Error` logged at ERROR and swallowed,
everything else propagating — because a lost curve point must not stop trading.

**`opening_equity`, `trade_date` and `benchmark_value` are not parameters, and
that is the point.** The baseline is written once, by the opening `write_daily`,
and a function that cannot express a change to it is the only thing that keeps
that true across every future edit of the caller: `pnl.daily_loss_pct` measures
the day against `opening_equity`, so a caller able to reseed it could erase the
morning's drawdown from the limit that exists to catch it (#9). Creating a row
is likewise out of reach here, because a mid-session process is forbidden to
invent an opening figure (`app.loops` step 4). `benchmark_value` is excluded
because nothing produces it — see the open decision below.

**`check_docs.py` correctly reds on this signature until
`tasks/11-db-snapshots.md` is re-run**, as it does on
`db.orders.count_for_day`. The red names the task to run next. Clear it by
implementing the function — never with an allowlist entry, and never by
recording in `interfaces.md` a function no code provides, which would turn the
one check that catches an under-counted amendment (failure class 1) into a
check that cannot.

**Owns rule 12 (v1.75, split v1.76).** Signals, snapshots and the instruments
cache are the non-critical write paths: a failed write here is logged at ERROR
and does not propagate, because losing an analytics row must not stop trading.
The swallow is `aiosqlite.Error` and nothing wider — every other exception
propagates and reaches rule 21's supervisor with its traceback. An analytics
path is where a silently dropped `TypeError` survives longest, since nothing
downstream misses the row until a weekly report is composed without it. Contrast
`db.cooldowns` above, which is rule 11 and propagates everything.

**Open decision — `benchmark_value` (v1.86). Not settled here.** The column is
named for a value and no function in the system produces one:
`pnl.benchmark_return(start, end)` returns a **return over a period**, and
`reporter.weekly` recomputes it live from candles on every report, which is why
a historical benchmark comparison cannot be reconstructed today (#17). Three
answers, none of them free:

- (a) **Define it as a level.** `allocated_capital × (1 + benchmark_return(first
  trading day, this date))` is comparable with `closing_equity` on the same
  axis, and is the answer that makes the equity curve mean something. It cannot
  be computed on the trading cycle: `benchmark_return` fetches daily candles for
  every watchlist ticker, so a per-cycle call is one request per ticker per
  minute for a number that moves once a day. It therefore needs a once-a-day
  producer — the rollover job is the natural home, it is already "due and not
  yet done" against `db.job_runs`, and it would write **yesterday's** row, which
  is legitimate for this column precisely because a benchmark level does not
  need yesterday's prices from *our* store. That is a second writer of the row
  and a seventh parameter here, and it is a design, not a detail.
- (b) **Redefine it as a return** and rename the §5 note accordingly. Cheap in
  documentation, but the column name then lies in the database forever, and a
  return is what `reporter.weekly` already recomputes, so the column buys only
  the history.
- (c) **Drop the column** in a forward migration, and with it the
  `DailySnapshot` field and the `_row_to_snapshot` branch.

**Until it is decided, nothing writes it and it stays `NULL`.** That is what
`write_daily` already records at the opening write and what `update_intraday`
above is deliberately unable to change, and §5 already permits null. This is not
a defect to be fixed by an agent reading this paragraph: no agent adds a
benchmark producer on its own reading of it. The equity curve #17 is about does
not depend on this column — `closing_equity`, `realised_pnl`, `unrealised_pnl`
and `open_positions` are the curve, and they are settled above.

**Why this heading was split (v1.76).** Until v1.76 these two modules shared one
`###` heading, and `list_for_period` was written once as
`→ list[...]` because the elision was standing for two different real return
types — `list[tuple[Signal, RiskDecision]]` here and `list[DailySnapshot]` there
(#116). One heading cannot carry two signatures of the same name: the signature
comparison in `scripts/ci/check_docs.py` reads the last one and compares it
against both modules, so one of the two was guaranteed to be wrong and the
divergence lived in an allowlist instead. Splitting the heading is what makes
each signature exact, which is what `AGENTS.md` requires. `make_tasks.py` needs
no change: its spec-keys are already the two distinct file paths.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

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

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A rejected signal is stored with its rejection reason and is retrievable by day
  (proves rejections are analysable, as the brief requires).
- A daily snapshot written twice for the same date updates rather than duplicates
  (proves the date is the key).
- `update_intraday` on an existing row changes `closing_equity`, `cash`,
  `realised_pnl`, `unrealised_pnl`, `open_positions` and `orders_placed`, and
  leaves `opening_equity` and `benchmark_value` exactly as they were (proves the
  baseline the daily loss limit measures against survives every intraday write —
  the row was written once and never revisited, so the alternative was never
  exercised).
- `update_intraday` for a date with no row creates nothing: `list_for_period`
  over that date is still empty afterwards, and no exception is raised (proves a
  mid-session process cannot invent an opening figure through the update path).

## Expected output

- `zarabot/db/snapshots.py` implementing the contract exactly
- `tests/test_db_snapshots.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_db_snapshots.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/db/snapshots.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
