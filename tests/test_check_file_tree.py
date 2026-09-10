"""Issue #118 gate 7, third clause: the AGENTS.md sketch vs the real .py files.

Fixtures, not only the live repo. The live-tree test is the smoke that today's
rulebook and today's files still agree; the rest prove the gate FAILS when they
drift, that a stale allowlist entry fails rather than being ignored, and —
requirement 2 — that a renamed heading produces a FAIL line rather than a
traceback or a silent pass over zero parsed rows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_file_tree.py"

_AGENTS = """\
# Rules

## File structure

```
zarabot/          models, clock
  db/             migrations, connection,
                  orders
sandbox/          data, backtest   (never imported by zarabot/)
tests/            one file per module
vendor/           the broker SDK wheel
```

## Git
"""

_FILES = (
    "zarabot/models.py",
    "zarabot/clock.py",
    "zarabot/__init__.py",
    "zarabot/db/__init__.py",
    "zarabot/db/migrations.py",
    "zarabot/db/connection.py",
    "zarabot/db/orders.py",
    "sandbox/__init__.py",
    "sandbox/data.py",
    "sandbox/backtest.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("check_file_tree", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_file_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    agents: str = _AGENTS,
    files: tuple[str, ...] = _FILES,
) -> Path:
    (tmp_path / "AGENTS.md").write_text(agents, encoding="utf-8")
    for rel in files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    return tmp_path


def test_matching_tree_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), allowlist={})
    assert code == 0, lines
    assert any(line.startswith("PASS file tree") for line in lines)


def test_init_files_are_ignored(tmp_path: Path) -> None:
    """__init__.py is named by no sketch line and must not fail either way."""
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), allowlist={})
    assert code == 0, lines
    assert not any("__init__" in line for line in lines)


def test_unlisted_new_file_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(tmp_path, files=(*_FILES, "zarabot/db/planted.py"))
    code, lines = check.evaluate(root, allowlist={})
    assert code == 1
    assert any(
        line.startswith("FAIL") and "zarabot/db/planted.py" in line for line in lines
    )


def test_unlisted_new_top_level_file_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(tmp_path, files=(*_FILES, "sandbox/exchange.py"))
    code, lines = check.evaluate(root, allowlist={})
    assert code == 1
    assert any("sandbox/exchange.py" in line for line in lines)


def test_allowlisted_file_passes(tmp_path: Path) -> None:
    check = _load()
    root = _tree(tmp_path, files=(*_FILES, "sandbox/exchange.py"))
    code, lines = check.evaluate(
        root, allowlist={"sandbox/exchange.py": "#117 item 9"}
    )
    assert code == 0, lines
    assert any("allowlisted" in line for line in lines)


def test_sketch_name_without_a_file_fails(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("  db/             migrations, connection,", "  db/             migrations, connection, kelly,")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents), allowlist={})
    assert code == 1
    assert any("zarabot/db/kelly.py" in line for line in lines)


def test_stale_allowlist_entry_now_in_sketch_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path), allowlist={"zarabot/clock.py": "#000 obsolete waiver"}
    )
    assert code == 1
    assert any("stale" in line and "zarabot/clock.py" in line for line in lines)


def test_stale_allowlist_entry_for_deleted_file_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path), allowlist={"zarabot/db/gone.py": "#000 obsolete waiver"}
    )
    assert code == 1
    assert any("stale" in line and "zarabot/db/gone.py" in line for line in lines)


def test_renamed_heading_fails_loudly(tmp_path: Path) -> None:
    """Requirement 2: the gate's own input moving is a FAIL, not a pass."""
    check = _load()
    agents = _AGENTS.replace("## File structure", "## Repository layout")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents), allowlist={})
    assert code == 1
    assert any("File structure" in line and line.startswith("FAIL") for line in lines)


def test_missing_agents_file_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(tmp_path, allowlist={})
    assert code == 1
    assert any("AGENTS.md" in line for line in lines)


def test_reformatted_sketch_line_fails_loudly(tmp_path: Path) -> None:
    """Prose where module names were is unreadable, so it must FAIL."""
    check = _load()
    agents = _AGENTS.replace(
        "sandbox/          data, backtest   (never imported by zarabot/)",
        "sandbox/          research helpers for the laptop",
    )
    code, lines = check.evaluate(_tree(tmp_path, agents=agents), allowlist={})
    assert code == 1
    assert any("non-module tokens" in line for line in lines)


def test_empty_tree_fails_loudly(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, files=()), allowlist={})
    assert code == 1
    assert any("no .py files" in line for line in lines)


def test_live_repository_passes() -> None:
    """Smoke: today's rulebook sketch and today's files agree, waivers aside."""
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, lines
