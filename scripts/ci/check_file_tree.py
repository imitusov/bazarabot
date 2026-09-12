#!/usr/bin/env python3
"""Fail when AGENTS.md's File structure sketch and the real .py files disagree.

Issue #118 gate 7, third clause — the one `check_rulebook.py` deliberately
left out and labelled as out of scope. `check_rulebook.py` gates the
*directories* named in the sketch: it catches a renamed package, a deleted
``vendor/``, a new top-level package under ``zarabot/``. It does not catch a
new ``.py`` file — a planted ``zarabot/db/planted.py`` passed it — which is
why #117 item 9's four paths (``db/job_runs.py``, ``db/trading_days.py``,
``ops/commissions.py``, ``sandbox/exchange.py``) sat unnamed in the rulebook
every agent reads before every task, and ungated.

This gate closes that clause: it compares the module names written in the
sketch against ``find zarabot sandbox -name '*.py'``, in BOTH directions.

  * a file on disk that the sketch does not name FAILS (a module an agent's
    rulebook says does not exist);
  * a name in the sketch with no file behind it FAILS (a module deleted or
    renamed without the rulebook following).

The sketch names every module under both trees as of #117 item 9, so
``KNOWN_MISSING_FROM_SKETCH`` is empty: the four names above and
``zarabot/__main__.py`` were waived here while the rulebook was wrong, and the
waivers were deleted by the commit that wrote them into the sketch. That
allowlist has a staleness arm: an entry whose file has since been added to the
sketch, or whose file no longer exists, FAILS rather than being ignored. An
allowlist without that arm only ever grows, which is how #124's and #180's
stale waivers were caught elsewhere.

WHAT THIS DOES NOT COVER, stated because a guard described as proving the
rulebook's tree correct must enumerate what it leaves out (failure class 6):

  * ``__init__.py``. Package plumbing appears in no sketch line, so it is
    excluded from both directions. Deleting one is a Python defect, not a
    documentation defect, and pytest finds it long before this does.
  * Anything outside ``zarabot/`` and ``sandbox/``. The sketch's
    ``scripts/verify/``, ``tests/``, ``migrations/`` and ``vendor/`` lines
    carry prose descriptions, not module names; their existence as
    directories is `check_rulebook.py`'s arm, and their CONTENTS are gated by
    nothing here or there.
  * Sub-packages nested more than one level under ``zarabot/``. The sketch is
    two levels deep and so is this parser; a ``zarabot/db/legacy/x.py`` is
    invisible to both. There are none today.
  * The prose on each sketch line, the order names appear in, and whether a
    named module is imported, non-empty, or does what its line implies.
  * The §8 rule range and the coverage sentence — the other two clauses of
    gate 7, both in `check_rulebook.py`.

A green run here means every module file under ``zarabot/`` and ``sandbox/``
is named in the rulebook's sketch and every sketch name is a real file — NOT
that the sketch's descriptions are true, and not that anything outside those
two trees is inventoried at all.
"""

from __future__ import annotations

import pathlib
import re
import sys

# Files that exist and are absent from the AGENTS.md sketch today. Key is the
# repository-relative path. Delete an entry when the sketch line is fixed —
# leaving it in place once the name is present is itself a FAIL.
#
# EMPTY as of #117 item 9. The five entries that stood here were the four
# modules the sketch omitted (`db/job_runs.py`, `db/trading_days.py`,
# `ops/commissions.py`, `sandbox/exchange.py`) plus `zarabot/__main__.py`; the
# sketch names all five now, so the waivers went with the same commit that
# fixed it. An allowlist that outlives its reason is a gate that has stopped
# gating, and an empty one still fails on the next unnamed module rather than
# letting it join a list.
KNOWN_MISSING_FROM_SKETCH: dict[str, str] = {}

# The two trees the sketch inventories by module name.
ROOTS = ("zarabot", "sandbox")

_FILE_STRUCTURE = re.compile(r"(?ms)^## File structure\s*\n+```[^\n]*\n(.*?)^```")
_DIR_LINE = re.compile(r"^(\s*)([A-Za-z0-9_./]+)/\s*(.*)$")
_CONT_LINE = re.compile(r"^(\s+)(\S.*)$")
_PAREN = re.compile(r"\(.*?\)")
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class SketchError(ValueError):
    """The sketch could not be parsed — reported as a FAIL, never a traceback."""


def _names(rest: str) -> list[str]:
    """Module names from the right-hand side of a sketch line."""
    rest = _PAREN.sub("", rest)
    return [tok.strip() for tok in rest.split(",") if tok.strip()]


def parse_sketch(agents: str) -> dict[str, set[str]]:
    """Map directory (``zarabot``, ``zarabot/db``, ``sandbox``) to module names.

    Only directories under `ROOTS` are returned; the sketch's other lines
    (``tests/``, ``vendor/``…) describe themselves in prose and name no
    modules. An indented line without its own ``dir/`` continues the previous
    directory's list, which is how ``db/`` spans two lines.
    """
    hit = _FILE_STRUCTURE.search(agents)
    if hit is None:
        raise SketchError(
            "AGENTS.md has no '## File structure' fenced block — the heading or "
            "the fence moved, so the file tree is no longer gated"
        )
    top: str | None = None
    current: str | None = None
    out: dict[str, set[str]] = {}
    for raw in hit.group(1).splitlines():
        if not raw.strip():
            continue
        dir_hit = _DIR_LINE.match(raw)
        if dir_hit is not None:
            indent, name, rest = dir_hit.groups()
            name = name.rstrip("/")
            if indent:
                if top is None:
                    raise SketchError(
                        f"indented sketch line {raw.strip()!r} has no parent "
                        "directory above it"
                    )
                current = f"{top}/{name}"
            else:
                top = name
                current = name
            if current.split("/", 1)[0] not in ROOTS:
                current = None
                continue
            out.setdefault(current, set()).update(_names(rest))
            continue
        cont_hit = _CONT_LINE.match(raw)
        if cont_hit is None or current is None:
            continue
        out[current].update(_names(cont_hit.group(2)))
    if not out:
        raise SketchError(
            "the File structure sketch names no module under "
            + " or ".join(f"{r}/" for r in ROOTS)
        )
    for directory, names in out.items():
        bad = sorted(n for n in names if not _NAME.match(n))
        if bad:
            raise SketchError(
                f"sketch line for {directory}/ has non-module tokens {bad} — "
                "the line format changed and this gate can no longer read it"
            )
    return out


def actual_modules(root: pathlib.Path) -> dict[str, set[str]]:
    """Map the same directories to the ``.py`` stems that really exist."""
    out: dict[str, set[str]] = {}
    for top in ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            rel = path.relative_to(root)
            out.setdefault(str(rel.parent), set()).add(path.stem)
    return out


def evaluate(
    root: pathlib.Path,
    *,
    allowlist: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    waivers = KNOWN_MISSING_FROM_SKETCH if allowlist is None else allowlist
    lines: list[str] = []
    agents_path = root / "AGENTS.md"
    if not agents_path.is_file():
        return 1, ["FAIL AGENTS.md is missing — the rulebook's tree cannot be gated"]
    try:
        sketch = parse_sketch(agents_path.read_text(encoding="utf-8"))
    except SketchError as exc:
        return 1, [f"FAIL {exc}"]

    actual = actual_modules(root)
    if not actual:
        return 1, [
            "FAIL no .py files found under "
            + " or ".join(f"{r}/" for r in ROOTS)
            + " — run this from the repository root"
        ]

    sketched_paths = {
        f"{directory}/{name}.py"
        for directory, names in sketch.items()
        for name in names
    }
    actual_paths = {
        f"{directory}/{name}.py"
        for directory, names in actual.items()
        for name in names
    }

    waived: list[str] = []
    for rel in sorted(actual_paths - sketched_paths):
        issue = waivers.get(rel)
        if issue:
            waived.append(f"{rel} ({issue})")
            continue
        lines.append(
            f"FAIL {rel} exists but is not named in the AGENTS.md File "
            "structure sketch"
        )
    for rel in sorted(sketched_paths - actual_paths):
        lines.append(
            f"FAIL the AGENTS.md File structure sketch names {rel} but no such "
            "file exists"
        )

    # Staleness arm. A waiver that no longer describes a real divergence is a
    # silent exemption waiting to hide the next one.
    for rel, issue in sorted(waivers.items()):
        if rel in sketched_paths:
            lines.append(
                f"FAIL allowlist entry {rel} ({issue}) is stale — the sketch "
                "now names it; delete the entry"
            )
        elif rel not in actual_paths:
            lines.append(
                f"FAIL allowlist entry {rel} ({issue}) is stale — no such file "
                "exists; delete the entry"
            )
    if waived:
        lines.append("PASS allowlisted files missing from the sketch:")
        lines.extend(f"  {item}" for item in waived)

    if any(line.startswith("FAIL") for line in lines):
        return 1, lines
    lines.append(
        f"PASS file tree: {len(actual_paths)} module files under "
        + ", ".join(f"{r}/" for r in ROOTS)
        + " reconciled with the AGENTS.md sketch"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
