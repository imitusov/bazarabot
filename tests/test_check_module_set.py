"""Issue #118 gate 4: spec §4 headings vs `M` vs dependency-order.md.

Fixtures, not only the live repo. The live-tree test is the smoke that the
three module lists agree today; the rest prove the gate FAILS on each way they
can drift — a contract no task file carries, a spec-key matching nothing, a
spec-key matching two headings, a module in one list and not the other — and
that a renamed §4 heading or a missing `M` table produces a FAIL line rather
than a traceback (requirement 2).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_module_set.py"

_SPEC = """\
### 3.2 Test contracts

**`models`**
- A case.

## 4. Module contracts

### `zarabot/models.py`

Contract.

### `zarabot/db/orders.py`

Contract.

### `zarabot/risk/gate.py`

Contract.

### `zarabot/execution/orders.py`

Contract.

## 5. Database tables
"""

_MAKE_TASKS = '''\
"""Generate one task file per module."""

M = [
 (1,"models","zarabot/models.py","`models`",[],[],"ctx"),
 (2,"db.orders","zarabot/db/orders.py","`db.orders`",[],[],"ctx"),
 (3,"risk.gate","zarabot/risk/gate.py","`risk.gate`",[],[],"ctx"),
 (4,"execution.orders","zarabot/execution/orders.py","`execution.orders`",[],[],"ctx"),
]
'''

_DEP = """\
# Build order

1. **models** — depends on: (nothing)
2. **db.orders** — depends on: models
3. **risk.gate** — depends on: models
4. **execution.orders** — depends on: models
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_module_set", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_module_set"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    spec: str = _SPEC,
    make_tasks: str = _MAKE_TASKS,
    dep: str = _DEP,
) -> Path:
    (tmp_path / "technical-spec.md").write_text(spec, encoding="utf-8")
    (tmp_path / "dependency-order.md").write_text(dep, encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "make_tasks.py").write_text(make_tasks, encoding="utf-8")
    return tmp_path


def test_consistent_module_set_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path))
    assert code == 0, lines
    assert any(line.startswith("PASS module set") for line in lines)


def test_spec_heading_with_no_m_entry_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(
        "### `zarabot/risk/gate.py`",
        "### `zarabot/risk/kelly.py`\n\nContract.\n\n### `zarabot/risk/gate.py`",
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 1
    assert any("kelly" in line and "no M entry" in line for line in lines)


def test_m_key_matching_no_heading_fails(tmp_path: Path) -> None:
    check = _load()
    make_tasks = _MAKE_TASKS.replace(
        '"zarabot/risk/gate.py"', '"zarabot/risk/gate_v2.py"'
    )
    code, lines = check.evaluate(_tree(tmp_path, make_tasks=make_tasks))
    assert code == 1
    assert any("gate_v2" in line and "matches no" in line for line in lines)


def test_ambiguous_m_key_fails(tmp_path: Path) -> None:
    """`section()` takes the first substring match; two matches is a defect."""
    check = _load()
    make_tasks = _MAKE_TASKS.replace('"zarabot/db/orders.py"', '"orders.py"')
    code, lines = check.evaluate(_tree(tmp_path, make_tasks=make_tasks))
    assert code == 1
    assert any("matches 2 headings" in line for line in lines)


def test_m_key_matching_a_heading_outside_section_4_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("**`models`**", "### `zarabot/risk/gate.py` notes")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 1
    assert any("outside §4" in line or "headings" in line for line in lines)


def test_module_missing_from_dependency_order_fails(tmp_path: Path) -> None:
    check = _load()
    dep = _DEP.replace("3. **risk.gate**", "3. **risk.kelly**")
    code, lines = check.evaluate(_tree(tmp_path, dep=dep))
    assert code == 1
    assert any("risk.gate" in line for line in lines)
    assert any("risk.kelly" in line for line in lines)


def test_stale_module_set_allowlist_entry_fails(tmp_path: Path) -> None:
    check = _load()
    check.KNOWN_MODULE_SET_DRIFT["models"] = "#000 obsolete waiver"
    try:
        code, lines = check.evaluate(_tree(tmp_path))
    finally:
        check.KNOWN_MODULE_SET_DRIFT.pop("models")
    assert code == 1
    assert any("stale" in line and "models" in line for line in lines)


def test_stale_unclaimed_heading_allowlist_entry_fails(tmp_path: Path) -> None:
    check = _load()
    check.KNOWN_UNCLAIMED_SPEC_HEADINGS["### `zarabot/models.py`"] = "#000 obsolete"
    try:
        code, lines = check.evaluate(_tree(tmp_path))
    finally:
        check.KNOWN_UNCLAIMED_SPEC_HEADINGS.pop("### `zarabot/models.py`")
    assert code == 1
    assert any("stale" in line for line in lines)


def test_renamed_section_4_fails_loudly(tmp_path: Path) -> None:
    """Requirement 2: a renamed §4 heading is a FAIL line, not a traceback."""
    check = _load()
    spec = _SPEC.replace("## 4. Module contracts", "## 4bis. Module contracts")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 1
    assert any("has no '## 4" in line for line in lines)


def test_missing_m_table_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, make_tasks='"""No table."""\n'))
    assert code == 1
    assert any("`M = [...]`" in line or "M table" in line for line in lines)


def test_unnumbered_dependency_order_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, dep="# Build order\n\nprose only\n"))
    assert code == 1
    assert any("dependency-order.md" in line for line in lines)


def test_missing_document_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(tmp_path)
    assert code == 1
    assert any(line.startswith("FAIL") for line in lines)


def test_live_repository_passes() -> None:
    """Smoke: §4, `M` and dependency-order.md name the same modules today."""
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, lines
