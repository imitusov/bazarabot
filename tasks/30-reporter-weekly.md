# Task 30/42: Implement `zarabot/reporter/weekly.py`

## Product context

The Sunday report. Undefined metrics are reported as not applicable, never as zero.

## Build order position

Module **30** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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
| `exit_commission` | TEXT NULL | Decimal string. Set only for an `EXTERNAL` close, where there is no closing order row to carry it |
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

### `signals`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `ticker` | TEXT NOT NULL | |
| `strategy` | TEXT NOT NULL | |
| `generated_at` | TEXT NOT NULL | UTC |
| `reference_price` | TEXT NOT NULL | Price the strategy saw |
| `decision` | TEXT NOT NULL | CHECK IN (`APPROVED`, `REJECTED`) |
| `rejection_reason` | TEXT NULL | Non-null exactly when `decision = 'REJECTED'` |
| `lots` | INTEGER NULL | Non-null exactly when approved |
| `order_key` | TEXT NULL | FK → `orders(key)` when an order followed |

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

### `zarabot/reporter/weekly.py`

**`async build(start: date, end: date) → str`**
- Composes the report: P&L against benchmark, per-strategy performance, win rate,
  worst trade, exit-trigger distribution, cooldown-blocked signal count, and
  intended-versus-actual exit price for gapped exits.
- A week with no closed trades produces a valid report saying so.
- Undefined metrics are reported as not applicable, never as zero.
- **The per-strategy section groups by the `strategy` value stored on the
  position, not by the enabled set (v1.82).** This module owns half of rule 35's
  reporting: a sentinel — `UNATTRIBUTED` from crash recovery, `ADOPTED` from
  reconciliation — therefore appears **under a heading of its own**, with its own
  P&L and trade count, and is never added to a named strategy's figures. Both
  directions matter. Crediting it to a real strategy is the systematic bias rule
  35 exists to prevent, and filtering it out so the section only lists enabled
  strategies is the other way to break the rule: the week's totals would then
  disagree with the sum of its per-strategy lines, and a trade the bot did not
  decide on would be invisible in the one document the owner reads weekly.
  `telegram.commands` owns the other half, and the writing half is
  `execution.orders`'.
- Over the length limit, sections are dropped in this order — exit-trigger
  distribution, cooldown counts, worst trade — and the omission is noted.

**`async send(now: datetime) → None`** — builds and sends; failure alerts but does not raise.
- **After `alert` returns, emit `weekly_report_built` (INFO) with
  `period_start` and `period_end` (v1.61, renamed from `weekly_report_sent`
  in v1.72).** The event names what this module can observe: the report was
  composed and handed to the notifier. Rule 13 makes a Telegram send failure
  retry, log and never propagate, so `alert` returning is not evidence of
  delivery and this module must not claim it. It therefore fires even when the
  notifier swallowed a send failure, and it is never emitted when `build` or
  `alert` raises. Delivery is observed by the **absence** of
  `telegram_send_failed`, not by the presence of this event; `reporter.weekly`
  neither receives nor infers a send-success flag.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

12. **Database write failure on a non-critical path** (signals, snapshots,
    instruments cache) → ERROR to stdout only, never propagated. Losing an
    analytics row must not stop trading.

    **Non-propagation covers `aiosqlite.Error` and only `aiosqlite.Error`
    (v1.75)** The swallow exists for a database that will not take the row, not
    for every way the call site can be wrong. Any other exception propagates and
    reaches rule 21's supervisor with its traceback. Unqualified, this rule reads
    as `except Exception: pass` on the analytics path, and an analytics path is
    exactly where a silently dropped `TypeError` survives longest — nothing
    downstream misses the row until a weekly report is composed from it.

13. **Telegram send failure** → retry, then log. **Never propagates.** Telegram
    being down never delays or blocks a trading decision.

    **A send failure is `telegram.error.TelegramError`, and only that (v1.75)**
    Every failure the library reports — `NetworkError` and its `TimedOut`,
    `RetryAfter`, `BadRequest`, `Forbidden`, `InvalidToken` — is a subclass of
    it, so the class covers a chat misconfigured as completely as a network that
    is down, and both are conditions the caller can do nothing about mid-trade.
    **Retry is for the transport failures only** — `telegram.error.NetworkError`
    (including `TimedOut`) and `telegram.error.RetryAfter`. The rest are settings
    that will be just as wrong on the third attempt: they are logged once, not
    retried three times. **Any other exception propagates** to the caller and
    thence to rule 21.

    That last clause is the point of the amendment and it is deliberately
    uncomfortable, because `alert()` is called from inside other modules' `except`
    blocks: a defect in the notifier will now surface there rather than be
    absorbed. It has to. `telegram.notifier` carries every alert this system
    sends, and an unqualified "never propagates" means a rename inside it turns
    the whole alerting channel silent while every module believes it has spoken —
    the one degraded state with no external symptom at all (#107, failure class
    5). `ruff`'s `BLE001` is the mechanical companion to this rule and is **not
    enabled** in `pyproject.toml`; enabling it belongs with the code change that
    implements this narrowing, because `telegram/notifier.py`, `ops/backup.py`
    and `app/startup.py` all catch bare `Exception` today.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A week with trades produces a report containing every documented section
  (happy path).
- A week with no trades produces a valid report stating so (proves the empty
  path, which is otherwise a division-by-zero waiting to happen).
- Win rate with zero closed trades is reported as not applicable, not as zero
  (proves the undefined-metric path).
- A report exceeding the message limit drops the least important section and
  notes the omission (proves the documented trimming order).
- The event emitted after the send is `weekly_report_built` with `period_start`
  and `period_end` (v1.61, renamed v1.72). It fires once `alert` has returned,
  including when the notifier swallowed a send failure under rule 13 — the
  module witnesses composition and hand-off, never delivery. Delivery is
  `telegram_send_failed` **absent**, not `weekly_report_built` **present**.

## Expected output

- `zarabot/reporter/weekly.py` implementing the contract exactly
- `tests/test_reporter_weekly.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_reporter_weekly.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/reporter/weekly.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
