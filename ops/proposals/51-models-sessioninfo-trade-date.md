# Proposal: #51 `calendar()` must key closed days by date

**Kind:** spec + `models` then `market.session` (and likely `db.trading_days`). Cannot be fixed inside `market.session` alone without guessing a `SessionInfo` field.

**Wrong line:** `calendar()` dedupes on `_sort_key`, which is `datetime.min` for every non-trading day (`start is None`). A fortnight with four weekend days returns one closed day.

**Should say:** `SessionInfo` carries `trade_date: date` (Moscow calendar date) for both open and closed days. `calendar()` uniques on `trade_date`. `db.trading_days` may persist closed days too, or `covers()` is defined only over recorded dates including closures.

**§3.2:**

- 14 fetched days → 14 `calendar().sessions`.
- Overlap of history and live window → that `trade_date` once.
- `clock.trading_days_between` unchanged for the same trading sessions.

**Modules to re-run, lowest first:** `01-models`, `05`/`db.trading_days` if schema changes, `22-market-session`, then any caller tests (`clock`, `app.loops`).

**Stop:** do not hack uniqueness on `id(session)` or fabricate timestamps for closed days.
