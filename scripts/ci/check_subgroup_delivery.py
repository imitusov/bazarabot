#!/usr/bin/env python3
"""Fail when a §3.2 sub-group does not land in the task file its prose names.

Issue #118. `scripts/make_tasks.py` cuts §3.2 by **bare** bold heading: a line
whose whole content is ``**text**`` opens a block, and only a heading named in
the generator's `M` table reaches a task file. A sub-group therefore never gets
a bare heading of its own — by the convention §3.2's own preamble states, it
carries trailing prose naming the module that owes it, as in
``**partial fills** (`execution.orders`)``, which keeps its cases inside the
owning module's block.

Nothing checked that the prose was telling the truth. `check_test_contracts.py`
proves a §3.2 block is reachable from *some* `M` entry, and sub-groups are
invisible to it by construction — it mirrors the generator's blindness rather
than correcting it, and says so. So the two together still would not have
caught #189: eighteen partial-fill cases written for `execution.orders`, the
highest-risk module in the project, delivered to nobody for eleven spec
versions. A gate blind in the same way as its subject proves less than it
appears to (failure class 6). This gate is the correction.

It checks, for every unindented bold-prefixed line in §3.2 that carries
trailing prose:

  1. The prose names at least one module from `M`. A typo'd, renamed or
     dropped owner turns a sub-group back into orphan prose silently, which is
     #189's shape with one character changed — so naming nobody is a FAIL
     unless the line is allowlisted as deliberate prose.
  2. For each module named, the sub-group's text appears **verbatim** in that
     module's generated task file under `tasks/`. That is the delivery claim
     itself, read off the artefact an agent is actually handed rather than
     re-derived from the generator's own cutting rule — re-deriving it would
     reproduce the blind spot this gate exists to close.

WHAT THIS DOES NOT COVER, stated because a guard about delivery completeness
must enumerate the completeness it does not prove (failure class 6):

  * BARE §3.2 headings. Whether ``**`models`**`` reaches a task file is
    `check_test_contracts.py`'s job and is untouched here; this gate is
    silent on every line it can parse as a bare heading.
  * INDENTED bold text. Only a line whose first character is ``*`` is a
    candidate. Mid-bullet emphasis — ``  **re-read**, not from the …`` — is
    therefore ignored, and so would a sub-group heading be if someone
    indented one. The convention §3.2 documents puts headings at column 0.
  * Whether the named module is the RIGHT owner. The prose is taken at its
    word: a sub-group naming `pnl` must reach `pnl`'s task file, and that it
    should have named `execution.orders` instead is the critic's finding, not
    this gate's.
  * Block CONTENT, and whether any of it became a test. A sub-group of one
    vacuous line delivered to the right file passes. Mapping cases to tests is
    the traceability check's job.
  * Whether `tasks/` is current. This reads the files on disk; that they match
    a fresh `python3 scripts/make_tasks.py` is the `Spec drift` step's job
    (`git diff --exit-code tasks/`). The two are complementary: drift proves
    the files are generated, this proves the generation delivered.
  * §3.2 blocks with NO sub-groups, and modules with no §3.2 block at all
    (#113). Nothing here counts sub-groups a module ought to have.

A green run means every sub-group in §3.2 names a real module and its cases
are in that module's task file — NOT that the cases are right, that the owner
is right, or that any test was written from them.

The allowlists are inventories, each entry naming its issue, and an entry that
stops describing a real gap FAILS rather than being ignored: a stale waiver is
a silent exemption, which is how #124's and #180's entries were caught.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from typing import NamedTuple

# Unindented bold-prefixed lines in §3.2 that name no module and are NOT
# sub-groups. Key is the bold text between the asterisks, verbatim. Delete an
# entry when its line goes, and expect this gate to say so if you forget.
KNOWN_PROSE_BOLD_LINES: dict[str, str] = {
    "How these blocks are delivered (v1.80).": (
        "#118/#189 — §3.2's own preamble stating the delivery convention this "
        "gate enforces; it is documentation, not a group of cases"
    ),
}

# Sub-group/module pairs whose cases do not reach that module's task file
# today. Key is ``<sub-group title>::<module>``. An entry here is a RECORDED
# DEFECT in technical-spec.md, never a design choice — it is waived only
# because this gate may not amend the spec.
KNOWN_UNDELIVERED_SUBGROUPS: dict[str, str] = {
    "stop-order lifecycle::broker.reconcile": (
        "#118 — the sub-group sits inside the `execution.orders` block, so its "
        "reconciliation cases (place a missing stop, cancel an orphan, replace "
        "a mispriced one, adopt a live one on restart) reach "
        "tasks/26-execution-orders.md and never tasks/27-broker-reconcile.md. "
        "#189's shape, second owner. Fixing it is a spec amendment"
    ),
}

_H32 = re.compile(r"(?m)^### 3\.2[ .]")
_H4 = re.compile(r"(?m)^## 4\.[ ]")
_BARE_BOLD = re.compile(r"^\*\*(.+)\*\*$")
_PREFIX_BOLD = re.compile(r"^\*\*(.+?)\*\*(.*)$")
_BACKTICKED = re.compile(r"`([^`]+)`")


class InputError(ValueError):
    """An input moved — reported as a FAIL line, never as a traceback."""


class SubGroup(NamedTuple):
    """A bold-prefixed §3.2 line with trailing prose, and the cases under it."""

    title: str
    trailing: str
    text: str
    named: tuple[str, ...]


def section_32_body(spec: str) -> str:
    """The text of §3.2, bounded by its own heading and the §4 anchor.

    Raises InputError when either anchor has moved. A moved anchor must be a
    failure and never an empty result: two gates in this repo shipped that
    defect (#165, #163) and passed with zero parsed rows.
    """
    start = _H32.search(spec)
    if start is None:
        raise InputError(
            "technical-spec.md has no '### 3.2 ...' heading — the test-contract "
            "section this gate reads has moved or been renamed"
        )
    rest = spec[start.end() :]
    end = _H4.search(rest)
    if end is None:
        raise InputError(
            "technical-spec.md has no '## 4. ' heading after §3.2 — the anchor "
            "that bounds the test-contract section has moved or been renamed"
        )
    return rest[: end.start()]


def subgroups(spec: str, modules: frozenset[str] = frozenset()) -> list[SubGroup]:
    """Every unindented bold-prefixed §3.2 line carrying trailing prose.

    A sub-group's text runs from its own line to the line before the next
    bold-prefixed line of any kind — sub-groups are siblings, so a sub-group
    ends where the next one begins, not where the generator's coarser cut
    would end it.
    """
    body = section_32_body(spec)
    lines = body.splitlines()
    bold: list[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        hit = _PREFIX_BOLD.match(line)
        if hit is not None:
            bold.append((i, hit))
    if not bold:
        raise InputError(
            "technical-spec.md §3.2 contains no bold lines at all — a parse "
            "that finds nothing is a broken parse, not a clean tree"
        )
    bold_at = [i for i, _ in bold]
    found: list[SubGroup] = []
    for position, (index, hit) in enumerate(bold):
        if _BARE_BOLD.match(lines[index]):
            continue  # a heading; check_test_contracts.py owns those
        trailing = hit.group(2)
        if not trailing.strip():
            continue
        stop = bold_at[position + 1] if position + 1 < len(bold_at) else len(lines)
        text = "\n".join(lines[index:stop]).strip()
        named = tuple(
            sorted({t for t in _BACKTICKED.findall(trailing) if t in modules})
        )
        found.append(SubGroup(hit.group(1), trailing.strip(), text, named))
    return found


def module_names(make_tasks_src: str) -> list[str]:
    """Every module name in `M`, in table order.

    Parsed with `ast`, never imported: importing `make_tasks` rewrites
    `tasks/`, and a gate that rewrites the very files it reads proves nothing
    about the files that are committed.
    """
    tree = ast.parse(make_tasks_src)
    table: object | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "M":
            table = ast.literal_eval(node.value)
            break
    if table is None:
        raise InputError(
            "scripts/make_tasks.py has no module-level `M = [...]` table — the "
            "generator table this gate reads has moved or been renamed"
        )
    if not isinstance(table, list) or not table:
        raise InputError("scripts/make_tasks.py `M` is empty")
    names: list[str] = []
    for entry in table:
        if not isinstance(entry, tuple) or len(entry) < 2:
            raise InputError(
                "scripts/make_tasks.py `M` has a malformed row: " + repr(entry)
            )
        name = entry[1]
        if not isinstance(name, str):
            raise InputError(
                "scripts/make_tasks.py `M` has a malformed row: " + repr(entry)
            )
        names.append(name)
    return names


def task_files(root: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """Task files on disk, keyed by the module slug in their name.

    `make_tasks.py` writes ``tasks/<order>-<module with dots as dashes>.md``.
    The order field is not derivable from the module name alone (it may be
    ``5b``), so the slug is matched after the first dash.
    """
    tasks = root / "tasks"
    if not tasks.is_dir():
        raise InputError(
            "tasks/ does not exist — the generated task files this gate reads "
            "are missing; run scripts/make_tasks.py"
        )
    out: dict[str, list[pathlib.Path]] = {}
    for path in sorted(tasks.glob("*.md")):
        stem = path.stem
        if "-" not in stem:
            continue
        out.setdefault(stem.split("-", 1)[1], []).append(path)
    if not out:
        raise InputError("tasks/ contains no generated task files")
    return out


def _slug(module: str) -> str:
    return module.replace(".", "-")


def evaluate(
    root: pathlib.Path,
    *,
    prose: dict[str, str] | None = None,
    undelivered: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    allow_prose = KNOWN_PROSE_BOLD_LINES if prose is None else prose
    allow_gap = KNOWN_UNDELIVERED_SUBGROUPS if undelivered is None else undelivered
    lines: list[str] = []

    spec = (root / "technical-spec.md").read_text(encoding="utf-8")
    make_tasks_src = (root / "scripts" / "make_tasks.py").read_text(encoding="utf-8")

    try:
        modules = frozenset(module_names(make_tasks_src))
        groups = subgroups(spec, modules)
        files = task_files(root)
    except InputError as exc:
        return 1, [f"FAIL {exc}"]

    delivered: set[str] = set()
    by_title: dict[str, SubGroup] = {}

    for group in groups:
        by_title[group.title] = group
        if not group.named:
            issue = allow_prose.get(group.title)
            if issue:
                lines.append(
                    f"PASS allowlisted §3.2 prose line **{group.title}** ({issue})"
                )
                continue
            seen = _BACKTICKED.findall(group.trailing)
            lines.append(
                f"FAIL §3.2 sub-group **{group.title}** names no module from `M` "
                f"in its trailing prose {group.trailing!r} (backticked names "
                f"seen: {seen or 'none'}) — a sub-group with no named owner "
                "reaches no task file, which is #189"
            )
            continue
        for module in group.named:
            key = f"{group.title}::{module}"
            paths = files.get(_slug(module), [])
            if len(paths) != 1:
                lines.append(
                    f"FAIL §3.2 sub-group **{group.title}** names `{module}`, but "
                    f"tasks/ has {len(paths)} file(s) matching "
                    f"'*-{_slug(module)}.md' — expected exactly one"
                )
                continue
            if group.text in paths[0].read_text(encoding="utf-8"):
                delivered.add(key)
                continue
            issue = allow_gap.get(key)
            if issue:
                lines.append(
                    f"PASS allowlisted undelivered sub-group **{group.title}** → "
                    f"`{module}` ({issue})"
                )
                continue
            lines.append(
                f"FAIL §3.2 sub-group **{group.title}** names `{module}`, but its "
                f"cases are absent from {paths[0].as_posix()} — specified for a "
                "module and delivered to nobody, which is #189"
            )

    # Requirement 4, prose arm: an entry that stopped describing a prose line.
    for title, issue in sorted(allow_prose.items()):
        group = by_title.get(title)
        if group is None:
            lines.append(
                f"FAIL stale allowlist entry **{title}** ({issue}) — §3.2 has no "
                "such bold line with trailing prose; delete the entry"
            )
        elif group.named:
            lines.append(
                f"FAIL stale allowlist entry **{title}** ({issue}) — the line now "
                f"names {', '.join(group.named)}, so it is a sub-group and must "
                "be delivered, not waived; delete the entry"
            )

    # Requirement 4, delivery arm: an entry that stopped describing a real gap.
    for key, issue in sorted(allow_gap.items()):
        if "::" not in key:
            lines.append(
                f"FAIL malformed allowlist key {key!r} ({issue}) — expected "
                "'<sub-group title>::<module>'"
            )
            continue
        title, module = key.split("::", 1)
        group = by_title.get(title)
        if group is None:
            lines.append(
                f"FAIL stale allowlist entry {key!r} ({issue}) — §3.2 has no "
                f"sub-group **{title}**; delete the entry"
            )
        elif module not in group.named:
            lines.append(
                f"FAIL stale allowlist entry {key!r} ({issue}) — **{title}** no "
                f"longer names `{module}`; delete the entry"
            )
        elif key in delivered:
            lines.append(
                f"FAIL stale allowlist entry {key!r} ({issue}) — the cases now "
                f"reach `{module}`'s task file; delete the entry"
            )

    failed = [line for line in lines if line.startswith("FAIL")]
    if failed:
        return 1, lines
    pairs = sum(len(group.named) for group in groups)
    lines.append(
        f"PASS sub-group delivery: {len(groups)} bold-prefixed §3.2 line(s), "
        f"{pairs} sub-group/module pair(s), {len(delivered)} delivered, "
        f"{len(allow_gap)} allowlisted gap(s), {len(allow_prose)} prose line(s)"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
