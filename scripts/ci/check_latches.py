#!/usr/bin/env python3
"""Fail when a module-level boolean latch is set and never reset.

Failure class 8: a latch set and never reset. Each module's tests pass, and
nobody lists set-sites against reset-sites — so an alert latches on once and
the channel goes quiet for the rest of the process's life. In a channel where
silence means healthy, that is indistinguishable from no alert at all.

The rule this enforces is the spec's: every latch names its reset. Concretely,
for every module-level `_name = False` (or `_name: bool = False`) that some
function sets to True, there must be an assignment back to False inside a
function in the same module.

WHAT THIS DOES NOT COVER, stated because a guard described as proving
completeness must enumerate what it leaves out (failure class 6):

  * Set- and dict-shaped latches. `market/data.py`'s `_alerted: set[str]`
    resets with `.discard(ticker)`; `pnl.py`'s `_alerted_reconstruction:
    set[date]` never discards and does not need to, because a new Moscow date
    is a new key. Whether a collection latch self-expires is not decidable by
    reading assignments, so this check ignores them. Read them by hand.
  * Whether the reset is REACHABLE. A reset behind a condition that never
    holds satisfies this check and leaves the latch stuck. That needs a
    behavioural test, not a structural one.
  * Latches held in a class, a closure, or the database.

So a green run here means "every boolean latch has a reset written down",
not "every latch resets".
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "zarabot"


def _module_level_bools(tree: ast.Module) -> set[str]:
    """Module-level names bound to a bool literal."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets = [node.target]
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.startswith("_"):
                names.add(target.id)
    return names


def _assignments(tree: ast.Module, wanted: set[str]) -> tuple[set[str], set[str]]:
    """Names assigned True and names assigned False inside any function body."""
    set_true: set[str] = set()
    set_false: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Name) and target.id in wanted):
                    continue
                (set_true if value.value else set_false).add(target.id)
    return set_true, set_false


def main() -> int:
    unreset: list[tuple[str, str]] = []
    checked = 0
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        latches = _module_level_bools(tree)
        if not latches:
            continue
        set_true, set_false = _assignments(tree, latches)
        for name in sorted(set_true):
            checked += 1
            if name not in set_false:
                unreset.append((str(path.relative_to(ROOT)), name))

    if unreset:
        print("FAIL module-level latches set to True and never reset to False")
        for where, name in unreset:
            print(f"  {where}: {name}")
        print("Every latch names its reset. A latch that never clears is")
        print("equivalent to no alert at all.")
        return 1

    print(f"PASS latches consistent ({checked} boolean latches, each with a reset)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
