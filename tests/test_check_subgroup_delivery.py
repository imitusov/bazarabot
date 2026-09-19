"""Issue #118: §3.2 sub-groups must land in the task file their prose names.

`check_test_contracts.py` proves a §3.2 block is reachable from *some* `M`
entry. It is blind to sub-groups, because a sub-group is deliberately not a
bare bold heading — it carries trailing prose naming the module that owes it.
That blindness is the generator's own, which is why #189 (eighteen
`execution.orders` cases delivered to nobody) sat unseen: a gate blind in the
same way as its subject proves less than it appears to (failure class 6).

Fixtures, not only the live repo. The live-tree test is the smoke that today's
spec and tasks still agree; the rest prove the gate fails when they drift, and
— requirement 2 — that it fails LOUDLY when its own anchors move rather than
passing with zero parsed rows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_subgroup_delivery.py"

_SPEC = """\
### 3.2 Test contracts

**How these blocks are delivered (v1.80).** `scripts/make_tasks.py` cuts this
section by heading.

**`models`**
- A case.

**`execution.orders`**
- An entry case.

**partial fills** (`execution.orders`)
- A partial-fill case.
- Another partial-fill case.

**`broker.reconcile`**
- A reconciliation case.

## 4. Module contracts

#### `models`
"""

_MAKE_TASKS = '''\
"""Generate one task file per module."""

M = [
 (1, "models", "zarabot/models.py", "`models`", [], [22], "ctx"),
 (26, "execution.orders", "zarabot/execution/orders.py", "`execution.orders`",
  [], [3], "ctx"),
 (27, "broker.reconcile", "zarabot/broker/reconcile.py", "`broker.reconcile`",
  [], [6], "ctx"),
]
'''

_PROSE = {"How these blocks are delivered (v1.80).": "#118 — §3.2's own delivery note"}

_SUBGROUP = """\
**partial fills** (`execution.orders`)
- A partial-fill case.
- Another partial-fill case."""

_TWO_OWNERS = _SUBGROUP.replace(
    "(`execution.orders`)", "(`execution.orders`, `broker.reconcile`)"
)


def _load():
    spec = importlib.util.spec_from_file_location("check_subgroup_delivery", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_subgroup_delivery"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    spec: str = _SPEC,
    make_tasks: str = _MAKE_TASKS,
    tasks: dict[str, str] | None = None,
) -> Path:
    (tmp_path / "technical-spec.md").write_text(spec, encoding="utf-8")
    gen = tmp_path / "scripts" / "make_tasks.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(make_tasks, encoding="utf-8")
    out = tmp_path / "tasks"
    out.mkdir(parents=True, exist_ok=True)
    files = (
        tasks
        if tasks is not None
        else {
            "01-models.md": "## Test cases\n\n- A case.\n",
            "26-execution-orders.md": (
                "## Test cases\n\n- An entry case.\n\n" + _SUBGROUP + "\n"
            ),
            "27-broker-reconcile.md": "## Test cases\n\n- A reconciliation case.\n",
        }
    )
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return tmp_path


_TWO_OWNER_TASKS = {
    "01-models.md": "## Test cases\n\n- A case.\n",
    "26-execution-orders.md": (
        "## Test cases\n\n- An entry case.\n\n" + _TWO_OWNERS + "\n"
    ),
    "27-broker-reconcile.md": "## Test cases\n\n- A reconciliation case.\n",
}


def _fails(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


# --- the agreeing baseline -------------------------------------------------


def test_delivered_subgroup_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered={})
    assert code == 0, lines
    assert any(line.startswith("PASS") for line in lines)


def test_bare_headings_are_not_treated_as_subgroups(tmp_path: Path) -> None:
    """`**`models`**` owes nothing here — that is check_test_contracts' job."""
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered={})
    assert code == 0
    assert not any("`models`" in line for line in lines)


# --- requirement 1: the defect the gate exists for --------------------------


def test_subgroup_missing_from_named_module_fails(tmp_path: Path) -> None:
    """#189 exactly: the cases are specified and reach nobody."""
    check = _load()
    tree = _tree(
        tmp_path,
        tasks={
            "01-models.md": "## Test cases\n\n- A case.\n",
            "26-execution-orders.md": "## Test cases\n\n- An entry case.\n",
            "27-broker-reconcile.md": "## Test cases\n\n- A reconciliation case.\n",
        },
    )
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered={})
    assert code == 1
    assert any(
        "partial fills" in line and "execution.orders" in line for line in _fails(lines)
    ), lines


def test_subgroup_naming_a_second_module_must_reach_it_too(tmp_path: Path) -> None:
    """The live `stop-order lifecycle` shape: two owners, one delivery."""
    check = _load()
    spec = _SPEC.replace(
        "**partial fills** (`execution.orders`)",
        "**partial fills** (`execution.orders`, `broker.reconcile`)",
    )
    tree = _tree(tmp_path, spec=spec, tasks=_TWO_OWNER_TASKS)
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered={})
    assert code == 1
    assert any(
        "partial fills" in line and "broker.reconcile" in line for line in _fails(lines)
    ), lines
    # The owner that DID receive the cases must not be reported.
    assert not any("execution.orders" in line for line in _fails(lines)), lines


def test_subgroup_naming_no_module_fails(tmp_path: Path) -> None:
    """A typo'd or dropped owner silently turns a sub-group into orphan prose."""
    check = _load()
    spec = _SPEC.replace("(`execution.orders`)", "(`execution.orderz`)")
    code, lines = check.evaluate(
        _tree(tmp_path, spec=spec), prose=_PROSE, undelivered={}
    )
    assert code == 1
    assert any(
        "partial fills" in line and "no module" in line for line in _fails(lines)
    ), lines


def test_named_module_without_a_task_file_fails(tmp_path: Path) -> None:
    check = _load()
    tree = _tree(
        tmp_path,
        tasks={
            "01-models.md": "## Test cases\n\n- A case.\n",
            "27-broker-reconcile.md": "## Test cases\n\n- A reconciliation case.\n",
        },
    )
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered={})
    assert code == 1
    assert any("execution-orders" in line for line in _fails(lines)), lines


def test_two_task_files_for_one_module_fails(tmp_path: Path) -> None:
    """An ambiguous glob must be loud, never resolved by picking one."""
    check = _load()
    tree = _tree(tmp_path)
    (tree / "tasks" / "99-execution-orders.md").write_text("x", encoding="utf-8")
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered={})
    assert code == 1
    assert any("execution-orders" in line for line in _fails(lines)), lines


# --- requirement 2: fail loudly when the input moves -----------------------


def test_renamed_32_heading_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("### 3.2 Test contracts", "### 3.2bis Test cases")
    code, lines = check.evaluate(
        _tree(tmp_path, spec=spec), prose=_PROSE, undelivered={}
    )
    assert code == 1
    assert any("3.2" in line for line in _fails(lines))


def test_renamed_section_4_anchor_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("## 4. Module contracts", "## 4bis. Module contracts")
    code, lines = check.evaluate(
        _tree(tmp_path, spec=spec), prose=_PROSE, undelivered={}
    )
    assert code == 1
    assert any("## 4" in line for line in _fails(lines))


def test_empty_32_section_fails(tmp_path: Path) -> None:
    """Zero parsed bold lines is a broken parse, never a clean bill of health."""
    check = _load()
    spec = "### 3.2 Test contracts\n\nProse only.\n\n## 4. Module contracts\n"
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), prose={}, undelivered={})
    assert code == 1
    assert any("no bold" in line.lower() for line in _fails(lines))


def test_missing_m_table_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path, make_tasks="MODULES = []\n"), prose=_PROSE, undelivered={}
    )
    assert code == 1
    assert any("M" in line for line in _fails(lines))


def test_missing_tasks_directory_fails(tmp_path: Path) -> None:
    check = _load()
    tree = _tree(tmp_path)
    for path in (tree / "tasks").iterdir():
        path.unlink()
    (tree / "tasks").rmdir()
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered={})
    assert code == 1
    assert any("tasks/" in line for line in _fails(lines))


# --- requirement 4: allowlist staleness ------------------------------------


def test_unallowlisted_prose_line_fails(tmp_path: Path) -> None:
    """A bold line naming no module is either a sub-group defect or prose."""
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), prose={}, undelivered={})
    assert code == 1
    assert any("How these blocks are delivered" in line for line in _fails(lines))


def test_stale_prose_entry_for_absent_line_fails(tmp_path: Path) -> None:
    check = _load()
    prose = dict(_PROSE, **{"A note that is gone.": "#999"})
    code, lines = check.evaluate(_tree(tmp_path), prose=prose, undelivered={})
    assert code == 1
    assert any(
        "stale" in line.lower() and "A note that is gone." in line for line in lines
    )


def test_stale_prose_entry_that_now_names_a_module_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(
        "**How these blocks are delivered (v1.80).** `scripts/make_tasks.py` cuts this",
        "**How these blocks are delivered (v1.80).** `models` cuts this",
    )
    code, lines = check.evaluate(
        _tree(tmp_path, spec=spec), prose=_PROSE, undelivered={}
    )
    assert code == 1
    assert any("stale" in line.lower() and "How these blocks" in line for line in lines)


def test_allowlisted_undelivered_subgroup_passes_with_a_pass_line(
    tmp_path: Path,
) -> None:
    check = _load()
    spec = _SPEC.replace(
        "**partial fills** (`execution.orders`)",
        "**partial fills** (`execution.orders`, `broker.reconcile`)",
    )
    allow = {"partial fills::broker.reconcile": "#999 — a real, recorded gap"}
    tree = _tree(tmp_path, spec=spec, tasks=_TWO_OWNER_TASKS)
    code, lines = check.evaluate(tree, prose=_PROSE, undelivered=allow)
    assert code == 0, lines
    assert any("allowlisted" in line and "broker.reconcile" in line for line in lines)


def test_stale_undelivered_entry_now_delivered_fails(tmp_path: Path) -> None:
    check = _load()
    allow = {"partial fills::execution.orders": "#999"}
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered=allow)
    assert code == 1
    assert any("stale" in line.lower() and "execution.orders" in line for line in lines)


def test_stale_undelivered_entry_for_absent_subgroup_fails(tmp_path: Path) -> None:
    check = _load()
    allow = {"gone group::execution.orders": "#999"}
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered=allow)
    assert code == 1
    assert any("stale" in line.lower() and "gone group" in line for line in lines)


def test_stale_undelivered_entry_for_unnamed_module_fails(tmp_path: Path) -> None:
    """The sub-group exists but no longer names the allowlisted module."""
    check = _load()
    allow = {"partial fills::broker.reconcile": "#999"}
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered=allow)
    assert code == 1
    assert any("stale" in line.lower() and "broker.reconcile" in line for line in lines)


def test_malformed_undelivered_key_fails(tmp_path: Path) -> None:
    check = _load()
    allow = {"partial fills": "#999 — missing the ::module half"}
    code, lines = check.evaluate(_tree(tmp_path), prose=_PROSE, undelivered=allow)
    assert code == 1
    assert any("::" in line for line in _fails(lines))


# --- the live tree ---------------------------------------------------------


def test_live_repository_passes() -> None:
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, "\n".join(lines)


def test_live_tree_sees_the_known_subgroups() -> None:
    """A parse that finds nothing must never read as a clean tree."""
    check = _load()
    spec_text = (ROOT / "technical-spec.md").read_text(encoding="utf-8")
    names = {group.title for group in check.subgroups(spec_text)}
    assert "partial fills" in names
    assert "stop-order lifecycle" in names
