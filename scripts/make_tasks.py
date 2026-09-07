#!/usr/bin/env python3
"""Generate one task file per module from technical-spec.md.

Task files are DERIVED, never hand-edited: re-run this after any spec change
so the tasks an agent is fed cannot drift from the contracts they implement.

    python3 scripts/make_tasks.py
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = (ROOT / "technical-spec.md").read_text(encoding="utf-8")
OUT = ROOT / "tasks"

# (order, module, spec heading fragment, test-contract key, tables, error rules, context)
M = [
 (1,"models","zarabot/models.py","`models`",[],[22],"Shared domain types. Every other module's signatures are written in these types, so this is the vocabulary the whole system speaks."),
 (2,"clock","zarabot/clock.py","`clock`",[],[22],"Sole owner of 'now' and of trading-day arithmetic. Everything else receives time rather than reading it, which is what makes the system testable and backtestable."),
 (3,"config","zarabot/config.py","`config`",[],[15,32],"Loads and validates every setting once at startup. The bot refuses to start rather than trade on an assumed risk limit."),
 (4,"logging_setup","zarabot/logging_setup.py","`logging_setup`",[],[19],"Structured logging with secret redaction. A leaked token in a log file is equivalent to a leaked brokerage password."),
 (5,"db.migrations","zarabot/db/migrations.py","`db.migrations`",["schema_version"],[11,16],"Schema creation and version tracking. Forward-only: a bad migration is fixed by a new one, never by rolling back a database holding real trade history."),
 ("5b","db.connection","zarabot/db/connection.py","`db.connection`",[],[11,30],"Sole owner of the process-wide SQLite connection. Opened by app.startup, closed by app.shutdown, never at import. Repositories, state.halt and broker.reconcile all run their SQL on it and none opens its own."),
 (6,"db.positions","zarabot/db/positions.py","`db.positions`",["positions","position_events"],[11,12,30],"Sole owner of position rows. Positions are never deleted; closing is a state transition, because the history is the point of the project."),
 (7,"db.orders","zarabot/db/orders.py","`db.orders`",["orders"],[5,11,12,30],"Sole owner of order rows. Records intent BEFORE the broker is called, which is what makes a crash mid-submission recoverable."),
 (8,"db.stop_orders","zarabot/db/stop_orders.py",None,["stop_orders"],[11,12,30],"Sole owner of stop-order rows. Tracks the standing stop the exchange holds for each open position."),
 (9,"db.cooldowns","zarabot/db/cooldowns.py","`db.cooldowns`",["cooldowns"],[12,30],"Sole owner of per-instrument re-entry cooldowns, which replace a daily order cap as the runaway-loop protection."),
 (10,"db.signals","zarabot/db/signals.py","`db.signals` / `db.snapshots`",["signals"],[12,30],"Records every signal with its risk decision, approved or rejected. Rejections are analysed in the weekly report."),
 (11,"db.snapshots","zarabot/db/snapshots.py","`db.signals` / `db.snapshots`",["daily_snapshots"],[12,30],"Daily equity snapshots. The opening baseline is what the daily loss limit measures against."),
 (12,"risk.sizing","zarabot/risk/sizing.py","`risk.sizing`",[],[],"Pure. Converts a price and a budget into whole lots, always rounding down. 95% coverage."),
 (13,"lifecycle.exits","zarabot/lifecycle/exits.py","`lifecycle.exits`",[],[],"Pure. Decides which of the three exit triggers fires. Returns STOP_LOSS only for LOCAL-protected positions. 95% coverage."),
 (14,"strategies.base","zarabot/strategies/base.py","`strategies.*`",[],[],"The Strategy protocol. Pure, entry-only, deterministic - the properties that let the backtester share this code with the live path."),
 (15,"strategies.ma_crossover","zarabot/strategies/base.py","`strategies.*`",[],[],"Moving-average crossover entries. Pure function over candles."),
 (16,"strategies.rsi_reversion","zarabot/strategies/base.py","`strategies.*`",[],[],"RSI mean-reversion entries. Pure function over candles."),
 (17,"strategies.momentum","zarabot/strategies/base.py","`strategies.*`",[],[],"Momentum breakout entries. Pure function over candles."),
 (18,"strategies.ml_model","zarabot/strategies/ml_model.py","`strategies.*`",[],[17],"Optional ML strategy, disabled unless a model file is configured. Fails loudly at startup, never mid-session."),
 (19,"strategies.registry","zarabot/strategies/base.py","`strategies.*`",[],[],"Builds the active strategy set from configuration."),
 (20,"risk.gate","zarabot/risk/gate.py","`risk.gate`",[],[],"Pure. Every entry check, with a fixed rejection priority so the recorded reason is deterministic. 95% coverage."),
 (21,"broker.client","zarabot/broker/client.py","`broker.client`",[],[1,2,3,4,5,19,28,33],"The ONLY module that talks to the broker. Wraps t_tech.invest.AsyncClient and returns domain types. Run the verification suite before building this."),
 (22,"market.session","zarabot/market/session.py","`market.session`",[],[10],"Is the exchange open? Queried from the broker calendar, never hardcoded. Defaults to closed when unknown."),
 (23,"market.data","zarabot/market/data.py","`market.data`",[],[1,9,36],"Candles for the watchlist. One failing instrument must never blind the bot to the rest, and one that fails persistently must never do so in silence."),
 (24,"state.halt","zarabot/state/halt.py","`state.halt`",["halt_state"],[20,30],"Sole owner of the halt flag. A halt suspends ENTRIES ONLY - exits keep running, and the halt survives restarts."),
 (25,"pnl","zarabot/pnl.py","`pnl`",["positions","daily_snapshots"],[12,20],"Realised and unrealised P&L, the daily loss percentage, and the buy-and-hold benchmark. Commission is read from the broker, never estimated."),
 (26,"execution.orders","zarabot/execution/orders.py","`execution.orders`",["positions","orders","stop_orders","cooldowns"],[3,4,5,11,23,26,27,28,33],"Where money moves. Write-then-send ordering, the submission locks, crash recovery, and the standing stop-loss. Highest-risk module in the project. 95% coverage."),
 (27,"broker.reconcile","zarabot/broker/reconcile.py","`broker.reconcile`",["positions","stop_orders","reconciliations"],[6,7,24,25,30,32],"Compares broker truth against local belief on startup. Observes and reports only - it never places or cancels an order."),
 (28,"telegram.notifier","zarabot/telegram/notifier.py","`telegram.notifier`",[],[13,19],"Pushes alerts. Never raises: Telegram being down must never delay a trading decision."),
 (29,"telegram.commands","zarabot/telegram/commands.py","`telegram.commands`",["positions"],[13,14],"The entire user interface. One authorised chat id; everything else is ignored and logged."),
 (30,"reporter.weekly","zarabot/reporter/weekly.py","`reporter.weekly`",["positions","signals","daily_snapshots"],[12,13],"The Sunday report. Undefined metrics are reported as not applicable, never as zero."),
 (31,"ops.backup","zarabot/ops/backup.py","`ops.backup`",[],[18],"Nightly database backup. A failure alerts but never stops trading."),
 ("31b","ops.commissions","zarabot/ops/commissions.py",None,["positions"],[12],"Records commissions the broker reported after the fill and corrects the profit figures that depended on them. Without it a trade's cost stays permanently understated."),
 (32,"app.startup","zarabot/app/startup.py","`app.startup`",[],[15,16,17,21,30,32],"Fixed startup ordering: config, logging, connection, migrations, strategies, session, order recovery, reconciliation, halt state, ready alert. 70% coverage."),
 (33,"app.loops","zarabot/app/loops.py","`app.loops` / `app.shutdown`",[],[1,21,29,33],"The trading cycle. Exits run before the halt check, which is what implements halt-blocks-entries-only. 70% coverage."),
 (34,"app.shutdown","zarabot/app/shutdown.py","`app.loops` / `app.shutdown`",[],[21,30],"Graceful shutdown. Never cancels or liquidates positions - restarts must have no financial consequence. 70% coverage."),
 (35,"__main__","zarabot/__main__.py",None,[],[15],"Process entry point so that `python -m zarabot` works. No logic of its own."),
 (36,"sandbox.data","sandbox/",None,[],[],"Historical candle loading for research. Laptop only."),
 (37,"sandbox.backtest","sandbox/","`sandbox.backtest`",[],[],"The backtester. Imports the live strategy, sizing and exit modules UNCHANGED - reimplementing any of them makes every backtest meaningless."),
 (38,"sandbox.train","sandbox/",None,[],[],"Model training and export with a feature manifest. Walk-forward validation only."),
]


def section(marker, start_prefix="### ", stop_prefix="### "):
    lines = SPEC.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(start_prefix) and marker in line:
            out = [line]
            for nxt in lines[i + 1:]:
                if nxt.startswith(stop_prefix) or nxt.startswith("## "):
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return None


def test_block(key):
    if not key:
        return None
    lines = SPEC.splitlines()
    want = "**{}**".format(key)
    for i, line in enumerate(lines):
        if line.strip() == want:
            out = []
            for nxt in lines[i + 1:]:
                if re.match(r"^\*\*[^*]+\*\*$", nxt.strip()) or nxt.startswith("#"):
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return None


# Error rules are searched ONLY within section 8. Searching the whole document
# matches the numbered list in section 1 (manual setup) instead, and an
# unbounded stop condition then swallows everything up to the next numbered
# list — which is how rule 10 once pulled in 1400 lines.
_E_START = SPEC.find("## 8. Error handling rules")
_E_END = SPEC.find("## 9. Dependencies")
ERRORS = SPEC[_E_START:_E_END] if _E_START >= 0 < _E_END else ""


def rule(n):
    m = re.search(r"^{}\. (\*\*.*?)(?=^\d+\. \*\*|\Z)".format(n),
                  ERRORS, re.M | re.S)
    return "{}. {}".format(n, m.group(1).strip()) if m else None


def table(name):
    return section("`{}`".format(name))


OUT.mkdir(exist_ok=True)
for order, name, spec_key, tkey, tables, rules, context in M:
    path = "sandbox/{}.py".format(name.split(".")[-1]) if name.startswith("sandbox.") \
        else "zarabot/{}.py".format(name.replace(".", "/"))
    testfile = "tests/test_{}.py".format(name.replace(".", "_"))
    contract = section(spec_key) or "SPEC SECTION NOT FOUND — read `{}` in technical-spec.md §4 yourself.".format(spec_key)
    tests = test_block(tkey) or "No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception."
    body = ["# Task {}/{}: Implement `{}`".format(order, len(M), path), ""]
    body += ["## Product context", "", context, ""]
    body += ["## Build order position", "",
             "Module **{}** of {} in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.".format(order, len(M)), ""]
    body += ["## Already-implemented interfaces", "",
             "**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.", ""]
    if tables:
        body += ["## Database tables used", ""]
        for t in tables:
            body += [table(t) or "(table `{}` not found in spec §5)".format(t), ""]
    body += ["## Module contract", "", contract, ""]
    if rules:
        body += ["## Relevant error handling rules", "",
                 "From `technical-spec.md` §8. Handle each exactly as written.", ""]
        for n in rules:
            body += [rule(n) or "(rule {} not found)".format(n), ""]
    body += ["## Test cases", "",
             "From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.", "", tests, ""]
    body += ["## Expected output", "",
             "- `{}` implementing the contract exactly".format(path),
             "- `{}` implementing every test case above".format(testfile),
             "- All tests passing, coverage threshold met",
             "- This module's public signatures appended to `interfaces.md`", ""]
    body += ["## Agent instructions", "",
             "1. Write `{}` FIRST, from the test cases above. No implementation yet.".format(testfile),
             "2. Run it. Confirm it **fails** — nothing is implemented.",
             "3. Write `{}` to satisfy the contract.".format(path),
             "4. Run again. Iterate until all pass.",
             "5. Match contract signatures EXACTLY, including `| None`.",
             "6. Call interfaces as recorded; do not reimplement them.",
             "7. Handle every error rule above as written.",
             "8. Add no dependency outside `requirements-*.txt`.",
             "9. Modify no module other than this one.",
             "10. Append public signatures to `interfaces.md` once green.",
             "11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test",
             "    cannot pass without violating it — **STOP and ask**. Do not guess.", ""]
    stem = order if isinstance(order, str) else "{:02d}".format(order)
    (OUT / "{}-{}.md".format(stem, name.replace(".", "-"))).write_text(
        "\n".join(body), encoding="utf-8")

print("generated {} task files in tasks/".format(len(M)))
