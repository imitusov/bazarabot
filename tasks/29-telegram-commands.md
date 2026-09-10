# Task 29/42: Implement `zarabot/telegram/commands.py`

## Product context

The entire user interface. One authorised chat id; everything else is ignored and logged.

## Build order position

Module **29** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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

## Module contract

### `zarabot/telegram/commands.py`

One handler per command in the brief's command table.

- Every handler first checks the sender against `TELEGRAM_CHAT_ID`; a mismatch
  emits `unauthorised_command` (INFO) with `chat_id` and `command` (v1.61), then
  returns without replying and without any state change. `chat_id` is not a
  brokerage secret; it is the field that makes the event answerable. **This is
  rule 14 (v1.75)**, and this module owns it: no reply of any kind, not even a
  refusal, because a refusal confirms the bot exists to whoever sent the message.
- Replies exceeding the platform limit are truncated with an explicit note of how
  many entries were omitted.
- No handler mutates a risk limit.
- `/halt` and `/resume` delegate to `state.halt` and to nothing else.
- **`/halt` passes no `daily_loss_pct` (v1.71, reversing v1.69).** It calls
  `state.halt.halt(reason, detail, at)` and nothing else. The reasoning is under
  `state.halt`'s heading with the other two call sites. Note that v1.69's
  obligation contradicted the line directly above it — `/halt` cannot delegate
  "to `state.halt` and to nothing else" while calling `pnl` — and that line is
  correct as written; it is v1.69 that was wrong. `/status` already reports the
  day's position on demand, so the figure remains one command away.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

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

14. **Telegram command from an unauthorised chat** → INFO log with the chat
    identifier, no reply, no state change.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Each command from the authorised chat returns its documented content (happy
  path per command).
- Any command from an unauthorised chat identifier returns nothing, emits
  `unauthorised_command` with `chat_id` and `command`, and performs no state
  change (v1.61; proves the single security boundary).
- `/resume` when not halted replies that nothing was halted (proves the
  no-op path).
- A response exceeding the message limit is truncated with an explicit note
  naming how many entries were omitted (proves the truncation contract).
- No command mutates a risk limit (proves the brief's prohibition).
- **`/halt` halts with the broker unreachable** — every `broker.client` call
  raising `BrokerUnavailable`, the halt is still persisted and the reply still
  sent (v1.71; proves the kill switch does not depend on the system it stops
  trading against. Written as a caller-shaped test: drive `/halt`, do not stub
  `state.halt`. A test that stubbed the halt would pass with the broker call
  still in place).

## Expected output

- `zarabot/telegram/commands.py` implementing the contract exactly
- `tests/test_telegram_commands.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_telegram_commands.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/telegram/commands.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
