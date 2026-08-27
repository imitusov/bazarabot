# Task 25/40: Implement `zarabot/pnl.py`

## Product context

Realised and unrealised P&L, the daily loss percentage, and the buy-and-hold benchmark. Commission is read from the broker, never estimated.

## Build order position

Module **25** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Database tables used

### `positions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ticker` | TEXT NOT NULL | |
| `figi` | TEXT NOT NULL | |
| `strategy` | TEXT NOT NULL | Name of the strategy that opened it. `ADOPTED` for reconciled holdings |
| `lots` | INTEGER NOT NULL | CHECK > 0 |
| `lot_size` | INTEGER NOT NULL | Units per lot at entry time |
| `entry_price` | TEXT NOT NULL | Decimal string, per unit |
| `entry_at` | TEXT NOT NULL | UTC. Age is counted from here |
| `stop_price` | TEXT NOT NULL | Computed at entry, stored — never recomputed from config later |
| `target_price` | TEXT NOT NULL | Same |
| `status` | TEXT NOT NULL | CHECK IN (`OPEN`, `CLOSED`) |
| `adopted` | INTEGER NOT NULL | Default 0. 1 when created by reconciliation |
| `open_order_key` | TEXT NOT NULL | FK → `orders(key)` |
| `close_order_key` | TEXT NULL | FK → `orders(key)`. Null while open |
| `exit_trigger` | TEXT NULL | CHECK IN (`STOP_LOSS`, `TAKE_PROFIT`, `MAX_AGE`, `EXTERNAL`) |
| `exit_price` | TEXT NULL | |
| `exit_at` | TEXT NULL | UTC |
| `realised_pnl` | TEXT NULL | Net of commission, actual not estimated |
| `stop_protection` | TEXT NOT NULL | CHECK IN (`EXCHANGE`, `LOCAL`). Which side owns the stop trigger |
| `stop_order_key` | TEXT NULL | FK → `stop_orders(key)`. Null only when `stop_protection = 'LOCAL'` |

**Invariants.**
- A partial unique index over `ticker` where `status = 'OPEN'` enforces at most
  one open position per instrument.
- `status = 'CLOSED'` requires `exit_trigger`, `exit_price`, `exit_at` and
  `realised_pnl` all non-null; `status = 'OPEN'` requires all four null.
- Exactly one of the two stop owners is active: `stop_protection = 'EXCHANGE'`
  requires a live `stop_order_key`; `'LOCAL'` requires none. This is the
  invariant that prevents a position being sold twice.
- `stop_price` and `target_price` are frozen at entry. Changing `STOP_LOSS_PCT`
  in configuration must never move the stop of an already-open position.
- Rows are never deleted.

### `daily_snapshots`

| Column | Type | Notes |
|---|---|---|
| `trade_date` | TEXT | Primary key. **Moscow** calendar date |
| `opening_equity` | TEXT NOT NULL | Baseline for the daily loss limit |
| `closing_equity` | TEXT NULL | Null until the session closes |
| `cash` | TEXT NOT NULL | |
| `realised_pnl` | TEXT NOT NULL | For the day |
| `unrealised_pnl` | TEXT NOT NULL | At snapshot time |
| `open_positions` | INTEGER NOT NULL | |
| `orders_placed` | INTEGER NOT NULL | Observational only — there is no daily cap |
| `benchmark_value` | TEXT NULL | Null when unavailable, never 0 |

## Module contract

### `zarabot/pnl.py`

**`realised(position: Position) → Decimal`** · **`unrealised(position: Position, price: Decimal) → Decimal`** — both net of commission.

Commission is the **actual figure reported by the broker** via
`broker.client.get_operations`, recorded on the order row when the order settles.
It is never estimated from a rate. On a small account, commission is a
material fraction of a 10% move, and an estimated figure would make every
realised P&L slightly and permanently wrong.

**`async bot_equity() → Decimal`**
- `allocated_capital + realised P&L of every closed position + unrealised P&L of
  every open position at current prices`.
- **Never reads broker cash or broker equity.** That is the whole point: the
  broker's equity moves when money is paid in or taken out, and those movements
  are not trading results. Reading them made a withdrawal look like a loss large
  enough to halt trading, and a deposit mask a real one (#9).

**`async daily_loss_pct(now: datetime) → Decimal`**
- `(opening bot equity − bot equity now) / ALLOCATED_CAPITAL × 100`. Positive
  means a loss.
- **The denominator is allocated capital**, the money actually at risk — not
  account equity. On an account holding twice the allocation, dividing by equity
  let a "5% daily limit" permit a 10% loss of the capital the bot was given
  (#9). `DAILY_LOSS_LIMIT_PCT` now means what an operator reads it to mean:
  a percentage of what they handed the bot.
- **The baseline is bot equity at the session open**, written to
  `daily_snapshots.opening_equity` when the session opens rather than lazily on
  whichever call happened to be first. A process that started at 14:00 previously
  seeded the baseline at 14:00 and was structurally blind to the morning's
  drawdown, and returned zero on the call that established the day — so the limit
  could not trip on the cycle that created it.
- **When no snapshot exists for the day** — the bot started mid-session and
  missed the open — the baseline is reconstructed as
  `allocated_capital + realised P&L of every position closed before today`, and
  the reconstruction is alerted once. It is not exact: unrealised movement on
  positions carried overnight is attributed to today. That direction is
  deliberate, because it makes the limit tighter rather than looser, and a limit
  that halts early is recoverable by `/resume` while one that halts late is not.

**Interaction with an existing halt.** `state.halt.halt()` returns early when
already halted, so a `DAILY_LOSS_LIMIT` breach arriving during a `MANUAL` halt
was discarded — the more serious reason and its detail lost. A halt reason of
strictly greater severity must replace a weaker one and re-alert;
`DAILY_LOSS_LIMIT` outranks `MANUAL` and `RECONCILIATION_MISMATCH`. **`halted_at`
keeps its original value across an upgrade**: trading has been suspended
continuously since the first halt, and moving the timestamp forward would assert
it was live in between. The moment the more severe condition arrived reaches the
owner in the alert. Re-halting
for a reason already recorded stays a no-op, so this adds no alert noise.

**`async benchmark_return(start: date, end: date) → Decimal | None`**
- Buy-and-hold return over the watchlist for the period.
- Returns `None` when any constituent price is missing — an unavailable benchmark
  is reported as unavailable, never as zero.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

20. **Daily loss limit breached** → halt, persist the halt, alert with the loss
    and the trades that produced it. Exits continue to run.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Realised P&L for a closed position matches the arithmetic including commission
  (happy path).
- Unrealised P&L for an open position uses the current price (happy path).
- The daily loss percentage divides by `ALLOCATED_CAPITAL`, not by account
  equity: the same rouble loss on an account holding twice the allocation gives
  the same percentage (proves the limit means a share of the money at risk, #9).
- A cash withdrawal between two calls does not change the daily loss percentage
  (proves broker equity is never read, so a transfer cannot read as a trading
  result — the failure that could halt trading for moving money).
- With no snapshot for the day, the baseline is reconstructed from realised P&L
  before today and the reconstruction is alerted (proves a mid-session start is
  not silently blind to the morning).
- `bot_equity` counts allocated capital plus realised plus unrealised, and is
  unchanged by a deposit.
- With no positions and no trades, all figures are zero rather than `None`
  (proves the empty-portfolio path).
- The buy-and-hold benchmark over a window with a missing price for one
  instrument reports the benchmark as unavailable rather than as zero (proves
  missing data is not silently treated as no return).

## Expected output

- `zarabot/pnl.py` implementing the contract exactly
- `tests/test_pnl.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_pnl.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/pnl.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
