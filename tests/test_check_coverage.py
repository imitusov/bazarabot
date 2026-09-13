"""Issue #219: the per-module coverage floors must apply to files that exist.

`STRICT_MODULES` names the four modules carrying the 95% money-path floor. The
floors are applied by matching the paths coverage.json reports, so a renamed or
moved money-path module used to match nothing, fall to the ordinary 80%, and
leave the gate reporting PASS — the strictest check in the project switching
itself off with no symptom (failure class 6). `KNOWN_BELOW` had the same hole.

Fixtures, not only the live repo. The live-tree test is the smoke that today's
entries name real files; the rest prove the gate FAILS on each way an entry can
stop applying — a path that no longer exists, a path that exists but the suite
never imported, a module listed in both tables — and that a missing or
malformed coverage.json produces a FAIL line rather than a traceback.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_coverage.py"

# One file per branch of the floor table: strict, ratchet, orchestration,
# standard. Percentages sit on or just above each floor, so a fixture that
# moves one number is a fixture that reds exactly one arm.
_PCTS = {
    "zarabot/risk/gate.py": 100.0,
    "zarabot/risk/sizing.py": 99.0,
    "zarabot/lifecycle/exits.py": 96.0,
    "zarabot/execution/orders.py": 95.0,
    "zarabot/broker/client.py": 86.0,
    "zarabot/pnl.py": 85.0,
    "zarabot/app/loops.py": 72.0,
    "zarabot/models.py": 81.0,
}


def _load():
    spec = importlib.util.spec_from_file_location("check_coverage", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_coverage"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    pcts: dict[str, float] | None = None,
    on_disk: list[str] | None = None,
    coverage: str | None = None,
) -> Path:
    """A tree with the module files on disk and a coverage.json beside them."""
    pcts = _PCTS if pcts is None else pcts
    for rel in list(pcts) if on_disk is None else on_disk:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    if coverage is None:
        coverage = json.dumps(
            {
                "files": {
                    path: {"summary": {"percent_covered": pct}}
                    for path, pct in pcts.items()
                }
            }
        )
    if coverage != "":
        (tmp_path / "coverage.json").write_text(coverage, encoding="utf-8")
    return tmp_path


@contextlib.contextmanager
def _patched(mod, name: str, value: object) -> Iterator[None]:
    """Swap a module-level table for the duration of one test."""
    original = getattr(mod, name)
    setattr(mod, name, value)
    try:
        yield
    finally:
        setattr(mod, name, original)


# --- the floors still apply ------------------------------------------------


def test_conforming_tree_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path))
    assert code == 0, lines
    assert any(line.startswith("PASS per-module coverage") for line in lines)


def test_strict_module_below_its_floor_fails(tmp_path: Path) -> None:
    check = _load()
    pcts = dict(_PCTS, **{"zarabot/risk/gate.py": 94.9})
    code, lines = check.evaluate(_tree(tmp_path, pcts=pcts))
    assert code == 1
    assert any("zarabot/risk/gate.py" in ln and "money-path" in ln for ln in lines)


def test_ratchet_module_below_its_pin_fails(tmp_path: Path) -> None:
    check = _load()
    pcts = dict(_PCTS, **{"zarabot/pnl.py": 84.0})
    code, lines = check.evaluate(_tree(tmp_path, pcts=pcts))
    assert code == 1
    assert any("zarabot/pnl.py" in ln and "84.0%" in ln for ln in lines)


def test_orchestration_module_below_relaxed_floor_fails(tmp_path: Path) -> None:
    check = _load()
    pcts = dict(_PCTS, **{"zarabot/app/loops.py": 69.0})
    code, lines = check.evaluate(_tree(tmp_path, pcts=pcts))
    assert code == 1
    assert any("zarabot/app/loops.py" in ln and "70%" in ln for ln in lines)


def test_standard_module_below_default_fails(tmp_path: Path) -> None:
    check = _load()
    pcts = dict(_PCTS, **{"zarabot/models.py": 79.9})
    code, lines = check.evaluate(_tree(tmp_path, pcts=pcts))
    assert code == 1
    assert any("zarabot/models.py" in ln and "standard" in ln for ln in lines)


# --- issue #219: an entry that names nothing -------------------------------


def test_renamed_strict_module_fails_naming_the_entry(tmp_path: Path) -> None:
    """The bug: a moved money-path module silently fell back to 80%."""
    check = _load()
    renamed = (check.STRICT_MODULES - {"zarabot/risk/gate.py"}) | {
        "zarabot/risk/gate_v2.py"
    }
    with _patched(check, "STRICT_MODULES", renamed):
        code, lines = check.evaluate(_tree(tmp_path))
    assert code == 1
    assert any(
        "STRICT_MODULES" in ln and "zarabot/risk/gate_v2.py" in ln for ln in lines
    ), lines


def test_missing_known_below_path_fails_naming_the_entry(tmp_path: Path) -> None:
    check = _load()
    renamed = dict(check.KNOWN_BELOW)
    renamed.pop("zarabot/pnl.py")
    renamed["zarabot/profit_and_loss.py"] = 85.0
    with _patched(check, "KNOWN_BELOW", renamed):
        code, lines = check.evaluate(_tree(tmp_path))
    assert code == 1
    assert any(
        "KNOWN_BELOW" in ln and "zarabot/profit_and_loss.py" in ln for ln in lines
    ), lines


def test_missing_relaxed_prefix_directory_fails(tmp_path: Path) -> None:
    check = _load()
    with _patched(check, "RELAXED_PREFIXES", ("zarabot/orchestration/",)):
        code, lines = check.evaluate(_tree(tmp_path))
    assert code == 1
    assert any("zarabot/orchestration/" in ln for ln in lines), lines


def test_strict_module_absent_from_coverage_json_fails(tmp_path: Path) -> None:
    """The file exists, the suite never imported it, so no floor was applied."""
    check = _load()
    pcts = {k: v for k, v in _PCTS.items() if k != "zarabot/risk/sizing.py"}
    tree = _tree(tmp_path, pcts=pcts, on_disk=list(_PCTS))
    code, lines = check.evaluate(tree)
    assert code == 1
    assert any(
        "zarabot/risk/sizing.py" in ln and "coverage.json" in ln for ln in lines
    ), lines


def test_known_below_absent_from_coverage_json_fails(tmp_path: Path) -> None:
    check = _load()
    pcts = {k: v for k, v in _PCTS.items() if k != "zarabot/broker/client.py"}
    tree = _tree(tmp_path, pcts=pcts, on_disk=list(_PCTS))
    code, lines = check.evaluate(tree)
    assert code == 1
    assert any(
        "zarabot/broker/client.py" in ln and "coverage.json" in ln for ln in lines
    ), lines


def test_module_in_both_tables_fails(tmp_path: Path) -> None:
    """STRICT wins the if/elif, so the KNOWN_BELOW entry would be dead code."""
    check = _load()
    both = dict(check.KNOWN_BELOW, **{"zarabot/risk/gate.py": 86.0})
    with _patched(check, "KNOWN_BELOW", both):
        code, lines = check.evaluate(_tree(tmp_path))
    assert code == 1
    assert any("zarabot/risk/gate.py" in ln and "both" in ln for ln in lines), lines


# --- loud, not a traceback -------------------------------------------------


def test_missing_coverage_json_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, coverage=""))
    assert code == 1
    assert any(ln.startswith("FAIL") and "coverage.json" in ln for ln in lines)
    assert not any("Traceback" in ln for ln in lines)


def test_malformed_coverage_json_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, coverage="{not json"))
    assert code == 1
    assert any(ln.startswith("FAIL") and "coverage.json" in ln for ln in lines)


def test_coverage_json_without_files_key_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, coverage='{"totals": {}}'))
    assert code == 1
    assert any(ln.startswith("FAIL") and "files" in ln for ln in lines)


def test_coverage_entry_without_percent_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    broken = json.dumps({"files": {"zarabot/models.py": {"summary": {}}}})
    code, lines = check.evaluate(_tree(tmp_path, coverage=broken))
    assert code == 1
    assert any(ln.startswith("FAIL") and "percent_covered" in ln for ln in lines)


def test_path_checks_run_even_without_coverage_json(tmp_path: Path) -> None:
    """A missing report must not hide the entry that names nothing."""
    check = _load()
    renamed = (check.STRICT_MODULES - {"zarabot/risk/gate.py"}) | {
        "zarabot/risk/gate_v2.py"
    }
    with _patched(check, "STRICT_MODULES", renamed):
        code, lines = check.evaluate(_tree(tmp_path, coverage=""))
    assert code == 1
    assert any("zarabot/risk/gate_v2.py" in ln for ln in lines), lines


# --- the live tree ---------------------------------------------------------


def test_live_repository_floor_tables_name_real_files() -> None:
    """Smoke: every entry in both tables resolves to a file in this repo.

    Static on purpose — coverage.json is written after the suite finishes, so
    a live `evaluate()` here would read a stale report or none at all.
    """
    check = _load()
    lines: list[str] = []
    check.check_tables(ROOT, lines)
    assert not [ln for ln in lines if ln.startswith("FAIL")], lines
