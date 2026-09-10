# Project Rules — Zarabot

You are implementing this project one module at a time. Read this file before
every task. These rules override any conflicting instinct.

**This file is the single rulebook for every agent.** `CLAUDE.md` is a symlink to
it, so Claude Code and Cursor Agent read the same text and it cannot drift.
Directory-level `AGENTS.md` files add rules for the code beneath them; where they
are stricter than this file, the stricter rule wins.

**This project trades real money on a live brokerage account.** A bug here does
not produce a stack trace, it produces a loss. When a rule below seems
excessive, that is why.

**Documents:** `business-brief.md` (what and why) → `technical-spec.md` (module
contracts) → `dependency-order.md` (build sequence) → `interfaces.md` (what
already exists). When the spec and the brief conflict, **the brief wins**.

## Workflow: test-first, always

1. Read the module task, its contract in the spec, and `interfaces.md`.
2. Write the tests FIRST, from the spec's test contracts for that module.
3. Run them — confirm they FAIL. Nothing is implemented yet.
4. Write the implementation to satisfy the contract.
5. Run the tests — iterate until ALL pass and coverage meets the threshold.
6. Append the module's public signatures to `interfaces.md`.
7. Stop. Wait for the next task.

Never write implementation before tests. Never declare done with failing tests.

## Must NEVER

**Money and orders — these cause financial loss, not bugs:**
- Never pass `confirm_margin_trade=True` to any order call. No condition makes
  this correct. It would allow losses exceeding the allocated capital, which is
  the one failure the brief promises cannot happen.
- Never call the broker before the intent is written to the database.
  Write-then-send is what makes a crash mid-submission recoverable.
- Never resubmit an entry order. Recovery is by querying with the idempotency
  key, never by sending again.
- Never let both the exchange and the bot own a position's stop-loss. Ownership
  is recorded in `stop_protection`, never inferred.
- Never sell before cancelling that position's standing stop order.
- Never use `float` for money. `Decimal` end to end.
- Never use the `post_sandbox_*` method family for trading logic. Sandbox is
  selected by endpoint, so the same code path runs in both.

**Correctness:**
- Never use naive datetimes. Everything timezone-aware, UTC internally.
- Never call `datetime.now()` outside `clock` — it is the sole owner of "now".
- Never modify a module other than the one in the current task.
- Never write to another module's tables. Ownership is listed in the spec.
- Never guess a signature — read it from `interfaces.md`.
- Never reimplement something already in `interfaces.md`.
- Never reimplement a strategy, sizing rule or exit condition in the backtester.
  It imports the live modules unchanged, or backtests prove nothing.
- Never invent behaviour absent from the contract.
- Never catch an exception silently — handle it per the spec's numbered rules.

**Hygiene:**
- Never log a token, in a message, a structured field, or an exception.
- Never commit `.env`, a database file, or a model file.
- Never add a dependency not listed in the spec.
- Never skip, weaken, or delete a test to make a suite pass.
- Never estimate commission. It is read from the broker's operations.

## Must ALWAYS

- Match contract signatures exactly — names, parameters, return types,
  including `| None`.
- Handle every failure per the spec's numbered error rules (1–38, plus 9b).
  Rules 30–38 are the ones incidents produced — shared-connection ownership,
  foreign holdings, recorded prices, partial fills, the degraded-state latch.
  Skipping them is how each of those incidents happened the first time.
- Release locks through an async context manager guaranteeing release on
  success, on exception, and on cancellation.
- Keep `strategies.*`, `risk.gate`, `risk.sizing` and `lifecycle.exits` pure —
  no I/O, no database, no clock except a `now` argument.
- Read configuration only through `config`.
- Access the database only through its owning repository module. Every table in
  spec §5 has exactly one owning module, named in that module's §4 contract, and
  that module is the only code that **writes** it. Write no SQL against a table
  you do not own; to change another module's table, call that module.
- **A module may own its own table.** The owner need not live under
  `zarabot/db/`: `state.halt` owns `halt_state` and `broker.reconcile` owns
  `reconciliations`, each the sole writer of a single-purpose table. A
  non-`db.*` owner owes every rule a repository owes — no `aiosqlite.connect`,
  no `BEGIN`/`commit`/`rollback`, and every write inside
  `db.connection.transaction()` on `db.connection.shared()`.
- The rule above is about writes. Reading is unrestricted **within the owning
  module** — `state.halt` reads `halt_state` with its own `SELECT`s — and every
  `db/` repository reads and writes the table it owns by definition.
- **Forward-only files under `migrations/` are the one carve-out.** A migration
  creates and may seed a table it does not own — `001_initial.sql` seeds the
  `halt_state` singleton row — so an owner is the sole writer at runtime, not
  the sole writer that has ever existed.
- Never call `aiosqlite.connect` outside `db.connection`, and never close the
  connection it owns. Repositories run on `db.connection.shared()`.
- Never issue `BEGIN`, `commit` or `rollback` outside `db.connection`. Every
  write runs inside `db.connection.transaction()`, which is reentrant.
- Return an empty list rather than `None` for "nothing found" collections.
- Treat the broker as authoritative whenever it disagrees with local state.

## Conventions

**Code.** Python 3.12+, PEP 8, type hints on every public function, `ruff` for
lint and format, `mypy` clean. All I/O is `async`; no blocking calls on the
event loop. No module-level side effects except `config`.

**Tests.** `pytest` with `pytest-asyncio`, `asyncio_mode = "auto"`. One file per
module: `tests/test_<module>.py`. Database tests get a temporary file database
with migrations applied. No real network calls — mock at `broker.client`,
`telegram.notifier` and `telegram.commands`. Tests must be deterministic: no
wall-clock reliance, no unseeded randomness, no `sleep` to advance time.

**Coverage.** 80% overall; 70% for `app.*` orchestration; **95% for
`risk.gate`, `risk.sizing`, `lifecycle.exits` and `execution.orders`** — a
missed branch in those four is a financial defect, not a coverage statistic.

## File structure

```
zarabot/          models, clock, config, logging_setup, pnl
  db/             migrations, connection, positions, orders, stop_orders,
                  cooldowns, signals, snapshots
  broker/         client, reconcile
  market/         session, data
  strategies/     base, ma_crossover, rsi_reversion, momentum, ml_model, registry
  risk/           sizing, gate
  lifecycle/      exits
  execution/      orders
  state/          halt
  telegram/       notifier, commands
  reporter/       weekly
  ops/            backup
  app/            startup, loops, shutdown
sandbox/          data, backtest, train   (never imported by zarabot/)
scripts/verify/   pre-development verification suite
tests/            one file per module
migrations/       NNN_description.sql, forward-only
vendor/           the broker SDK wheel
```

## Git

- **Two commits per module**, in this order:
  1. `test(<module>): failing tests from spec §3.2` — the tests alone, red.
  2. `Implement <module>: <one-line summary>` — the implementation, green.
  The first commit is the evidence that tests preceded implementation. A single
  combined commit hides the order, leaving only the agent's assurance that
  test-first was followed.
- Never commit with failing tests.
- Never commit secrets or generated artefacts.

## Stop and ask — do not guess — when

- The contract is ambiguous or self-contradictory.
- The contract conflicts with something already in `interfaces.md`.
- A test case cannot be satisfied without violating the contract.
- No error rule covers a failure mode you hit.
- Implementing the module would require changing another module.
- Anything would require relaxing a rule in the **Must NEVER** list.

Stop, explain the conflict, and ask. A wrong guess costs more than a question —
and in this project some wrong guesses cost money.
