# Proposal: #59 `halt(..., daily_loss_pct)` cannot land in `state.halt` alone

**Kind:** spec sequencing (or a signature amendment). Hold a `state.halt` implementation of O-05 until this is settled.

**Wrong line** (`technical-spec.md` §4 `state.halt`, v1.61):

> **`async halt(reason: HaltReason, detail: str, at: datetime, daily_loss_pct: Decimal) → None`**
> … `daily_loss_pct` is a required argument of `halt` (`Decimal`); callers that already computed the day's loss pass it, and `/halt` passes the current figure from `pnl`.

§7.1 requires `halt_triggered` to carry `daily_loss_pct`. That field is load-bearing. The argument is how the producer is supposed to know it.

**Why it cannot be implemented in this module's session.** `AGENTS.md` forbids modifying another module to make this one work. Three live callers still use the three-argument form recorded in `interfaces.md`:

| Caller | Call |
|---|---|
| `zarabot/app/loops.py` | `halt(HaltReason.DAILY_LOSS_LIMIT, detail, moment)` — already has the day's loss in `detail`, does not pass a `Decimal` |
| `zarabot/telegram/commands.py` | `persist_halt(HaltReason.MANUAL, "manual halt via /halt", now())` — would need `pnl.daily_loss_pct` |
| `zarabot/execution/orders.py` | `halt(HaltReason.MANUAL, detail, clock_now())` |

Adding the required argument in `state.halt` alone makes `make check` red in those modules and in `tests/test_app_startup.py`, `tests/test_execution_orders.py`, `tests/test_db_connection.py`. That is not a test to weaken; it is the one-module rule firing.

`halt` must not call `pnl.daily_loss_pct` itself: `pnl` is a later module, and `halt` taking a clock `at` then asking `pnl` for "now" would mix two clocks.

**Do not:** default `daily_loss_pct=Decimal("0")` so callers keep compiling. A constant zero on `/halt` and on an execution-path halt is the same shape as `backoff_seconds: 0` / hardcoded `critical` — a required field that does not answer the question.

**Should say** (pick one; do not leave both live):

1. **Keep the required argument, and name the callers.** `app.loops`, `telegram.commands`, and `execution.orders` pass `daily_loss_pct` in *their* sessions (loops already computed the daily-loss halt; `/halt` and the execution-path halt call `pnl.daily_loss_pct(at)`). O-05 in `state.halt` waits until that amendment is scheduled, or those three land in the same batch as O-05 with an explicit exception to one-module.

2. **Keep the three-argument `halt`**, and say `halt_triggered.daily_loss_pct` is `None` on a `MANUAL` halt that did not receive a figure, and is passed only when the caller has one — which requires a signature the callers do not have. This option is worse; prefer (1).

**§3.2** (once the signature is callable):

- A persisted or upgraded halt emits `halt_triggered` CRITICAL with `reason`, `detail`, and the `daily_loss_pct` argument (not a value this module computed).
- A same-or-weaker re-halt emits nothing.
- `resume` of a halted process emits `halt_cleared` INFO with `actor`; a no-op resume emits nothing.

**Modules to re-run, lowest first:** `24-state-halt` (events + argument), then `32-execution-orders`, `33-telegram-commands`, `38-app-loops` for the call sites. Tests of those modules that call `halt(...)` update in the same sessions.

**Stop:** do not add the fourth argument in `state.halt` until the spec names who passes it, or until one-module is waived for those three call sites.
