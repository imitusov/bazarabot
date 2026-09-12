# Proposal: #117 S-27 — eleven low-severity items (tick list)

Independent; do not one-amend them all.

Spec v1.82 and the same-PR rulebook edit closed nine of the eleven. This file
stays until the residue below is gone, per the README's "delete a proposal once
its amendment is merged" — the amendment is merged, the residue is not.

**Do not:** change V6 to run live. v1.82 states the sandbox-only rule outright
and records that widening it is the owner's decision, not an agent's.

## Landed in spec v1.82 / the rulebook

1. V6 sandbox vs live preamble — "on a live account" struck; V6 carries V2's
   prohibition, with the live-account measurement §2.1 does hold kept separate.
2. Rate-limit headroom — 400× corrected to 200×, with the arithmetic written out.
3. `session_closed` required fields — §7.1 row split; `session_closed` requires
   `trade_date` alone, and a null-valued key is stated not to be a missing field.
4. V11 before V10 — reordered, and §2 now says run order is `run_all.sh`'s
   dependency order rather than the numbering.
5. §6 migration list — ascending, with the cost of the old order recorded.
6. Duplicated supervision sentence — deleted; the backoff bound (1s, doubling,
   300s ceiling, reset on clean return) is written down.
7. Strategy tasks 15–17, 19 — `ma_crossover`, `rsi_reversion`, `momentum` and
   `registry` each have a §4 heading with parameters, lookback and entry
   condition; `make_tasks.py` points each task at its own.
8. (a) and (b) — AGENTS.md says the three floors are per file as well as the
   project total and names the #30 ratchet; `check_coverage.py`'s `KNOWN_BELOW`
   comment no longer calls a stricter floor an exemption.
9. AGENTS.md file tree — `job_runs`, `trading_days`, `ops.commissions`,
   `sandbox.exchange` and `__main__` named; the five
   `check_file_tree.KNOWN_MISSING_FROM_SKETCH` waivers deleted with them.
11. `UNATTRIBUTED` — the reporting half is stated under `telegram.commands` and
    `reporter.weekly`; `execution.orders` keeps only the writing half.

## Not a defect

10. `close_position`'s never-blocked guarantee is pinned, and was when #117 was
    filed: `tests/test_execution_orders.py::test_halt_does_not_block_close`
    (behavioural, halts then closes) and
    `tests/test_state_halt.py::test_halt_does_not_block_exits_or_exit_orders`
    (structural — `assert "is_halted" not in inspect.getsource(close_position)`).
    A future amendment adding a halt check to the exit path reds both. No spec
    edit was made. What is pinned by nothing is the cooldown and risk-limit half
    of the same sentence; neither is read on the exit path today.

## Residue

- **8(c), a code fix, not a spec fix.** `check_coverage.py` never checks that a
  `STRICT_MODULES` path exists, and `check_rulebook.py` compares that set only
  against AGENTS.md. Rename `zarabot/risk/gate.py` and the 95% floor evaporates
  with no FAIL. The gap is now written into `check_coverage.py`'s docstring; the
  fix is an existence assertion, one session, no spec change.
- **The `/strategies` sentinel case is owed a test.** §3.2 `telegram.commands`
  carries it as of v1.82 and says in the case itself that it was unwritten when
  v1.82 was issued. `reporter.weekly`'s half is already pinned by
  `test_recovered_entry_without_a_signal_is_unattributed`.
- **V12 is in no §2 paragraph.** `scripts/verify/verify_operations.py` exists,
  §2.1 cites it three times as the source of the operations-feed measurements,
  and §2 defines V1–V11 only; `run_all.sh` does not run it, deliberately (its
  SELL check fails until the account has sold once, #44). Spotted while fixing
  item 4 and left alone: it is a twelfth item, not one of the eleven.
- **`tests/test_export_health.py::test_known_events_matches_the_spec_71_table`**
  still offers `session_open` / `session_closed` as its example of a two-name
  row in its docstring. The assertion is over the name set and passes either
  way; `backup_ok` / `backup_failed` is the surviving example. A one-line
  docstring fix, in a test file, for whoever next touches that file.
