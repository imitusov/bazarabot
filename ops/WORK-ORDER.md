# Work order — 29 audit findings

Derived from the open issues. **Do not run 29 separate task cycles.** The
findings cluster onto ten or so modules, and issues touching the same module
must be fixed in one pass — two agents amending `execution.orders` for F-04 and
F-05 separately will conflict, and the second will be written against a contract
the first already changed.

Order below is `dependency-order.md` order. Fix downward, never sideways.

## Closed already

- **#1 F-01** — `migrations/` is copied into the image as of `6f4be32`. Close it.

## Not yet filed

- **`zarabot/broker/client.py:187`** — `mypy` fails on `main`:
  `Argument 1 to "money_to_decimal" has incompatible type "object"`. It is not
  in any issue, and it will fail the new CI gate on its first run.

## Clusters

| Order | Module | Task | Issues | Why together |
|---|---|---|---|---|
| 1 | `config` | `03-config` | F-26, part of F-15 | Both are validation rules; F-15 needs the cross-field check F-26 touches |
| 2 | `db.*` | `06`–`11` | F-20, F-29 | Connection handling and pragmas are one change across every repository |
| 3 | `risk.sizing`, `risk.gate` | `12`, `20` | F-15, F-16, part of F-24 | A limit that can never bind and the missing portfolio ceiling are the same contract |
| 4 | `lifecycle.exits` | `13` | F-06 | Stale-price guard belongs with the trigger it corrupts |
| 5 | `broker.client` | `21` | F-10, F-18, F-23, mypy | Channel reuse, status mapping and error typing all live in the wrapper |
| 6 | `market.session`, `market.data` | `22`, `23` | F-03, F-19, part of F-13 | Caching is inverted in one direction and absent in the other |
| 7 | `execution.orders` | `26` | F-04, F-05, F-10, F-11, F-22, F-28 | **Six issues, one module.** The highest-risk change in the project |
| 8 | `broker.reconcile` | `27` | F-07, F-11 | Adoption and invented prices are the same code path |
| 9 | `pnl` | `25` | F-09 | Daily-loss baseline |
| 10 | `telegram.commands` | `29` | F-02 | Handlers exist; only the composition is missing |
| 11 | `ops.backup`, `ops.commissions` | `31`, `31b` | F-25, F-08 | |
| 12 | `app.*` | `32`, `33`, `34` | F-02, F-03, F-17, F-21, F-24, F-27 | Composition, scheduling and shutdown ordering |
| 13 | `sandbox.*` | `37`, `38` | F-12, F-13, F-14 | Research only — no effect on live trading |

## A spec amendment touching N modules means N task re-runs

In dependency order, lowest first. This is not bookkeeping — it is the rule that
keeps the one-module discipline workable.

v1.17 amended two modules for F-03: `market.session` gained `cache_exhausted`,
and `app.loops` gained the obligation to call it. Re-running only task 33 left
the agent needing a function the spec promised and no code provided. Its three
options were to guess the signature, reimplement it inside `app.loops`, or stop.
Only stopping is correct, and it cost a full task cycle to discover.

`scripts/ci/check_docs.py` now fails when the spec specifies a function that
`interfaces.md` does not record, and names the task to re-run. **After every
spec amendment, run `make check` before dispatching any agent.** The failure is
free; the blocked task cycle is not.

## Sequencing rules

**Blocking first, and by hand.** F-02 and F-03 are two-line wiring fixes with
outsized consequences: without F-02 there is no kill switch, so you have no way
to stop the bot when a later fix goes wrong. Fix F-02 before automating anything
else.

**`execution.orders` last among the live modules.** It carries six issues, needs
95% coverage, and every module beneath it should be correct before it is
rewritten against them.

**Sandbox last.** F-12 says the backtester measures a system that does not
exist. That is true and worth fixing, but it changes no live behaviour, and
fixing it before the live modules means backtesting a system you are about to
change.

## Per cluster

1. Amend `technical-spec.md` for every issue in the cluster, in one edit.
2. `python scripts/make_tasks.py`
3. Re-run that module's task.
4. Contract Critic.
5. Close the issues, referencing the commit.

## Status

Procedure: `ops/RUNBOOK.md`.

| Batch | Modules | Issues | State |
|---|---|---|---|
| 1 | `market.session`, `app.loops` | #2, #3 | **done** — v1.17, `b0d2e4b`, critic clean |
| 2a | `market.session` | #31 | next — one line, same module as batch 1 |
| 2 | `config` | #26, part of #15 | |
| 3 | `db.*` | #20, #29 | |
| 4 | `risk.sizing`, `risk.gate` | #15, #16 | |
| 5 | `lifecycle.exits` | #6 | |
| 6 | `broker.client` | #10, #18, #23 | |
| 7 | `market.session`, `market.data` | #19, #13 | |
| 8 | `execution.orders` | #4, #5, #10, #11, #22, #28 | **six issues — the hard one** |
| 9 | `broker.reconcile` | #7, #11 | |
| 10 | `pnl` | #9 | |
| 11 | `ops.backup`, `ops.commissions` | #8, #25 | |
| 12 | `app.*` | #17, #21, #24, #27 | |
| 13 | `sandbox.*` | #12, #13, #14 | research only |
| 14 | `broker.client`, `pnl` | #30 | coverage ratchet |

Closed: #1 (`6f4be32`), #2 and #3 (`b0d2e4b`).
Opened during the work: #30 (coverage floors), #31 (empty-cache contract gap).
