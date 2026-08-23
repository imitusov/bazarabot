#!/usr/bin/env python3
"""Export what the running bot has actually DONE, for the weekly audit.

Runs on the VPS. A static audit reads contracts and finds contract violations.
It cannot see that every exit for three weeks was booked as TAKE_PROFIT, that
commission is null on every order, or that daily_snapshots has no rows - and
those are the findings that matter, because the code looks correct in each case.

Exports AGGREGATES only. No prices, no position ids, no order keys, nothing
that reconstructs a trade. The export lands in a git repository; a trade log
does not belong there, and counts are enough to spot every failure above.

Reads the database READ-ONLY. Never writes to it.

    export_health.py [--db PATH] [--out DIR] [--days 7]
"""

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import subprocess

p = argparse.ArgumentParser()
p.add_argument("--db", default="/opt/zarabot/data/zarabot.db")
p.add_argument("--out", default="ops/health")
p.add_argument("--days", type=int, default=7)
p.add_argument("--compose", default="/opt/zarabot/app/docker-compose.deploy.yml")
args = p.parse_args()

now = dt.datetime.now(dt.UTC)
since = (now - dt.timedelta(days=args.days)).isoformat()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)


def q(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return [("ERROR", str(exc))]


def counts(rows):
    return {str(a): b for a, b in rows}


health = {"generated_at": now.isoformat(), "window_days": args.days}

db = pathlib.Path(args.db)
if not db.exists():
    health["database"] = {"present": False, "note": "no database file — bot has never run"}
else:
    # read-only URI: an audit export must never be able to mutate trade history
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    health["database"] = {
        "present": True,
        "schema_version": (q(conn, "SELECT MAX(version) FROM schema_version") or [(None,)])[0][0],
        "positions_open": q(conn, "SELECT COUNT(*) FROM positions WHERE status='OPEN'")[0][0],
        "positions_closed_total": q(conn, "SELECT COUNT(*) FROM positions WHERE status='CLOSED'")[0][0],
        "positions_closed_window": q(conn, "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND exit_at>=?", (since,))[0][0],
        "adopted_open": q(conn, "SELECT COUNT(*) FROM positions WHERE status='OPEN' AND adopted=1")[0][0],

        # F-04/F-05/F-11 class: is every exit the same trigger? that is a
        # mis-attribution bug, not a strategy that only ever takes profit.
        "exit_trigger_distribution": counts(q(conn,
            "SELECT COALESCE(exit_trigger,'NULL'), COUNT(*) FROM positions "
            "WHERE status='CLOSED' GROUP BY 1")),

        # exactly one owner per position: any EXCHANGE without a key, or LOCAL
        # with one, is the invariant broken in production
        "stop_protection_distribution": counts(q(conn,
            "SELECT stop_protection, COUNT(*) FROM positions WHERE status='OPEN' GROUP BY 1")),
        "stop_ownership_violations": q(conn,
            "SELECT COUNT(*) FROM positions WHERE status='OPEN' AND ("
            "(stop_protection='EXCHANGE' AND stop_order_key IS NULL) OR "
            "(stop_protection='LOCAL' AND stop_order_key IS NOT NULL))")[0][0],

        "orders_by_status": counts(q(conn, "SELECT status, COUNT(*) FROM orders GROUP BY 1")),
        "orders_in_flight": q(conn, "SELECT COUNT(*) FROM orders WHERE status IN ('SUBMITTING','SUBMITTED')")[0][0],

        # commission null on every filled order means P&L is silently gross
        "filled_orders_missing_commission": q(conn,
            "SELECT COUNT(*) FROM orders WHERE status='FILLED' AND commission IS NULL")[0][0],
        "filled_orders_total": q(conn, "SELECT COUNT(*) FROM orders WHERE status='FILLED'")[0][0],

        # why is the bot NOT trading? rejection reasons are the answer, and a
        # dominant reason is a finding on its own
        "signal_decisions": counts(q(conn,
            "SELECT decision, COUNT(*) FROM signals WHERE generated_at>=? GROUP BY 1", (since,))),
        "rejection_reasons": counts(q(conn,
            "SELECT COALESCE(rejection_reason,'NONE'), COUNT(*) FROM signals "
            "WHERE generated_at>=? AND decision='REJECTED' GROUP BY 1", (since,))),
        "signals_per_strategy": counts(q(conn,
            "SELECT strategy, COUNT(*) FROM signals WHERE generated_at>=? GROUP BY 1", (since,))),

        # F-17: an equity curve that was never written
        "daily_snapshots_rows": q(conn, "SELECT COUNT(*) FROM daily_snapshots")[0][0],
        "daily_snapshots_missing_close": q(conn,
            "SELECT COUNT(*) FROM daily_snapshots WHERE closing_equity IS NULL")[0][0],
        "snapshot_date_range": [
            q(conn, "SELECT MIN(trade_date) FROM daily_snapshots")[0][0],
            q(conn, "SELECT MAX(trade_date) FROM daily_snapshots")[0][0],
        ],

        "halted": q(conn, "SELECT halted, reason FROM halt_state WHERE id=1")[0],
        "reconciliations_window": q(conn,
            "SELECT COUNT(*) FROM reconciliations WHERE ran_at>=?", (since,))[0][0],
        "reconciliations_with_adjustments": q(conn,
            "SELECT COUNT(*) FROM reconciliations WHERE ran_at>=? AND adjustments!='[]'", (since,))[0][0],
    }
    conn.close()

# --- what the process actually logged ---------------------------------------
# Event counts, plus recent errors verbatim. Not the whole log: docker keeps up
# to 30 MB and the audit needs the shape, not the transcript.
events: dict[str, int] = {}
errors: list[str] = []
try:
    raw = subprocess.run(
        ["docker", "compose", "-f", args.compose, "logs", "--since", f"{args.days * 24}h", "--no-color"],
        capture_output=True, text=True, timeout=120,
    ).stdout
    for line in raw.splitlines():
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            rec = json.loads(line[brace:])
        except ValueError:
            continue
        name = str(rec.get("event", "unknown"))
        events[name] = events.get(name, 0) + 1
        if rec.get("level") in ("ERROR", "CRITICAL") and len(errors) < 25:
            errors.append(json.dumps(rec)[:400])
except Exception as exc:  # noqa: BLE001
    events = {"log_export_failed": 1}
    errors = [f"{type(exc).__name__}"]

health["events"] = dict(sorted(events.items(), key=lambda kv: -kv[1]))
health["recent_errors"] = errors
health["heartbeats"] = events.get("heartbeat", 0)
health["expected_heartbeats"] = args.days

digest = pathlib.Path("/opt/zarabot/data/.deployed-digest")
health["deployed_digest"] = digest.read_text().strip() if digest.exists() else None

(out / "latest.json").write_text(json.dumps(health, indent=2, default=str), encoding="utf-8")
print(f"wrote {out / 'latest.json'}")

db_info = health.get("database", {})
lines = [
    "# Production health",
    "",
    f"Generated {now:%Y-%m-%d %H:%M} UTC, window {args.days} days.",
    f"Deployed digest: `{health['deployed_digest'] or 'unknown'}`",
    "",
    f"- Heartbeats: {health['heartbeats']} of ~{health['expected_heartbeats']} expected",
    f"- Open positions: {db_info.get('positions_open', '?')}"
    f" (adopted: {db_info.get('adopted_open', '?')})",
    f"- Closed this window: {db_info.get('positions_closed_window', '?')}",
    f"- Orders in flight: {db_info.get('orders_in_flight', '?')}",
    f"- Filled orders with no commission: "
    f"{db_info.get('filled_orders_missing_commission', '?')} of {db_info.get('filled_orders_total', '?')}",
    f"- Stop-ownership invariant violations: {db_info.get('stop_ownership_violations', '?')}",
    f"- Exit triggers: {db_info.get('exit_trigger_distribution', {})}",
    f"- Rejection reasons: {db_info.get('rejection_reasons', {})}",
    f"- daily_snapshots rows: {db_info.get('daily_snapshots_rows', '?')}",
    f"- Halted: {db_info.get('halted', '?')}",
    "",
    "## Errors logged",
    "",
]
lines += [f"- `{e}`" for e in errors] or ["- none"]
(out / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out / 'latest.md'}")
