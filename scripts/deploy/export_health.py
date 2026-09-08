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

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import subprocess
import sys

# Spec §7.1 catalogue. A record with no `event` is omitted. A name not in
# this set is counted as `unknown` and the export exits non-zero (v1.61).
KNOWN_EVENTS = frozenset(
    {
        "startup_ok",
        "startup_failed",
        "config_invalid",
        "session_open",
        "session_closed",
        "candles_failed",
        "signal_generated",
        "signal_rejected",
        "order_submitting",
        "order_filled",
        "order_rejected",
        "order_unresolved",
        "order_resolved",
        "position_opened",
        "position_closed",
        "exit_failed",
        "stop_order_placed",
        "stop_order_cancelled",
        "stop_order_executed",
        "stop_protection_degraded",
        "stop_order_orphaned",
        "partial_fill",
        "cooldown_started",
        "halt_triggered",
        "halt_cleared",
        "reconciliation",
        "broker_unavailable",
        "rate_limited",
        "db_write_failed",
        "telegram_send_failed",
        "unauthorised_command",
        "secret_redacted",
        "backup_ok",
        "backup_failed",
        "task_crashed",
        "clock_drift",
        "heartbeat",
        "weekly_report_sent",
    }
)

_MIGRATION = re.compile(r"^(\d+)_.*\.sql$")


def expected_schema_version(migrations: pathlib.Path | None = None) -> int:
    root = migrations or pathlib.Path(__file__).resolve().parents[2] / "migrations"
    if not root.is_dir():
        return 0
    versions = [
        int(match.group(1))
        for path in root.iterdir()
        if path.is_file() and (match := _MIGRATION.match(path.name))
    ]
    return max(versions) if versions else 0


def read_only_uri(db: pathlib.Path) -> str:
    return f"file:{db}?mode=ro"


def parse_logs(raw: str) -> tuple[dict[str, int], list[str], bool, int]:
    events: dict[str, int] = {}
    errors: list[str] = []
    unknown = False
    malformed = 0
    for line in raw.splitlines():
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            rec = json.loads(line[brace:])
        except ValueError:
            malformed += 1
            continue
        if not isinstance(rec, dict) or "event" not in rec:
            continue
        name = str(rec["event"])
        if name not in KNOWN_EVENTS:
            name = "unknown"
            unknown = True
        events[name] = events.get(name, 0) + 1
        if rec.get("level") in ("ERROR", "CRITICAL") and len(errors) < 25:
            safe = {
                "event": rec.get("event"),
                "level": rec.get("level"),
            }
            errors.append(json.dumps(safe, default=str))
    return events, errors, unknown, malformed


def collect_log_events(
    compose: str, days: int
) -> tuple[dict[str, int], list[str], bool, bool, int]:
    """Return (events, errors, failed, unknown, malformed)."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                compose,
                "logs",
                "--since",
                f"{days * 24}h",
                "--no-color",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"log_export_failed": 1}, [type(exc).__name__], True, False, 0
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"log_export_failed": 1}, ["CalledProcessError"], True, False, 0
    events, errors, unknown, malformed = parse_logs(proc.stdout)
    failed = unknown or malformed > 0 or events.get("heartbeat", 0) < 1
    return events, errors, failed, unknown, malformed


def fill_database_health(
    conn: sqlite3.Connection, since: str, problems: list[str]
) -> dict[str, object]:
    def q(sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]] | None:
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
            return None

    def counts(rows: list[tuple[object, ...]] | None) -> dict[str, object]:
        if rows is None:
            return {}
        return {str(a): b for a, b in rows}

    def scalar(sql: str, params: tuple[object, ...] = ()) -> object | None:
        rows = q(sql, params)
        if not rows:
            return None
        return rows[0][0]

    halted_rows = q("SELECT halted, reason FROM halt_state WHERE id=1")
    halted: object | None = None if not halted_rows else halted_rows[0]

    return {
        "positions_open": scalar("SELECT COUNT(*) FROM positions WHERE status='OPEN'"),
        "positions_closed_total": scalar(
            "SELECT COUNT(*) FROM positions WHERE status='CLOSED'"
        ),
        "positions_closed_window": scalar(
            "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND exit_at>=?",
            (since,),
        ),
        "adopted_open": scalar(
            "SELECT COUNT(*) FROM positions WHERE status='OPEN' AND adopted=1"
        ),
        "exit_trigger_distribution": counts(
            q(
                "SELECT COALESCE(exit_trigger,'NULL'), COUNT(*) FROM positions "
                "WHERE status='CLOSED' GROUP BY 1"
            )
        ),
        "stop_protection_distribution": counts(
            q(
                "SELECT stop_protection, COUNT(*) FROM positions "
                "WHERE status='OPEN' GROUP BY 1"
            )
        ),
        "stop_ownership_violations": scalar(
            "SELECT COUNT(*) FROM positions WHERE status='OPEN' AND ("
            "(stop_protection='EXCHANGE' AND stop_order_key IS NULL) OR "
            "(stop_protection='LOCAL' AND stop_order_key IS NOT NULL))"
        ),
        "orders_by_status": counts(q("SELECT status, COUNT(*) FROM orders GROUP BY 1")),
        "orders_in_flight": scalar(
            "SELECT COUNT(*) FROM orders WHERE status IN ('SUBMITTING','SUBMITTED')"
        ),
        "filled_orders_missing_commission": scalar(
            "SELECT COUNT(*) FROM orders WHERE status='FILLED' AND commission IS NULL"
        ),
        "filled_orders_total": scalar(
            "SELECT COUNT(*) FROM orders WHERE status='FILLED'"
        ),
        "signal_decisions": counts(
            q(
                "SELECT decision, COUNT(*) FROM signals WHERE generated_at>=? GROUP BY 1",
                (since,),
            )
        ),
        "rejection_reasons": counts(
            q(
                "SELECT COALESCE(rejection_reason,'NONE'), COUNT(*) FROM signals "
                "WHERE generated_at>=? AND decision='REJECTED' GROUP BY 1",
                (since,),
            )
        ),
        "signals_per_strategy": counts(
            q(
                "SELECT strategy, COUNT(*) FROM signals WHERE generated_at>=? GROUP BY 1",
                (since,),
            )
        ),
        "daily_snapshots_rows": scalar("SELECT COUNT(*) FROM daily_snapshots"),
        "daily_snapshots_missing_close": scalar(
            "SELECT COUNT(*) FROM daily_snapshots WHERE closing_equity IS NULL"
        ),
        "snapshot_date_range": [
            scalar("SELECT MIN(trade_date) FROM daily_snapshots"),
            scalar("SELECT MAX(trade_date) FROM daily_snapshots"),
        ],
        "halted": halted,
        "reconciliations_window": scalar(
            "SELECT COUNT(*) FROM reconciliations WHERE ran_at>=?", (since,)
        ),
        "reconciliations_with_adjustments": scalar(
            "SELECT COUNT(*) FROM reconciliations WHERE ran_at>=? AND adjustments!='[]'",
            (since,),
        ),
    }


def read_database(
    db: pathlib.Path, since: str | None = None
) -> tuple[dict[str, object], list[str]]:
    problems: list[str] = []
    if not db.exists():
        return (
            {"present": False, "note": "no database file — bot has never run"},
            ["database file missing"],
        )
    conn = sqlite3.connect(read_only_uri(db), uri=True)
    expected = expected_schema_version()
    info: dict[str, object] = {"present": True}
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        version = row[0] if row else None
    except sqlite3.Error as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        version = None
    info["schema_version"] = version
    if version != expected:
        problems.append(
            f"schema_version {version} does not match code version {expected}"
        )
    window = since or (dt.datetime.now(dt.UTC) - dt.timedelta(days=7)).isoformat()
    info.update(fill_database_health(conn, window, problems))
    conn.close()
    return info, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/opt/zarabot/data/zarabot.db")
    parser.add_argument("--out", default="ops/health")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--compose", default="/opt/zarabot/app/docker-compose.deploy.yml"
    )
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    since = (now - dt.timedelta(days=args.days)).isoformat()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    failed = False
    health: dict[str, object] = {
        "generated_at": now.isoformat(),
        "window_days": args.days,
    }

    db_info, db_problems = read_database(pathlib.Path(args.db), since)
    health["database"] = db_info
    if db_problems:
        failed = True
        health["database_problems"] = db_problems

    events, errors, log_failed, unknown, malformed = collect_log_events(
        args.compose, args.days
    )
    if log_failed:
        failed = True
    health["events"] = dict(sorted(events.items(), key=lambda kv: -kv[1]))
    health["recent_errors"] = errors
    health["heartbeats"] = events.get("heartbeat", 0)
    health["unknown"] = unknown
    health["malformed"] = malformed
    health["export_failed"] = failed

    digest = pathlib.Path("/opt/zarabot/data/.deployed-digest")
    health["deployed_digest"] = digest.read_text().strip() if digest.exists() else None

    (out / "latest.json").write_text(
        json.dumps(health, indent=2, default=str), encoding="utf-8"
    )
    print(f"wrote {out / 'latest.json'}")

    db_info_out = health.get("database", {})
    if not isinstance(db_info_out, dict):
        db_info_out = {}
    lines = [
        "# Production health",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC, window {args.days} days.",
        f"Deployed digest: `{health['deployed_digest'] or 'unknown'}`",
        "",
        f"- Heartbeats observed: {health['heartbeats']}",
        f"- Log export failed: {'yes' if log_failed else 'no'}",
        f"- Unknown catalogue names: {'yes' if unknown else 'no'}",
        f"- Malformed JSON lines: {malformed}",
        f"- Open positions: {db_info_out.get('positions_open', '?')}"
        f" (adopted: {db_info_out.get('adopted_open', '?')})",
        f"- Closed this window: {db_info_out.get('positions_closed_window', '?')}",
        f"- Orders in flight: {db_info_out.get('orders_in_flight', '?')}",
        f"- Filled orders with no commission: "
        f"{db_info_out.get('filled_orders_missing_commission', '?')} of "
        f"{db_info_out.get('filled_orders_total', '?')}",
        f"- Stop-ownership invariant violations: "
        f"{db_info_out.get('stop_ownership_violations', '?')}",
        f"- Exit triggers: {db_info_out.get('exit_trigger_distribution', {})}",
        f"- Rejection reasons: {db_info_out.get('rejection_reasons', {})}",
        f"- daily_snapshots rows: {db_info_out.get('daily_snapshots_rows', '?')}",
        f"- Halted: {db_info_out.get('halted', '?')}",
        "",
        "## Errors logged",
        "",
    ]
    lines += [f"- `{e}`" for e in errors] or ["- none"]
    if db_problems:
        lines += ["", "## Database problems", ""]
        lines += [f"- `{p}`" for p in db_problems]
    (out / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out / 'latest.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
