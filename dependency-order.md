# Dependency Order — Zarabot

**Version:** 1.4
**Derived from:** `technical-spec.md` v1.23
**Versioning:** new version when a module is added, removed, or its dependencies
change.

Build in this order. A module is **complete only when its tests pass** — never
start a dependent module against an untested dependency. After each module
completes, append its public signatures to `interfaces.md`, which the next task
reads.

---

## Layer 1 — Foundation (no internal dependencies)

1. **models** — depends on: (nothing)
2. **clock** — depends on: (nothing)
3. **config** — depends on: (nothing)
4. **logging_setup** — depends on: config, clock
4b. **telegram.notifier** — depends on: config, logging_setup
    *(built as step 28 — see Corrections. It belongs here: it imports nothing
    but `config`, and modules from step 21 onward are contractually required to
    alert.)*

`models` comes first because every other module's signatures are written in its
types. `clock` before everything that touches time, since it is the sole owner
of "now" and every later test injects it.

## Layer 2 — Storage

5. **db.migrations** — depends on: config
5b. **db.connection** — depends on: config
    *(sole owner of the process-wide connection. Numbered after `db.migrations`
    because `apply` is written to receive a connection rather than open one, and
    the two are built in that order.)*
6. **db.positions** — depends on: models, clock, db.migrations
7. **db.orders** — depends on: models, clock, db.migrations
8. **db.stop_orders** — depends on: models, clock, db.migrations
8b. **db.job_runs** — depends on: clock, db.migrations
8c. **db.trading_days** — depends on: models, clock, db.migrations
9. **db.cooldowns** — depends on: clock, db.migrations
10. **db.signals** — depends on: models, db.migrations
11. **db.snapshots** — depends on: models, clock, db.migrations

Repository modules are independent of one another — order among 6–11 (and
8b/8c) is free.
Each owns its own tables exclusively; none reads or writes another's. All of them
run their SQL on `db.connection.shared()`; none opens a connection. So do
`state.halt` (24) and `broker.reconcile` (27), which are not repositories but
were opening their own.

## Layer 3 — Pure logic (no I/O, no clock, no database)

12. **risk.sizing** — depends on: models
13. **lifecycle.exits** — depends on: models
14. **strategies.base** — depends on: models
15. **strategies.ma_crossover** — depends on: models, strategies.base
16. **strategies.rsi_reversion** — depends on: models, strategies.base
17. **strategies.momentum** — depends on: models, strategies.base
18. **strategies.ml_model** — depends on: models, strategies.base, config
19. **strategies.registry** — depends on: config, strategies.*
20. **risk.gate** — depends on: models, risk.sizing

This whole layer needs nothing but `models`, so it can be built immediately
after Layer 1 and in any internal order. It is also the layer worth building
most carefully: these are the modules the backtester shares with the live path,
and every one of them is a pure function with exhaustive test cases. Getting
them right early means the expensive layers above have solid ground.

## Layer 4 — Broker integration

21. **broker.client** — depends on: config, logging_setup, models

The only module that touches the broker network. Everything above it is mocked
at this boundary in tests.

## Layer 5 — Market and state

22. **market.session** — depends on: broker.client, clock, models
23. **market.data** — depends on: broker.client, models, clock
24. **state.halt** — depends on: db.migrations, clock, models
25. **pnl** — depends on: db.positions, db.snapshots, broker.client, models, clock

## Layer 6 — Execution

26. **execution.orders** — depends on: broker.client, db.orders, db.stop_orders,
    db.positions, db.cooldowns, models, clock, logging_setup
27. **broker.reconcile** — depends on: broker.client, db.positions,
    db.stop_orders, models, clock

`execution.orders` is the highest-risk module in the project — it is where money
moves, where the locks live, and where write-then-send ordering is enforced. It
requires 95% coverage. Build it only when everything beneath it is proven.

`broker.reconcile` comes after the executor even though it does not call it:
reconciliation only *reports* discrepancies, and `app.startup` applies the
remedies through the executor. Building it second keeps that separation obvious.

## Layer 7 — Interface and reporting

29. **telegram.commands** — depends on: config, state.halt, db.positions, pnl,
    execution.orders
30. **reporter.weekly** — depends on: pnl, db.positions, db.signals,
    db.snapshots, telegram.notifier
31. **ops.backup** — depends on: config, telegram.notifier
31b. **ops.commissions** — depends on: broker.client, db.orders, db.positions,
    telegram.notifier, clock

## Layer 8 — Orchestration (depends on everything)

32. **app.startup** — depends on: all of the above
33. **app.loops** — depends on: all of the above
34. **app.shutdown** — depends on: execution.orders, db.*, logging_setup
35. **__main__** — depends on: app.startup, app.loops, app.shutdown

## Layer 9 — Research sandbox (never imported by server code)

36. **sandbox.data** — depends on: broker.client, models
37. **sandbox.backtest** — depends on: models, strategies.*, risk.sizing,
    lifecycle.exits
38. **sandbox.train** — depends on: sandbox.data, sandbox.backtest

`sandbox.backtest` imports the Layer 3 pure modules **directly and unchanged**.
It must never reimplement a strategy, a sizing rule, or an exit condition. If it
does, backtests stop being evidence about live behaviour and the whole research
loop becomes decorative.

---

## Corrections

**`telegram.notifier` was placed at step 28 and belongs at step 4b.** It imports
only `config`, so nothing forced it late — the placement came from grouping it
with the other Telegram code rather than from its dependencies.

The cost was not cosmetic. `execution.orders` (26) and `broker.reconcile` (27)
are both contractually required to alert the owner — a failed exit, an
unprotected position, an external close, an orphaned stop — and both were built
before the notifier existed. Each defined a local `_alert()` writing to the error
log instead. The brief's monitoring model is that Telegram is the dashboard and
silence means something is wrong; those alerts were going to stdout on an
unwatched server, and nothing in the build loop was scheduled to come back for
them.

Step numbers below 28 are left as built so they continue to match `tasks/`
filenames and commit history. The lesson generalises: **place a module by what it
imports, not by which directory it lives in.** Anything the safety-critical
modules must call belongs beneath them, whatever it is named.

## Notes on the graph

**No cycles.** Every arrow points down a layer. The one that would tempt you is
`broker.reconcile` needing to place a replacement stop order — that is why the
spec makes reconciliation observational and puts the remedy in `app.startup`.
Letting reconcile call the executor, which calls repositories that reconcile
also owns, would create exactly the cycle this ordering exists to avoid.

**Foundation modules rarely change.** If `models`, `clock` or `config` change
after Layer 1 is done, every dependent module must be re-checked — their types
and time semantics are wired through everything.

**A natural pause point exists after Layer 3.** At that point the entire
decision-making core is built and tested with no I/O anywhere, and the
backtester (36–38) can run against it. Building the sandbox early — out of
numeric order, straight after Layer 4 gives you `broker.client` for data — is a
legitimate deviation, and probably a good one: it lets you see whether the
strategies are worth deploying before building the machinery to deploy them.
