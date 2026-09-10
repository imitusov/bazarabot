"""Issue #118 item 5: §3.2 test contracts vs the `M` table in make_tasks.py.

Fixtures, not only the live repo. The live-tree test is the smoke that today's
spec and generator still agree; the rest prove the gate fails when they drift,
and — requirement 2 — that it fails LOUDLY when its own anchors move rather
than passing with zero parsed rows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_test_contracts.py"

_SPEC = """\
### 3.2 Test contracts

**`models`**
- A case.

**`db.orders`**
- A case.

**`ops.commissions`**
- A case.

**partial fills**
- A case nobody's task file receives.

## 4. Module contracts

#### `models`
"""

_MAKE_TASKS = '''\
"""Generate one task file per module."""

M = [
 (1, "models", "zarabot/models.py", "`models`", [], [22], "ctx"),
 (7, "db.orders", "zarabot/db/orders.py", "`db.orders`", ["orders"], [5], "ctx"),
 ("31b", "ops.commissions", "zarabot/ops/commissions.py", "`ops.commissions`",
  [], [12], "ctx"),
 (8, "db.stop_orders", "zarabot/db/stop_orders.py", None, ["stop_orders"], [11],
  "ctx"),
]
'''

_ALLOW = {"partial fills": "#118 — themed sub-block under execution.orders"}


def _load():
    spec = importlib.util.spec_from_file_location("check_test_contracts", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_test_contracts"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    spec: str = _SPEC,
    make_tasks: str = _MAKE_TASKS,
) -> Path:
    (tmp_path / "technical-spec.md").write_text(spec, encoding="utf-8")
    gen = tmp_path / "scripts" / "make_tasks.py"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(make_tasks, encoding="utf-8")
    return tmp_path


def _fails(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


# --- the agreeing baseline -------------------------------------------------


def test_consistent_tree_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), unreached=_ALLOW)
    assert code == 0, lines
    assert any(line.startswith("PASS") for line in lines)


def test_none_key_with_genuinely_absent_block_passes(tmp_path: Path) -> None:
    """#113's shape: db.stop_orders has no §3.2 block and a `None` key.

    That is the consistent state, so the gate is deliberately silent on it.
    """
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), unreached=_ALLOW)
    assert code == 0
    assert not any("stop_orders" in line for line in lines)


# --- requirement 1: every violation direction ------------------------------


def test_orphan_heading_fails(tmp_path: Path) -> None:
    """A §3.2 block reachable from no `M` entry (the `sandbox.exchange` shape)."""
    check = _load()
    spec = _SPEC.replace(
        "**partial fills**",
        "**`sandbox.exchange`**\n- A case.\n\n**partial fills**",
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), unreached=_ALLOW)
    assert code == 1
    assert any("sandbox.exchange" in line and "no `M` entry" in line for line in lines)


def test_none_key_with_existing_block_fails(tmp_path: Path) -> None:
    """#114 exactly: the task file says 'no test block' and §3.2 has one."""
    check = _load()
    spec = _SPEC.replace(
        "**partial fills**",
        "**`db.stop_orders`**\n- A case.\n\n**partial fills**",
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), unreached=_ALLOW)
    assert code == 1
    assert any(
        "db.stop_orders" in line and "None" in line for line in _fails(lines)
    ), lines


def test_key_naming_a_missing_block_fails(tmp_path: Path) -> None:
    """An `M` key whose §3.2 block does not exist — a silent fallback."""
    check = _load()
    make_tasks = _MAKE_TASKS.replace('"`db.orders`"', '"`db.orderz`"')
    code, lines = check.evaluate(
        _tree(tmp_path, make_tasks=make_tasks), unreached=_ALLOW
    )
    assert code == 1
    assert any("db.orderz" in line for line in _fails(lines))


# --- requirement 2: fail loudly when the input moves -----------------------


def test_renamed_32_heading_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("### 3.2 Test contracts", "### 3.2bis Test cases")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), unreached=_ALLOW)
    assert code == 1
    assert any("3.2" in line for line in _fails(lines))


def test_renamed_section_4_anchor_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("## 4. Module contracts", "## 4bis. Module contracts")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), unreached=_ALLOW)
    assert code == 1
    assert any("## 4" in line for line in _fails(lines))


def test_empty_32_section_fails(tmp_path: Path) -> None:
    """Zero parsed headings is a broken parse, never a clean bill of health."""
    check = _load()
    spec = "### 3.2 Test contracts\n\nProse only.\n\n## 4. Module contracts\n"
    make_tasks = 'M = [\n (1, "models", "zarabot/models.py", None, [], [], "c"),\n]\n'
    code, lines = check.evaluate(
        _tree(tmp_path, spec=spec, make_tasks=make_tasks), unreached={}
    )
    assert code == 1
    assert any("no test-contract headings" in line for line in _fails(lines))


def test_missing_m_table_fails(tmp_path: Path) -> None:
    check = _load()
    make_tasks = "MODULES = []\n"
    code, lines = check.evaluate(
        _tree(tmp_path, make_tasks=make_tasks), unreached=_ALLOW
    )
    assert code == 1
    assert any("M" in line for line in _fails(lines))


def test_empty_m_table_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path, make_tasks="M = []\n"), unreached=_ALLOW
    )
    assert code == 1
    assert any("empty" in line.lower() for line in _fails(lines))


# --- requirement 4: allowlist staleness ------------------------------------


def test_stale_allowlist_entry_for_absent_heading_fails(tmp_path: Path) -> None:
    check = _load()
    allow = dict(_ALLOW, **{"`gone.module`": "#999"})
    code, lines = check.evaluate(_tree(tmp_path), unreached=allow)
    assert code == 1
    assert any("stale" in line.lower() and "gone.module" in line for line in lines)


def test_stale_allowlist_entry_now_reachable_fails(tmp_path: Path) -> None:
    """The waiver stopped describing a real gap: an `M` entry now reaches it."""
    check = _load()
    make_tasks = _MAKE_TASKS.replace(
        '"ctx"),\n]',
        '"ctx"),\n (99, "partial", "zarabot/x.py", "partial fills", [], [], "c"),\n]',
    )
    code, lines = check.evaluate(
        _tree(tmp_path, make_tasks=make_tasks), unreached=_ALLOW
    )
    assert code == 1
    assert any("stale" in line.lower() and "partial fills" in line for line in lines)


def test_allowlisted_orphan_is_reported_as_pass_line(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), unreached=_ALLOW)
    assert code == 0
    assert any("allowlisted" in line and "partial fills" in line for line in lines)


# --- the live tree ---------------------------------------------------------


def test_live_repository_passes() -> None:
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, "\n".join(lines)


def test_live_headings_match_make_tasks_lookup() -> None:
    """The parsed heading set must be exactly what `test_block` can resolve.

    A heading this gate sees but `test_block` cannot find would make the gate
    agree with a generator that still falls back to the 'no dedicated test
    block' text.
    """
    check = _load()
    # Import the generator's functions WITHOUT its module-level generation
    # loop: importing make_tasks rewrites tasks/, and a test that rewrites a
    # tracked directory can mask the drift gate it shares a tree with.
    source = (ROOT / "scripts" / "make_tasks.py").read_text("utf-8")
    head = source.split("OUT.mkdir(")[0]
    namespace: dict[str, object] = {
        "__name__": "make_tasks_head",
        "__file__": str(ROOT / "scripts" / "make_tasks.py"),
    }
    exec(compile(head, "make_tasks.py", "exec"), namespace)  # noqa: S102
    test_block = namespace["test_block"]
    headings = check.spec_headings((ROOT / "technical-spec.md").read_text("utf-8"))
    assert headings
    for heading in headings:
        assert test_block(heading) is not None, heading  # type: ignore[operator]
