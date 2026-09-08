# Proposal: #65 / v1.69 — `/halt` must not call `pnl`

**Kind:** spec contradiction (Stop-and-ask). Adjacent lines in §4 `telegram.commands`:

> `/halt` and `/resume` delegate to `state.halt` and to nothing else.
> **`/halt` passes `daily_loss_pct` (v1.69).** It calls `pnl.daily_loss_pct(clock.now())` …

Those cannot both be true. Implementing the second made the kill switch wait on the broker: `daily_loss_pct` → `bot_equity` → `get_last_price` per open position, and an `alert()` when the opening snapshot is missing. If any of that raises, `persist_halt` never runs and the operator gets no halt and no reply.

This is the same class of defect #90 / v1.69 already excluded for `execution.orders._halt_on_db_failure` (do not call `pnl` when the reason you are halting is that the store `pnl` needs is broken). `/halt` is the control that has to work when the broker does not.

**Should say:**

- Keep: `/halt` and `/resume` delegate to `state.halt` and to nothing else.
- Strike the v1.69 sentence that `/halt` calls `pnl.daily_loss_pct`.
- Optional follow-up, not this module's job: a caller that already has a `Decimal` (for example `app.loops`) may pass it. `/halt` omits the argument. `halt_triggered` then omits `daily_loss_pct` per v1.69's omit-when-absent rule.

If a later amendment wants `/halt` to *try* to attach the figure: catch `Exception` from `daily_loss_pct`, pass `None`, and **always** persist the halt first or regardless. That is a new error-rule decision; do not invent it in `telegram.commands` until it is written.

**Test contract:** `/halt` from the authorised chat persists a `MANUAL` halt even when `pnl.daily_loss_pct` would raise. No broker call on that path.

**Modules to re-run:** `29-telegram-commands` only, after the amendment. This PR ships `unauthorised_command` and leaves `/halt` as `persist_halt(reason, detail, now())` with no fourth argument.

**Do not:** call `pnl` or `broker.client` from `/halt` to satisfy v1.69 as written.
