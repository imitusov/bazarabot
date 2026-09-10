#!/usr/bin/env python3
"""Fail when spec §4, dependency-order.md and `M` in make_tasks.py disagree.

Issue #118 gate 4 (module-set completeness). Three documents each carry the
project's module list: technical-spec.md §4 has one ``### `path` `` heading
per contract, dependency-order.md numbers the build sequence, and
``scripts/make_tasks.py``'s ``M`` table decides which task files an agent is
ever handed. A module present in the spec and absent from ``M`` has a full
contract that reaches nobody — #96 — and nothing said so.

This gate checks:

  1. Every §4 heading is claimed by at least one ``M`` entry, using the same
     matching rule ``make_tasks.section()`` uses (a ``### `` line containing
     the entry's spec-key as a substring). A contract no task file carries.
  2. Every ``M`` spec-key resolves to EXACTLY ONE ``### `` heading in the
     whole spec, and that heading is inside §4. ``section()`` takes the first
     substring match over the entire document, so a key matching two headings
     silently generates the wrong contract, and a key matching a heading
     outside §4 generates a section that is not a contract at all. A key
     matching none produces the "SPEC SECTION NOT FOUND" placeholder, which
     is visible in the task file but red in no check.
  3. The module NAMES in ``M`` and the numbered modules in
     dependency-order.md are the same set, both directions.

WHAT THIS DOES NOT COVER, stated because a guard named "module-set
completeness" must enumerate the completeness it does not prove
(failure class 6):

  * Whether a module in all three lists has any code. `check_docs.py` checks
    1-3 relate the spec to `interfaces.md`; nothing here opens a ``.py``.
  * ORDER. dependency-order.md's ordinals and ``M``'s first field are
    compared as sets, not sequences: they legitimately differ today
    (``telegram.notifier`` is 4b in dependency-order.md and 28 in ``M``), so
    a reordering that breaks the build sequence passes this gate.
  * ``M``'s other fields — the test-contract key is
    `check_test_contracts.py`'s, the tables are `check_docs.py` check 5's,
    the error rules are check 4's. The context blurb is gated by nothing.
  * The CONTENT of a §4 heading's body. A heading with an empty contract
    under it is a module as far as this gate is concerned.
  * Headings outside §4, and the ``sandbox/`` heading's internal structure:
    three ``M`` entries share the single ``sandbox/`` key by design, so this
    gate cannot tell a missing sandbox contract from a shared one.
  * `dependency-order.md`'s dependency EDGES. Only the module names on the
    numbered lines are read; "depends on: …" is not parsed.

A green run means the three module lists name the same modules and every task
file gets the contract it asks for — NOT that those contracts are correct,
complete, ordered, or implemented.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# Divergences present when this gate was written. Each key names its issue.
# All three arms are empty today: the module lists agree. An entry that stops
# describing a real divergence FAILS rather than being ignored, so the
# allowlist cannot outlive the gap it documents.
KNOWN_UNCLAIMED_SPEC_HEADINGS: dict[str, str] = {}
KNOWN_MODULE_SET_DRIFT: dict[str, str] = {}

_H4 = re.compile(r"(?m)^## 4\.[ ]")
_NEXT_H2 = re.compile(r"(?m)^## ")
_H3 = re.compile(r"^### ")
_DEP_MODULE = re.compile(r"(?m)^\d+[a-z]?\.\s+\*\*([\w.*_]+)\*\*")


class InputError(ValueError):
    """An input moved — reported as a FAIL line, never as a traceback."""


def section_4_bounds(spec: str) -> tuple[int, int]:
    """Line indices [start, end) of technical-spec.md §4."""
    lines = spec.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _H4.match(line):
            start = i
            break
    if start is None:
        raise InputError(
            "technical-spec.md has no '## 4. …' heading — §4 was renamed or "
            "renumbered, so the module contracts are no longer gated"
        )
    for i in range(start + 1, len(lines)):
        if _NEXT_H2.match(lines[i]):
            return start, i
    return start, len(lines)


def spec_headings(spec: str) -> list[str]:
    """Every ``### `` heading line inside §4, verbatim."""
    lines = spec.splitlines()
    start, end = section_4_bounds(spec)
    found = [line for line in lines[start:end] if _H3.match(line)]
    if not found:
        raise InputError("technical-spec.md §4 contains no '### ' module headings")
    return found


def all_h3(spec: str) -> list[tuple[int, str]]:
    """(line index, text) for every ``### `` heading in the whole document."""
    return [(i, l) for i, l in enumerate(spec.splitlines()) if _H3.match(l)]


def module_table(source: str) -> list[tuple]:
    """The literal ``M`` table from scripts/make_tasks.py, via ast."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "M":
            table = ast.literal_eval(node.value)
            if not table:
                raise InputError("scripts/make_tasks.py's M table is empty")
            return list(table)
    raise InputError(
        "scripts/make_tasks.py has no module-level `M = [...]` assignment — "
        "the task generator's module list is no longer gated"
    )


def dependency_modules(dep: str) -> set[str]:
    """Module names on dependency-order.md's numbered lines."""
    found = set(_DEP_MODULE.findall(dep))
    if not found:
        raise InputError(
            "dependency-order.md has no '<n>. **module**' lines — the build "
            "sequence format changed"
        )
    return found


def _check_keys(spec: str, table: list[tuple], lines: list[str]) -> None:
    headings = spec_headings(spec)
    start, end = section_4_bounds(spec)
    every = all_h3(spec)
    claimed: set[str] = set()
    for entry in table:
        name, key = entry[1], entry[2]
        matches = [(i, text) for i, text in every if key in text]
        inside = [(i, text) for i, text in matches if start <= i < end]
        if not matches:
            lines.append(
                f"FAIL M entry {name!r} spec-key {key!r} matches no '### ' "
                "heading — make_tasks.py emits SPEC SECTION NOT FOUND"
            )
            continue
        if len(matches) > 1:
            where = ", ".join(text.strip() for _, text in matches)
            lines.append(
                f"FAIL M entry {name!r} spec-key {key!r} matches {len(matches)} "
                f"headings ({where}) — section() takes the first, so the task "
                "file may carry the wrong contract"
            )
            continue
        if not inside:
            lines.append(
                f"FAIL M entry {name!r} spec-key {key!r} matches "
                f"{matches[0][1].strip()!r}, which is outside §4 — the task "
                "file would carry a non-contract section"
            )
            continue
        claimed.add(inside[0][1])
    waived: list[str] = []
    for heading in headings:
        if heading in claimed:
            continue
        issue = KNOWN_UNCLAIMED_SPEC_HEADINGS.get(heading.strip())
        if issue:
            waived.append(f"{heading.strip()} ({issue})")
            continue
        lines.append(
            f"FAIL §4 heading {heading.strip()!r} is claimed by no M entry — "
            "its contract reaches no task file"
        )
    for heading, issue in sorted(KNOWN_UNCLAIMED_SPEC_HEADINGS.items()):
        if heading in {h.strip() for h in claimed}:
            lines.append(
                f"FAIL allowlist entry {heading!r} ({issue}) is stale — an M "
                "entry now claims it; delete the entry"
            )
        elif heading not in {h.strip() for h in headings}:
            lines.append(
                f"FAIL allowlist entry {heading!r} ({issue}) is stale — §4 has "
                "no such heading; delete the entry"
            )
    if waived:
        lines.append("PASS allowlisted unclaimed §4 headings: " + ", ".join(waived))


def _check_dependency_order(
    dep: str, table: list[tuple], lines: list[str]
) -> None:
    dep_modules = dependency_modules(dep)
    m_modules = {entry[1] for entry in table}
    waived: list[str] = []
    for name in sorted(m_modules - dep_modules):
        issue = KNOWN_MODULE_SET_DRIFT.get(name)
        if issue:
            waived.append(f"{name} ({issue})")
            continue
        lines.append(
            f"FAIL M has module {name!r} with no numbered entry in "
            "dependency-order.md"
        )
    for name in sorted(dep_modules - m_modules):
        issue = KNOWN_MODULE_SET_DRIFT.get(name)
        if issue:
            waived.append(f"{name} ({issue})")
            continue
        lines.append(
            f"FAIL dependency-order.md numbers module {name!r} that M does not "
            "list — no task file is generated for it"
        )
    for name, issue in sorted(KNOWN_MODULE_SET_DRIFT.items()):
        if (name in dep_modules) == (name in m_modules):
            lines.append(
                f"FAIL allowlist entry {name!r} ({issue}) is stale — the two "
                "lists agree about it now; delete the entry"
            )
    if waived:
        lines.append("PASS allowlisted module-set drift: " + ", ".join(waived))


def evaluate(root: pathlib.Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    try:
        spec = (root / "technical-spec.md").read_text(encoding="utf-8")
        dep = (root / "dependency-order.md").read_text(encoding="utf-8")
        table = module_table(
            (root / "scripts" / "make_tasks.py").read_text(encoding="utf-8")
        )
    except (OSError, InputError, SyntaxError, ValueError) as exc:
        return 1, [f"FAIL {exc}"]
    for arm in (_check_keys, _check_dependency_order):
        try:
            if arm is _check_keys:
                arm(spec, table, lines)
            else:
                arm(dep, table, lines)
        except InputError as exc:
            lines.append(f"FAIL {exc}")
    if any(line.startswith("FAIL") for line in lines):
        return 1, lines
    lines.append(
        f"PASS module set: {len(table)} M entries, "
        f"{len(spec_headings(spec))} §4 headings, "
        f"{len(dependency_modules(dep))} dependency-order modules — consistent"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
