"""Tests for scripts/deploy/export_health.py — host export (spec §7.1 v1.61)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)


def _mod():
    path = Path("scripts/deploy/export_health.py")
    spec = importlib.util.spec_from_file_location("export_health", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(event: str | None, level: str = "INFO", **extra: object) -> str:
    rec: dict[str, object] = {"level": level, "message": "x"}
    if event is not None:
        rec["event"] = event
    rec.update(extra)
    return "zarabot  | " + json.dumps(rec)


def test_heartbeat_event_counts_as_heartbeat() -> None:
    mod = _mod()
    events, errors, unknown = mod.parse_logs(
        _line("heartbeat", uptime_seconds=1, open_positions=0, halted=False)
    )
    assert events["heartbeat"] == 1
    assert errors == []
    assert unknown is False


def test_record_without_event_is_omitted_not_unknown() -> None:
    mod = _mod()
    events, _errors, unknown = mod.parse_logs(_line(None))
    assert events == {}
    assert unknown is False
    assert "unknown" not in events


def test_event_not_in_catalogue_is_unknown_and_fails() -> None:
    mod = _mod()
    events, _errors, unknown = mod.parse_logs(_line("not_a_catalog_event"))
    assert events["unknown"] == 1
    assert unknown is True


def test_docker_nonzero_is_log_export_failed_not_empty_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _mod()

    def _run(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="compose: failed")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    events, errors, failed = mod.collect_log_events(
        compose="compose.yml", days=7
    )
    assert failed is True
    assert events == {"log_export_failed": 1}
    assert errors == ["CalledProcessError"]
    assert events.get("heartbeat", 0) == 0


def test_docker_stderr_only_is_log_export_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _mod()

    def _run(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="permission denied")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    events, _errors, failed = mod.collect_log_events(
        compose="compose.yml", days=7
    )
    assert failed is True
    assert events == {"log_export_failed": 1}


def test_missing_database_is_a_failure_not_healthy_zeros(tmp_path: Path) -> None:
    mod = _mod()
    info, problems = mod.read_database(tmp_path / "missing.db")
    assert info["present"] is False
    assert problems
    assert "positions_open" not in info or info.get("positions_open") is None


def test_old_schema_missing_tables_are_errors_not_zero(tmp_path: Path) -> None:
    mod = _mod()
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    conn.execute(
        "INSERT INTO schema_version VALUES (2, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    info, problems = mod.read_database(db)
    assert info["present"] is True
    assert info["schema_version"] == 2
    assert problems
    assert any(
        "schema" in item.lower() or "no such table" in item.lower()
        for item in problems
    )


def test_recent_errors_omit_keys_prices_and_tokens() -> None:
    mod = _mod()
    raw = _line(
        "order_filled",
        "ERROR",
        key="secret-order-key",
        ticker="SBER",
        filled_price="100",
        tinvest_token="tok",  # noqa: S106
    )
    _events, errors, _unknown = mod.parse_logs(raw)
    blob = " ".join(errors)
    assert "secret-order-key" not in blob
    assert "100" not in blob
    assert "tok" not in blob
    assert "SBER" not in blob


def test_connects_read_only(tmp_path: Path) -> None:
    mod = _mod()
    db = tmp_path / "ro.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (7, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    uri = mod.read_only_uri(db)
    assert "mode=ro" in uri
    opened = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        opened.execute("INSERT INTO schema_version VALUES (8, 'x')")
    opened.close()
